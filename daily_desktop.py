"""Wire the existing Qt mascot to the existing daily app in the SAME process."""
import json, os
from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import QObject,QTimer,Signal
from PySide6.QtWidgets import QApplication,QMessageBox
from experiments.desktop_pet.window import PetWindow
from daily_voice_status import VoiceStatus
ROOT=Path(__file__).resolve().parent

class DailyDesktop(QObject):
    audit=Signal(object)
    def __init__(self,overlay):
        super().__init__(overlay.control)
        self.overlay=overlay;self.control=overlay.control;self.voice=VoiceStatus();self._state=None
        self.text_only=getattr(overlay,'text_only',False)
        self.wechat_dialog=None;self.diagnostic_reply=None;self.memory_dialog=None;self.performance_dialog=None
        self.control.close_to_hide=True;self.control.hide()
        self.pet=PetWindow(daily=True);self.pet.open_panel.connect(self.open_chat)
        self.pet.command.connect(self.command);self.pet.quit_requested.connect(overlay._quit)
        self.unsubscribe=None;self.timer=None
        if self.text_only:
            overlay.changed.connect(self.refresh)
            # Hide the pet before QApplication.quit: its normal close handler keeps it alive.
            overlay.quitting.connect(self.pet.dispose)
        else:
            from tts_events import subscribe
            self.control.local_test_requested.connect(self.local_test)
            self.control.cloud_test_requested.connect(self.cloud_test)
            self.audit.connect(self.on_event);self.unsubscribe=subscribe(self.audit.emit)
            self.timer=QTimer(self);self.timer.setInterval(250);self.timer.timeout.connect(self.refresh);self.timer.start()
        overlay.translation.changed.connect(self.refresh)
        overlay.practice.changed.connect(self.refresh)
        self.refresh();self.pet.show()
        from performance.shortcut import PerformanceShortcut
        self.performance_shortcut=PerformanceShortcut(self.open_performance)
        QApplication.instance().aboutToQuit.connect(self.cleanup)
    def open_chat(self):
        from time import monotonic
        began=monotonic()
        self.control.showNormal();self.control.raise_();self.control.activateWindow()
        if self.text_only:self.overlay.ui_metrics.append(dict(kind='open_window_ui',ms=(monotonic()-began)*1000))
    def command(self,name,args):
        o=self.overlay
        if o.practice.engaged and name in ('translation','mic','voice'):
            o.practice.open();return
        if name=='memory':
            from memory.ui import MemoryDialog
            if self.memory_dialog is None:self.memory_dialog=MemoryDialog(o.companion,self.control)
            self.memory_dialog.tabs.setCurrentIndex(args.get('tab',0));self.memory_dialog.show();self.memory_dialog.raise_()
        elif name=='mode':
            # A mode change invalidates pending replies before changing TTS policy.
            if o._pause_gate.allows():o._stop_current_reply()
            mode=args['mode'];o.companion.set_mode(mode)
            self.control._set_mode('learning' if mode=='CLASS' else 'entertainment',persist=False)
            if not self.text_only:o._on_mode_changed(self.control.current_mode())
        elif name=='voice':
            self.control.voice_settings_requested.emit()
        elif name=='wechat':
            from integrations.wechat.ui import WechatDialog
            if self.wechat_dialog is None:self.wechat_dialog=WechatDialog(o.wechat,self.control)
            self.wechat_dialog.show();self.wechat_dialog.raise_()
        elif name=='settings':
            self.open_chat()
            if not self.control._expanded:self.control._toggle_more()
        elif name=='performance':
            self.open_performance()
        elif name in ('start','resume'):o._resume_all()
        elif name=='pause':o._pause_all()
        elif name=='translation':
            if o._pause_gate.allows():
                self.control._set_mode('learning'); o.translation.toggle()
        elif name=='translation_settings':o.translation.open_settings()
        elif name=='practice':o.practice.open()
        elif name=='interrupt':o._stop_current_reply()
        elif name=='mic' and not self.text_only:o._apply_settings(replace(o._settings,mic_enabled=args['enabled']))
        self.refresh()
    def open_performance(self):
        # Loaded only when explicitly opened.
        try:
            from performance.panel import PerformancePanel
        except ImportError:return
        if self.performance_dialog is None:self.performance_dialog=PerformancePanel(self.overlay,self.control)
        self.performance_dialog.show();self.performance_dialog.raise_()

    def on_event(self,row):
        if row.get('pid')!=os.getpid():return
        if row.get('reply_id')==self.diagnostic_reply:
            with (ROOT/'local_tts/logs/daily-click-diagnostic.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(row,ensure_ascii=False)+'\n')
        self.voice.apply(row)
        if row['event'] in ('ERROR','REPLY_FAILED','PLAYBACK_FAILED') and self.overlay._pause_gate.allows():
            if self.control._failure!=str(row.get('reason','')):self.control.set_failure(str(row.get('reason','')))
        self.refresh()
    def refresh(self):
        o=self.overlay;s=o._settings
        translating=o.translation.active
        self.control._translation_active=translating
        self.control._translation_mic=o.translation.uses_microphone
        self.control._translation_source=o.translation.source_description
        self.control._refresh_status()
        if self.text_only:
            summary='学习实时翻译 · '+o.translation.source_description if translating else '仅文字 · 未启用麦克风'
            if o.practice.engaged:summary='口语练习 · '+('麦克风收音中' if o.practice.uses_microphone else '参考回答仅字幕')
            self.control.set_voice_state(summary,'本机预设：'+str(o.device_store.path)+'\n默认仅文字；学习翻译需手动开启，Windows 字幕管理声音来源，不加载朗读、GPU 监测或本地模型。')
            state=dict(mode=o.companion.mode,state=o._pause_gate.state,mic_active=o.translation.uses_microphone or o.practice.uses_microphone,mic_enabled=False,text_only=True,translation_active=translating,practice_active=o.practice.engaged,
                learning=self.control.current_mode()=='learning',
                thinking=o._busy(),speaking=False,caption=self.control._last_reply,
                instance=f'daily-{os.getpid()}',voice_status=summary)
            if state!=self._state:self._state=state;self.pet.update_state(state)
            return
        mic=bool(getattr(o._mic,'_accepting',False) and getattr(o._mic,'_stream',None) is not None)
        system=bool(o._system_ok and getattr(o._system,'_running',False))
        self.control.set_capture_state(mic,system,s.mic_enabled,o._system_enabled)
        summary=self.voice.summary()
        if o.practice.engaged:summary='口语练习 · '+('麦克风收音中' if o.practice.uses_microphone else '参考回答仅字幕')
        if o._pause_gate.state=='PAUSED':summary='已暂停 · '+('上次 '+self.voice.actual if self.voice.actual else '未播放声音')
        detail=(f'引擎：日常对话（支持外部 TTS）\n保存偏好：{s.tts_mode or s.voice_backend} · {s.local_voice}；云端备选 {s.voice_name}\n'
                f'本轮实际：{self.voice.actual or "尚未取得有效音频"} · {self.voice.phase}\n原因：{self.voice.reason or "无"}\n'
                f'配置：{o._settings_store.path}\n本地服务：127.0.0.1:9881；按需启动；首次加载会较慢。')
        self.control.set_voice_state(summary,detail)
        state=dict(mode=o.companion.mode,state=o._pause_gate.state,mic_active=mic or o.translation.uses_microphone or o.practice.uses_microphone,mic_enabled=s.mic_enabled,practice_active=o.practice.engaged,
            learning=self.control.current_mode()=='learning',translation_active=translating,
            thinking=bool(o._voice_worker or o._worker or o._speech._worker),
            speaking=bool(o._player.is_busy() and o._player._started),
            caption=self.control._last_reply,instance=f'daily-{os.getpid()}',voice_status=summary)
        if state!=self._state:
            self._state=state;self.pet.update_state(state);self.control._refresh_status()
    def local_test(self):
        if self.text_only:return
        from tts_pipeline import SpeechJob
        o=self.overlay
        if not o._pause_gate.allows() or o._busy():return
        settings=replace(o._settings,tts_fallback=False,speech_quirk_enabled=False)
        job=SpeechJob('我在，今天想聊点什么？',settings,source='preview',local_only=True)
        self.control.append_message('本地试听','我在，今天想聊点什么？')
        self.control.set_busy(True);o._speech.submit(job)
    def cloud_test(self):
        if self.text_only:return
        o=self.overlay
        if not o._pause_gate.allows() or o._busy():return
        box=QMessageBox(self.control);box.setWindowTitle('当前链路测试')
        box.setText('将发送一个固定短问题：“用一句话说说你能帮我做什么。”\n\n使用当前模型、人格和声音模式；不截图、不附带录音。会产生一次模型请求，以及当前声音策略需要的 TTS 请求。\n计时仅记录事件，不保存你的日常对话。')
        run=box.addButton('开始测试（使用 API）',QMessageBox.AcceptRole);box.addButton('取消',QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is run and o._pause_gate.allows() and not o._busy():
            self.control.chat_input.setText('用一句话说说你能帮我做什么。');self.control._on_chat_send()
            if o._voice_worker:self.diagnostic_reply=o._voice_worker.reply_id
    def cleanup(self):
        self.performance_shortcut.close()
        if self.timer:self.timer.stop()
        if self.unsubscribe:self.unsubscribe()
        self.pet.dispose()
        self.overlay.translation.stop();self.overlay.translation.window.hide()
        self.overlay.practice.stop()
        if self.overlay.practice.window:self.overlay.practice.window.hide()
        if not self.text_only:
            from local_tts_service import close_owned
            close_owned()
