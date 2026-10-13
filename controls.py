"""Compact Qt chat; existing OverlayApp owns requests, capture and speech."""
from html import escape
from typing import Literal
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from experiments.desktop_pet.appearance import character_icon
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QLineEdit,QPlainTextEdit,QTextBrowser,QSlider,QProgressBar,QFrame)
Mode=Literal['entertainment','learning']
DEFAULT_VOLUME=.8
VOLUME_STEP=.1
STYLE='''
QWidget{color:#e0e4ec;font:13px "Microsoft YaHei";background:#202329;}
QLabel{background:transparent;} QLabel#wordmark{font-size:19px;font-weight:600;} QLabel#muted{color:#969fab;font-size:12px;}
QFrame#settings{background:#282c33;border:1px solid #3a404b;border-radius:6px;}
QPushButton{background:#2d323b;border:1px solid #414956;border-radius:5px;padding:7px 10px;}
QPushButton:hover{background:#384351;border-color:#62768f;} QPushButton:pressed{background:#243449;}
QPushButton:disabled{color:#68717c;border-color:#343a44;} QPushButton:checked{background:#2c4260;border-color:#5a81b4;}
QPushButton#primary{background:#476eaa;border-color:#557eba;color:white;}
QLineEdit{background:#282d35;border:1px solid #424c5c;border-radius:5px;padding:10px;selection-background-color:#466ea1;}
QLineEdit:focus{border-color:#749acd;}
QTextBrowser,QPlainTextEdit{background:#24282e;border:1px solid #343b46;border-radius:5px;padding:10px;selection-background-color:#466ea1;}
QScrollBar:vertical{background:#252a31;width:8px;margin:0;} QScrollBar::handle:vertical{background:#4a5463;min-height:24px;border-radius:3px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
QSlider::groove:horizontal{background:#414955;height:4px;} QSlider::handle:horizontal{background:#79a5df;width:12px;margin:-5px 0;border-radius:5px;}
QProgressBar{border:none;background:#2c323a;} QProgressBar::chunk{background:#608bca;}
QComboBox,QDoubleSpinBox{background:#2d323b;border:1px solid #424c5c;padding:5px;}
'''
def label(text='',name='muted',wrap=False):
    w=QLabel(text);w.setObjectName(name);w.setWordWrap(wrap);w.setTextFormat(Qt.PlainText);return w

