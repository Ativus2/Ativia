"""Text-only run preset for the existing daily Qt app, sharing its UI and persona."""
from collections import deque
import hashlib
from pathlib import Path
from time import monotonic
from uuid import uuid4
from PySide6.QtCore import QObject,QTimer,Signal,Qt
from PySide6.QtWidgets import QApplication,QDialog,QLabel,QVBoxLayout,QPushButton
from assistant_pause import PauseGate,running_only
from character import CharacterMemory
from persona import ConversationRhythm
from runtime_settings import SettingsStore
from controls import ControlBar
from text_engine import TextEngine


class TextSession(QObject):
    changed=Signal()
    quitting=Signal()
    text_only=True

    def __init__(self,device_store,*,start_paused=False):
        super().__init__()
        self.device_store=device_store;self._settings_store=SettingsStore();self._settings=self._settings_store.load()
        self._pause_gate=PauseGate(start_paused);self.control=ControlBar(text_only=True)
        from companion_runtime import CompanionRuntime
        self.companion=CompanionRuntime();self.control.companion=self.companion
        from performance.scheduler import TaskScheduler
        self.scheduler=TaskScheduler()
        self._scheduler_timer=QTimer(self.control);self._scheduler_timer.setInterval(250)
        self._scheduler_timer.timeout.connect(self.scheduler.expire);self._scheduler_timer.start()
        self.companion.memory.scheduler=self.scheduler
        self.control._set_mode('learning' if self.companion.mode=='CLASS' else 'entertainment',persist=False)
        self._engine=TextEngine(self);self._active=None;self._pending=[];self._capture_pending=False
        self._active_image=None;self._last_image=None;self._last_summary=None;self._last_command=None
        self._recent_turns=deque(maxlen=3);self._character_memory=CharacterMemory();self._conversation=ConversationRhythm()
        self._select=None;self._actions=None;self._selected_image=None;self._selection_command=''
        self._quitting=False;self._dialogs=[];self.ui_metrics=deque(maxlen=64)
        self._flush=QTimer(self);self._flush.setSingleShot(True);self._flush.setInterval(40);self._flush.timeout.connect(self._flush_text)
        self._capture_timer=QTimer(self);self._capture_timer.setSingleShot(True)
        self._capture_callback=None
        self._capture_timer.timeout.connect(self._run_capture_callback)
        self._hotkey=None
        self.control.chat_submitted.connect(self._on_chat_submitted)
        self.control.pause_requested.connect(self._toggle_pause)
        self.control.stop_reply_requested.connect(self._stop_current_reply)
        self.control.quit_requested.connect(self._quit)
        self.control.observe_requested.connect(self._start_observe)
        self.control.snip_requested.connect(self._on_snip)
        self.control.explain_requested.connect(self._on_further_explanation)
        self.control.voice_settings_requested.connect(self._open_device_settings)
        self.control.trace_requested.connect(self._resource_help)
        self._engine.delta.connect(self._delta);self._engine.completed.connect(self._completed)
        self._engine.failed.connect(self._failed);self._engine.settled.connect(self._settled)
        self._engine.closed.connect(self._closed)
        QTimer.singleShot(0,self._setup_hotkey)
        from live_translation import TranslationController
        self.translation=TranslationController(self.control,self._pause_gate)
        from language_practice.controller import PracticeController
        self.practice=PracticeController(self)
        self._refresh()

    def _setup_hotkey(self):
        if self._quitting:return
        from hotkey import AltQWatcher
        self._hotkey=AltQWatcher(self.control);self._hotkey.activated.connect(self._on_snip)
        self._hotkey.status_changed.connect(self.control.set_hotkey_status)
        self.control.set_hotkey_status(self._hotkey.available,self._hotkey.error)

    def _refresh(self):
        self.control.set_lifecycle(self._pause_gate.state,'仅接受主动文字或截图；暂停不补发，恢复仍为仅文字。')
        self.control.set_busy(self._busy());self.changed.emit()

    def _busy(self):return self._active is not None or self._capture_pending

    def _context_for_request(self):
        # Same bounded request context as daily voice; completed UI history is never pruned.
        now=monotonic()
        while self._recent_turns and now-self._recent_turns[0][0]>180:self._recent_turns.popleft()
        return '\n'.join(f'用户：{command}\nAtivia：{reply}' for _,command,reply in self._recent_turns)

    @running_only
    def _on_chat_submitted(self,command):
        if self.practice.engaged:self.practice.open();return
        if command.strip():
            if self._busy():self._cancel()
            self._submit(command)

    def _submit(self,command,image=None,previous_summary=None):
        if not self._pause_gate.allows() or self._active:return
        reply=uuid4().hex;epoch=self._pause_gate.epoch
        self._ticket=self.scheduler.admit('text',request_id=reply,timeout=45,cancel=self._cancel,replace=True)
        self.scheduler.start(self._ticket)
        from performance.metrics import metrics
        metrics.mark(reply,'agent_start',ms=(self._ticket.started-self._ticket.created)*1000)
        self._active=reply;self._active_image=image;self._active_command=command;self._active_mode=self.control.current_mode()
        self._request_began=monotonic();self._first_delta=True;self._pending=[]
        self.companion.refresh_environment()
        self.companion.remember_input(command)
        request=dict(spoken=command,recent_context=self._context_for_request(),
            observation_context=self._character_memory.context(monotonic()),settings=self._settings,mode=self._active_mode,
            companion_context=self.companion.context(command,legacy_mode=self._active_mode))
        if image is not None:request.update(image_jpeg=image,previous_summary=previous_summary)
        self.control.begin_stream();self._refresh()
        self._engine.submit(reply,request,lambda:self._active==reply and self._pause_gate.allows(epoch) and self._ticket.valid())

    def _valid(self,reply):return self._active==reply and self._pause_gate.allows()

    def _delta(self,reply,text):
        if not self._valid(reply):return
        self._pending.append(text)
        if self._first_delta:
            self._first_delta=False;self._flush_text()
            self.ui_metrics.append(dict(kind='first_display_delta',ms=(monotonic()-self._request_began)*1000))
        elif not self._flush.isActive():self._flush.start()

    def _flush_text(self):
        if self._active is not None and self._pause_gate.allows() and self._pending:
            self.control.append_stream(''.join(self._pending));self._pending.clear();self.changed.emit()

    def _completed(self,reply,text,metrics):
        if not self._valid(reply):return
        self._flush.stop();self._flush_text();self.control.finish_stream(text)
        self._conversation.engaged()
        self.companion.record(self._active_command,text)
        self._recent_turns.append((monotonic(),self._active_command[:120],text[:240]))
        if self._active_image is not None and self._active_mode=='learning':
            self._last_image=self._active_image;self._last_summary=text;self._last_command=self._active_command
            self.control._has_learning_summary=True
        self._active=None;self._active_image=None;self._refresh()

    def _failed(self,reply,message):
        if not self._valid(reply):return
        self._flush.stop();self._flush_text();self.control.finish_stream()
        self._active=None;self._active_image=None;self._refresh();self.control.set_failure(message)

    def _settled(self,reply):
        ticket=getattr(self,'_ticket',None)
        if ticket and ticket.request_id==reply:self.scheduler.finish(ticket)
        if self._active==reply:
            self._active=None;self._active_image=None;self._pending.clear();self._flush.stop()
            self.control.finish_stream();self._refresh()

    def _cancel(self):
        had_reply=self._active is not None
        ticket=getattr(self,'_ticket',None)
        if ticket:
            ticket.cancel_callback=None;self.scheduler.cancel(ticket,'user_cancelled')
        self._active=None;self._active_image=None;self._pending.clear();self._flush.stop();self._engine.cancel()
        self._capture_timer.stop();self._capture_callback=None;self._capture_pending=False;self._selected_image=None
        if self._select:self._select.release_capture();self._select.hide()
        if self._actions:self._actions.hide()
        self.control.finish_stream()
        if had_reply:self.control.append_message('状态','本轮回答已停止。')

    @running_only
    def _stop_current_reply(self):
        if self.practice.engaged:self.practice.stop()
        began=monotonic();self._pause_gate.epoch+=1;self._cancel();self._refresh()
        self.ui_metrics.append(dict(kind='cancel_ui',ms=(monotonic()-began)*1000))

    def _pause_all(self):
        if self._pause_gate.state!='RUNNING':return
        began=monotonic();self._pause_gate.transition('PAUSING');self.companion.memory.paused=True;self._cancel()
        self.practice.stop();self.translation.stop();self._finish_pause()
        self.ui_metrics.append(dict(kind='pause_ui',ms=(monotonic()-began)*1000))

    def _finish_pause(self):
        if self._quitting:return
        if self.translation.worker or not self.practice.quiescent() or (getattr(self,'wechat',None) and not self.wechat.quiescent()):
            QTimer.singleShot(30,self._finish_pause);self._refresh();return
        self._pause_gate.transition('PAUSED');self._refresh()

    def _resume_all(self):
        if self._pause_gate.state!='PAUSED':return
        began=monotonic();self._pause_gate.transition('RESUMING');self._pause_gate.transition('RUNNING');self.companion.memory.paused=False;self._refresh()
        self.ui_metrics.append(dict(kind='resume_ui',ms=(monotonic()-began)*1000))

    def _toggle_pause(self):
        if self._pause_gate.allows():self._pause_all()
        else:self._resume_all()

    def _later_capture(self,callback):
        epoch=self._pause_gate.epoch
        self._capture_callback=lambda:callback() if self._pause_gate.allows(epoch) else None
        self._capture_timer.start(180)

    def _run_capture_callback(self):
        callback=self._capture_callback;self._capture_callback=None
        if callback:callback()

    @running_only
    def _start_observe(self):
        if self.practice.engaged:return
        if self._busy():return
        self._selection_command=self.control.chat_input.text().strip()
        self._capture_pending=True;self.control.hide();self._refresh()
        self._later_capture(lambda:self._capture(None))

    def _on_snip(self):
        if self.practice.engaged:return
        if not self._pause_gate.allows():
            self.control.show_snip_notice('助手已暂停。请先点击恢复，再按 Alt+Q 框选。');return
        if self._busy():
            self.control.show_snip_notice('当前任务正在进行，请等待完成或点击停止回答后再框选。');return
        if self._busy():return
        if self._select is None:
            from region_select import RegionSelectOverlay,LearningActionMenu
            self._select=RegionSelectOverlay();self._actions=LearningActionMenu()
            self._select.region_selected.connect(self._selected);self._select.cancelled.connect(self._capture_cancelled)
            self._actions.action_chosen.connect(self._image_action);self._actions.cancelled.connect(self._capture_cancelled)
        self._selection_command=self.control.chat_input.text().strip()
        self._capture_pending=True;self.control.hide();self._refresh()
        self._later_capture(self._begin_snip)

    def _begin_snip(self):
        try:self._select.begin(live=True)
        except Exception:
            self._capture_cancelled();self.control.set_failure("无法打开框选，请重试。")

    def _selected(self,rect):
        if self._pause_gate.allows():self._later_capture(lambda:self._capture(rect))

    def _capture(self,rect):
        if not self._pause_gate.allows():return
        try:
            from screenshot import capture_selected_jpeg
            image=capture_selected_jpeg(rect)
            if len(image)>8*1024*1024:raise ValueError('选区较大，请缩小范围后重试。')
            if self._select:self._select.release_capture()
            if not self.control.close_to_hide:self.control.show()
            if rect is not None and self.control.current_mode()=='learning' and not self._selection_command:
                self._selected_image=image;self._actions.popup_near(rect.center());return
            self._capture_pending=False
            command=self._selection_command or '请根据本次截图，简短说明主要内容。'
            self.control.append_message('你',command+'（已附本次截图）');self._submit(command,image)
        except Exception as exc:
            self._capture_cancelled();self.control.set_failure('截图失败：'+str(exc))

    def _image_action(self,command):
        if not self._pause_gate.allows():return
        image=self._selected_image;self._selected_image=None;self._capture_pending=False
        if image is not None:self.control.append_message('你',command+'（已附本次框选）');self._submit(command,image)

    def _capture_cancelled(self):
        self._selected_image=None;self._capture_pending=False
        if self._select:self._select.release_capture()
        if not self._quitting and not self.control.close_to_hide:self.control.show()
        self._refresh()

    @running_only
    def _on_further_explanation(self):
        if not self._busy() and self._last_image and self._last_summary:
            self._submit(self._last_command or '进一步解释',self._last_image,self._last_summary)

    def _dialog(self,title,text):
        dialog=QDialog(self.control);dialog.setWindowTitle(title);layout=QVBoxLayout(dialog)
        label=QLabel(text);label.setWordWrap(True);label.setMinimumWidth(360);layout.addWidget(label)
        close=QPushButton('关闭');close.clicked.connect(dialog.accept);layout.addWidget(close)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._dialogs.append(dialog);dialog.destroyed.connect(lambda:self._dialogs.remove(dialog))
        dialog.show();return dialog,layout

    def _open_device_settings(self):
        dialog,layout=self._dialog('设备运行方式','当前：仅文字 · 未启用麦克风。\n本机预设：'+str(self.device_store.path)+'\n语音和音色配置保留。切换后需退出并重新启动，再明确点击恢复才启用保存的语音输入。')
        button=QPushButton('下次启动使用语音模式（需手动恢复）')
        button.clicked.connect(lambda:(self.device_store.save('voice'),dialog.accept()))
        layout.addWidget(button)

    def _resource_help(self):
        self._dialog('资源检查','运行 check-text-resources.bat，选择当前场景，保持一分钟。仅采集本实例进程资源和阶段时间，不保存截图或聊天文字，不自动发送请求。')

    def snapshot(self):
        import os,sys
        return dict(pid=os.getpid(),mode='text',state=self._pause_gate.state,
            capture_pending=self._capture_pending,image_cache_bytes=sum(len(x) for x in (self._active_image,self._last_image,self._selected_image) if x),
            ui_metrics=list(self.ui_metrics),**self._engine.snapshot(),
            imported_audio_modules=[m for m in ('audio','sounddevice','soundcard','speech_activity','voice_pipeline','tts_pipeline','gpu_monitor','torch') if m in sys.modules])

    def _quit(self):
        if self._quitting:return
        self._quitting=True;self._pause_gate.transition('PAUSING');self._cancel()
        self.practice.stop()
        if self.practice.window:self.practice.window.hide()
        self.translation.stop()
        if self._hotkey:self._hotkey.stop()
        if getattr(self,'wechat',None):self.wechat.close()
        self.companion.close()
        self.control.hide();self.quitting.emit();self._engine.close()

    def _closed(self):
        if self._quitting:
            if self.translation.worker or not self.practice.quiescent() or not self.companion.memory.close_future.done() or (getattr(self,'wechat',None) and not self.wechat.closed_finished()):QTimer.singleShot(30,self._closed)
            else:self.translation.window.hide();QApplication.instance().quit()