class ControlBar(QWidget):
    practice_requested=Signal()
    observe_requested=Signal();snip_requested=Signal();auto_toggled=Signal(bool);quit_requested=Signal()
    volume_changed=Signal(float);mode_changed=Signal(str);speak_toggled=Signal(bool);chat_submitted=Signal(str)
    explain_requested=Signal();trace_requested=Signal();voice_settings_requested=Signal();pause_requested=Signal()
    stop_reply_requested=Signal();system_toggled=Signal(bool);mic_toggled=Signal(bool)
    local_test_requested=Signal();cloud_test_requested=Signal();reply_displayed=Signal(str)
    translation_requested=Signal();translation_settings_requested=Signal();lifecycle_changed=Signal(str)
    def __init__(self, *, text_only=False):
        self.text_only=text_only
        super().__init__();self.setObjectName('workspace');self.setWindowTitle('Ativia');self.setMinimumSize(580,440);self.resize(760,600)
        self._busy=False;self._background_busy=False;self._auto=False;self._volume=.8;self._mode='entertainment'
        self._speak=True;self._expanded=False;self._has_learning_summary=False;self._lifecycle='RUNNING';self._input_note=''
        self._failure='';self._mic=False;self._system=False;self._mic_preference=True;self._last_reply='';self.close_to_hide=False
        layout=QVBoxLayout(self);layout.setContentsMargins(16,14,16,12);layout.setSpacing(10)
        self.setWindowIcon(character_icon())
        header=QHBoxLayout();avatar=label();avatar.setPixmap(character_icon().pixmap(32,32))
        header.addWidget(avatar);header.addWidget(label('Ativia','wordmark'));header.addStretch()
        self.pause_button=QPushButton('完全暂停');self.more_button=QPushButton('设置')
        self.practice_button=QPushButton('口语练习')
        self.practice_button.clicked.connect(self.practice_requested.emit)
        header.addWidget(self.practice_button);header.addWidget(self.pause_button);header.addWidget(self.more_button);layout.addLayout(header)
        self.status_label=label('',wrap=True);self.status_label.setAccessibleName('助手真实运行状态');layout.addWidget(self.status_label)
        self.extra_panel=QFrame();self.extra_panel.setObjectName('settings');extra=QVBoxLayout(self.extra_panel)
        def row(*widgets):
            h=QHBoxLayout()
            for w in widgets:h.addWidget(w)
            extra.addLayout(h)
        self.voice_settings_button=QPushButton('麦克风与声音');self.trace_button=QPushButton('记录与反馈')
        self.stop_reply_button=QPushButton('停止回答');self.quit_button=QPushButton('退出')
        row(self.voice_settings_button,self.trace_button,self.stop_reply_button,self.quit_button)
        self.entertainment_button=QPushButton('陪伴');self.mode_button=QPushButton('学习');self.auto_button=QPushButton('自动观察');self.system_button=QPushButton('系统音频偏好')
        for w in (self.entertainment_button,self.mode_button,self.auto_button,self.system_button):w.setCheckable(True)
        self.entertainment_button.setChecked(True);self.system_button.setChecked(True)
        row(self.entertainment_button,self.mode_button,self.auto_button,self.system_button)
        self.translation_button=QPushButton('开始实时翻译');self.translation_settings_button=QPushButton('翻译 API 设置')
        self.translation_button.clicked.connect(self.translation_requested.emit)
        self.translation_settings_button.clicked.connect(self.translation_settings_requested.emit)
        self.translation_panel=QWidget();translation_row=QHBoxLayout(self.translation_panel)
        translation_row.setContentsMargins(0,0,0,0)
        translation_row.addWidget(self.translation_button);translation_row.addWidget(self.translation_settings_button)
        self.translation_panel.hide()
        self.preference_note=label('勾选项是保存的偏好；暂停时采集停止，恢复后生效。',wrap=True);extra.addWidget(self.preference_note)
        self.speak_button=QPushButton('朗读：开');self.speak_button.setCheckable(True);self.speak_button.setChecked(True)
        self.volume_down_button=QPushButton('−');self.volume_up_button=QPushButton('+');self.volume_label=label('音量 80%')
        self.volume_slider=QSlider(Qt.Horizontal);self.volume_slider.setRange(0,100);self.volume_slider.setValue(80);self.volume_slider.setAccessibleName('音量')
        row(self.speak_button,self.volume_down_button,self.volume_slider,self.volume_up_button,self.volume_label)
        self.local_test_button=QPushButton('仅本地试听');self.cloud_test_button=QPushButton('当前链路测试…');row(self.local_test_button,self.cloud_test_button)
        self.extra_panel.hide();layout.addWidget(self.extra_panel);layout.addWidget(self.translation_panel)
        self.history=QTextBrowser();self.history.setOpenExternalLinks(False);self.history.setOpenLinks(False)
        self.history.setAccessibleName('连续聊天记录');self.history.setPlaceholderText('输入消息开始聊天。需要查看屏幕时，请点击截图或框选。');layout.addWidget(self.history,1)
        # Hidden compatibility buffers retain existing learning and copy APIs.
        self.learning_text=QPlainTextEdit(self);self.learning_text.hide();self.learning_text.textChanged.connect(self._learning_changed)
        self.learning_title=label('学习回答');self.companion_text=label('');self.learning_panel=QWidget(self);self.learning_panel.hide();self.welcome_panel=QWidget(self);self.welcome_panel.hide()
        utilities=QHBoxLayout();self.copy_button=QPushButton('复制回答');self.explain_button=QPushButton('进一步解释')
        self.copy_button.setEnabled(False);self.explain_button.setEnabled(False)
        utilities.addWidget(self.copy_button);utilities.addWidget(self.explain_button);utilities.addStretch()
        self.voice_label=label('本轮声音：尚未播放');self.voice_label.setAccessibleName('本轮实际声音来源');utilities.addWidget(self.voice_label)
        self.detail_button=QPushButton('详情');utilities.addWidget(self.detail_button);layout.addLayout(utilities)
        self.voice_detail=label('日常引擎支持 GPT-SoVITS 和 OpenAI TTS。',wrap=True);self.voice_detail.setTextInteractionFlags(Qt.TextSelectableByMouse);self.voice_detail.hide();layout.addWidget(self.voice_detail)
        self.progress=QProgressBar();self.progress.setRange(0,1);self.progress.setValue(0);self.progress.setFixedHeight(2);self.progress.setTextVisible(False);layout.addWidget(self.progress)
        composer=QHBoxLayout();self.chat_input=QLineEdit();self.chat_input.setPlaceholderText('消息或指令…');self.chat_input.setAccessibleName('消息或指令')
        self.chat_send_button=QPushButton('发送');self.chat_send_button.setObjectName('primary');self.mic_button=QPushButton('麦克风')
        self.mic_button.setToolTip('切换麦克风偏好；暂停时不会开始采集');self.snip_button=QPushButton('框选');self.snip_button.setToolTip('Alt+Q：框选后选择操作')
        self.observe_button=QPushButton('截图');self.observe_button.setToolTip('发送当前屏幕进行分析（云端请求）')
        composer.addWidget(self.chat_input,1)
        for w in (self.chat_send_button,self.mic_button,self.snip_button,self.observe_button):composer.addWidget(w)
        layout.addLayout(composer)
        for button,slot in ((self.pause_button,self.pause_requested.emit),(self.more_button,self._toggle_more),
            (self.voice_settings_button,self.voice_settings_requested.emit),(self.trace_button,self.trace_requested.emit),
            (self.stop_reply_button,self.stop_reply_requested.emit),(self.quit_button,self.quit_requested.emit),
            (self.entertainment_button,lambda:self._set_mode('entertainment')),(self.mode_button,lambda:self._set_mode('learning')),
            (self.auto_button,self._on_auto_clicked),(self.speak_button,self._on_speak_clicked),
            (self.volume_down_button,self._on_volume_down),(self.volume_up_button,self._on_volume_up),
            (self.local_test_button,self.local_test_requested.emit),(self.cloud_test_button,self.cloud_test_requested.emit),
            (self.copy_button,self._copy_result),(self.explain_button,self.explain_requested.emit),
            (self.detail_button,lambda:self.voice_detail.setVisible(not self.voice_detail.isVisible())),
            (self.chat_send_button,self._on_chat_send),(self.mic_button,lambda:self.mic_toggled.emit(not self._mic_preference)),
            (self.observe_button,self.observe_requested.emit),(self.snip_button,self.snip_requested.emit)):button.clicked.connect(slot)
        self.system_button.toggled.connect(self.system_toggled.emit);self.volume_slider.valueChanged.connect(lambda v:self.set_volume(v/100));self.chat_input.returnPressed.connect(self._on_chat_send)
        self.setStyleSheet(STYLE)
        self._stream_cursor=None
        if text_only:
            self._speak=False;self._mic_preference=False;self.speak_button.setChecked(False)
            for w in (self.speak_button,self.volume_down_button,self.volume_up_button,self.volume_slider,self.volume_label,
                      self.local_test_button,self.cloud_test_button,self.auto_button,self.system_button,self.preference_note):w.hide()
            self.mic_button.setText('麦克风：未启用');self.mic_button.setEnabled(False)
            self.mic_button.setToolTip('仅文字预设不初始化音频。需要语音时，请明确切换设备运行方式。')
            self.voice_settings_button.setText('设备运行方式')
            self.trace_button.setText('资源检查说明')
            self.voice_label.setText('仅文字 · 未启用麦克风')
        self._refresh_status();self._place_center()
    def _place_center(self):
        a=QApplication.primaryScreen().availableGeometry();self.move(a.center().x()-self.width()//2,a.center().y()-self.height()//2)
    def append_message(self,who,text):
        if not text:return
        cursor=self.history.textCursor();cursor.movePosition(QTextCursor.End);color='#9fbff0' if who=='你' else '#e3e8ef' if who=='Ativia' else '#e9aa98'
        cursor.insertHtml(f'<p style="margin-top:14px;color:{color};"><b>{escape(who)}</b></p><p style="margin-top:5px;margin-bottom:14px;">{escape(text).replace(chr(10),"<br>")}</p>')
        self.history.setTextCursor(cursor);self.history.ensureCursorVisible()
        if who in ('Ativia','错误'):self.reply_displayed.emit(text)
    def _learning_changed(self):
        text=self.learning_text.toPlainText()
        if text:self._last_reply=text;self.append_message('Ativia',text);self.copy_button.setEnabled(True)
    def begin_stream(self):
        self._stream_text='';self._last_reply=''
        cursor=QTextCursor(self.history.document());cursor.movePosition(QTextCursor.End)
        cursor.insertHtml('<p style="margin-top:14px;color:#e3e8ef;"><b>Ativia</b></p>')
        cursor.insertBlock();self._stream_cursor=cursor
    def append_stream(self,text):
        if not text or self._stream_cursor is None:return
        scroll=self.history.verticalScrollBar();follow=scroll.value()>=scroll.maximum()-24
        self._stream_cursor.insertText(text)
        self._stream_text+=text;self._last_reply=self._stream_text
        if follow:scroll.setValue(scroll.maximum())
    def finish_stream(self,text=None):
        if self._stream_cursor is not None:self._stream_cursor.insertBlock()
        self._stream_cursor=None
        if text is not None:
            self._last_reply=text;self.copy_button.setEnabled(bool(text))
            if text:self.reply_displayed.emit(text)
    def _toggle_more(self):
        self._expanded=not self._expanded;self.extra_panel.setVisible(self._expanded);self.more_button.setText('收起设置' if self._expanded else '设置')
    def _on_auto_clicked(self):self.set_auto(not self._auto);self.auto_toggled.emit(self._auto)
    def _on_mode_clicked(self):self._set_mode('learning' if self._mode=='entertainment' else 'entertainment')
    def _set_mode(self,mode,*,persist=True):
        if self._busy:return
        if hasattr(self,'companion') and persist:self.companion.set_mode('CLASS' if mode=='learning' else 'NORMAL')
        changed=self._mode!=mode;self._mode=mode;self.mode_button.setChecked(mode=='learning');self.entertainment_button.setChecked(mode=='entertainment')
        self.translation_panel.setVisible(mode=='learning')
        if changed:self.mode_changed.emit(mode)
    def _on_speak_clicked(self):
        self._speak=not self._speak;self.speak_button.setChecked(self._speak);self.speak_button.setText('朗读：开' if self._speak else '朗读：关');self.speak_toggled.emit(self._speak)
    def _on_volume_down(self):self.set_volume(self._volume-VOLUME_STEP)
    def _on_volume_up(self):self.set_volume(self._volume+VOLUME_STEP)
    def _on_chat_send(self):
        text=self.chat_input.text().strip()
        if not text or self._lifecycle!='RUNNING':return
        self.chat_input.clear();self.append_message('你',text);self.chat_submitted.emit(text)
    def _copy_result(self):
        if self._last_reply:QApplication.clipboard().setText(self._last_reply)
    def set_volume(self,value):
        self._volume=max(0.,min(1.,float(value)));self.volume_label.setText(f'音量 {round(self._volume*100)}%')
        self.volume_slider.blockSignals(True);self.volume_slider.setValue(round(self._volume*100));self.volume_slider.blockSignals(False)
        self.volume_down_button.setEnabled(self._volume>0);self.volume_up_button.setEnabled(self._volume<1);self.volume_changed.emit(self._volume)
    def set_auto(self,on):self._auto=on;self.auto_button.setChecked(on);self._refresh_status()
    def set_busy(self,busy,background=False):
        self._busy=busy;self._background_busy=busy and background
        if busy:self._failure=''
        self._refresh_status()
    def set_lifecycle(self,state,note=''):
        changed=self._lifecycle!=state;self._lifecycle=state;self._input_note=note;self._refresh_status()
        if changed:self.lifecycle_changed.emit(state)
    def show_snip_notice(self,message):
        self.showNormal();self.raise_();self.activateWindow();self.set_failure(message)
    def set_hotkey_status(self,available,message):
        if available:
            self.snip_button.setToolTip(message or 'Alt+Q：框选后在桌宠对话框显示解释')
            if self._failure==getattr(self,'_hotkey_error',None):self._failure='';self._refresh_status()
            self._hotkey_error=''
        else:
            self._hotkey_error=message;self.snip_button.setToolTip(message);self.show_snip_notice(message)
    def set_capture_state(self,mic,system,mic_preference,system_preference):
        self._mic=mic;self._system=system;self._mic_preference=mic_preference
        self.system_button.blockSignals(True);self.system_button.setChecked(system_preference);self.system_button.blockSignals(False)
        self.system_button.setText('系统音频偏好：'+('开' if system_preference else '关'));self.mic_button.setText('麦克风：'+('收音中' if mic else '已停'))
        self.mic_button.setToolTip('保存偏好：'+('开' if mic_preference else '关')+'；点击改为'+('关' if mic_preference else '开')+'。暂停时仅保存，恢复后生效。')
    def set_voice_state(self,summary,detail):self.voice_label.setText(summary);self.voice_detail.setText(detail)
    def set_failure(self,message):self._failure=message;self.append_message('错误',message);self._refresh_status()
    def set_learning_result(self,summary,explanation=None):
        self._has_learning_summary=True;self.learning_title.setText('进一步解释' if explanation else '学习回答')
        self.learning_text.blockSignals(True)
        self.learning_text.setPlainText(summary+'\n\n进一步解释\n'+explanation if explanation else summary)
        self.learning_text.blockSignals(False)
        self._last_reply=explanation or summary;self.append_message('Ativia',self._last_reply);self.copy_button.setEnabled(True)
        self.explain_button.setEnabled(not self._busy and self._lifecycle=='RUNNING')
    def clear_learning_result(self):self._has_learning_summary=False;self.learning_text.clear();self.explain_button.setEnabled(False)
    def set_entertainment_result(self,text):self.companion_text.setText(text);self._last_reply=text;self.copy_button.setEnabled(bool(text));self.append_message('Ativia',text)
    def current_mode(self):return self._mode
    def tts_allowed(self):
        if getattr(self,'_practice_active',False):return False
        if self.text_only or self._mode=='learning':return False
        companion=getattr(self,'companion',None)
        if companion:
            from personality.engine import MODES
            return MODES[companion.mode].tts
        return True
    def speak_enabled(self):return self._speak and self.tts_allowed()
    def _refresh_status(self):
        running=self._lifecycle=='RUNNING';self.pause_button.setText('恢复' if self._lifecycle=='PAUSED' else '重试暂停' if self._lifecycle=='FAILED' else '完全暂停')
        self.pause_button.setEnabled(self._lifecycle not in ('PAUSING','RESUMING'))
        for w in (self.observe_button,self.snip_button):w.setEnabled(running and (not self._busy or self._background_busy))
        for w in (self.chat_send_button,self.chat_input):w.setEnabled(running)
        for w in (self.mode_button,self.entertainment_button):w.setEnabled(not self._busy)
        self.explain_button.setEnabled(running and not self._busy and self._has_learning_summary);self.stop_reply_button.setEnabled(running);self.auto_button.setEnabled(running)
        self.local_test_button.setEnabled(running and not self._busy and not self.text_only);self.cloud_test_button.setEnabled(running and not self._busy and not self.text_only);self.progress.setRange(0,0 if self._busy and running else 1)
        if self._lifecycle=='PAUSED':text='已暂停 · 麦克风和系统音频已停止'
        elif not running:text={'PAUSING':'正在暂停 · 等待资源释放','RESUMING':'正在恢复','FAILED':'停止失败 · 新任务已禁止'}.get(self._lifecycle,'状态未知')
        elif self._failure:text='失败：'+self._failure
        else:text=('正在处理' if self._busy else '待机')+' · 麦克风'+('收音中' if self._mic else '已停')+' · 系统音频'+('采集中' if self._system else '已停')
        if self.text_only:
            self.mic_button.setEnabled(False);self.auto_button.setEnabled(False);self.system_button.setEnabled(False)
            if running:text=('失败：'+self._failure if self._failure else '正在回复' if self._busy else '就绪')+' · 仅文字 · 未启用麦克风'
            elif self._lifecycle=='PAUSED':text='已完全暂停 · 仅文字 · 未启用麦克风'
        self.translation_button.setEnabled(running)
        if running and getattr(self,'_translation_active',False):text='实时翻译已开启 · '+getattr(self,'_translation_source','字幕来源')
        if self.text_only:self.mic_button.setText('麦克风：翻译收音' if getattr(self,'_translation_mic',False) else '麦克风：未启用')
        self.practice_button.setEnabled(running)
        if running and getattr(self,'_practice_active',False):
            text='口语练习 · '+('正在听你说' if getattr(self,'_practice_mic',False) else '参考回答仅显示字幕')
            for w in (self.observe_button,self.snip_button,self.chat_send_button,self.chat_input,
                      self.translation_button,self.auto_button,self.system_button,self.mic_button,
                      self.voice_settings_button,self.local_test_button,self.cloud_test_button):w.setEnabled(False)
        else:
            self.voice_settings_button.setEnabled(running)
            self.system_button.setEnabled(running and not self.text_only)
            self.mic_button.setEnabled(running and not self.text_only)
        self.status_label.setText(text);self.status_label.setToolTip(self._input_note)
    def closeEvent(self,event):
        if self.close_to_hide:event.ignore();self.hide()
        else:super().closeEvent(event)
