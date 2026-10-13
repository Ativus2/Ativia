"""A persistent, scrollable practice caption window, separate from ordinary chat."""
from dataclasses import replace
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget)
from .core import LANGUAGES, LEVELS


class PracticeWindow(QWidget):
    closed = Signal()

    def __init__(self, settings):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Ativia · 外语口语陪练')
        self.resize(820, 800)
        self.setMinimumSize(620, 600)
        self.setStyleSheet('''
            QWidget { background:#202831; color:#ecf0f6; font:14px "Microsoft YaHei"; }
            QLineEdit,QPlainTextEdit,QTextBrowser,QComboBox {background:#151e27;border:1px solid #475866;border-radius:6px;padding:7px;}
            QPushButton {background:#344b60;border:1px solid #526f86;border-radius:6px;padding:8px;}
            QPushButton:hover {background:#43627e;}
            QPushButton:disabled {color:#788897;background:#26333e;}
            QLabel {background:transparent;}
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel('外语口语陪练')
        title.setStyleSheet('font-size:22px;font-weight:600;')
        layout.addWidget(title)
        note = QLabel('Ativia提问，你来回答。参考字幕可以直接念，也可以用自己的说法。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.options = QWidget()
        form = QFormLayout(self.options)
        form.setContentsMargins(0, 0, 0, 0)
        self.language = QComboBox()
        for code, label in LANGUAGES.items():
            self.language.addItem(label, code)
        self.language.setCurrentIndex(self.language.findData(settings.language))
        self.level = QComboBox()
        self.level.addItems(LEVELS)
        self.level.setCurrentText(settings.level)
        choices = QHBoxLayout()
        choices.addWidget(self.language)
        choices.addWidget(self.level)
        form.addRow('练习语言 / 难度', choices)
        self.topic = QLineEdit(settings.topic)
        self.topic.setMaxLength(500)
        form.addRow('今天聊什么', self.topic)
        self.profile = QPlainTextEdit(settings.learner_profile)
        self.profile.setPlaceholderText('可选：填入真实兴趣、经历或想表达的内容，参考答案会更贴近你。')
        self.profile.setMaximumHeight(62)
        form.addRow('关于我', self.profile)
        layout.addWidget(self.options)
        row = QHBoxLayout()
        self.start_button = QPushButton('开始新练习')
        self.stop_button = QPushButton('结束练习')
        self.settings_button = QPushButton('练习设置')
        self.progress = QLabel('00:00 · 已回答0题')
        for widget in (self.start_button, self.stop_button, self.settings_button, self.progress):
            row.addWidget(widget)
        layout.addLayout(row)
        self.status = QLabel('准备好后点击“开始新练习”；只在点击“开始说话”后使用麦克风。')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.captions = QTextBrowser()
        self.captions.setOpenExternalLinks(False)
        self.captions.setOpenLinks(False)
        self.captions.setAccessibleName('题目及不朗读的参考回答字幕')
        layout.addWidget(self.captions, 1)
        support = QHBoxLayout()
        self.listen_button = QPushButton('听题目')
        self.simplify_button = QPushButton('换个简单说法')
        self.chinese = QCheckBox('显示中文释义')
        self.chinese.setChecked(settings.show_chinese)
        for widget in (self.listen_button, self.simplify_button, self.chinese):
            support.addWidget(widget)
        layout.addLayout(support)
        layout.addWidget(QLabel('我实际说的回答（识别后可修改，也可直接输入）'))
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText('按“开始说话”练口语，结束后核对识别结果，再提交进入下一问。')
        self.answer.setMaximumHeight(92)
        layout.addWidget(self.answer)
        actions = QHBoxLayout()
        self.record_button = QPushButton('开始说话')
        self.submit_button = QPushButton('提交回答 / 下一问')
        actions.addWidget(self.record_button)
        actions.addWidget(self.submit_button)
        layout.addLayout(actions)
        self.set_controls(False, False, False, False, True)

    def read_settings(self, current):
        return replace(current, language=self.language.currentData(), level=self.level.currentText(),
                       topic=self.topic.text().strip(), learner_profile=self.profile.toPlainText().strip(),
                       show_chinese=self.chinese.isChecked())

    def display(self, turn):
        def paragraph(text, color, size):
            return f'<p style="color:{color};font-size:{size}px;white-space:pre-wrap;">{escape(text).replace(chr(10), "<br>")}</p>'
        html = '<p style="color:#83b7e3;">Ativia · 本轮问题</p>'
        html += paragraph(turn.question, '#eef4ff', 23)
        if self.chinese.isChecked():
            html += paragraph(turn.question_zh, '#aebecb', 15)
        html += '<hr><p style="color:#9bd8c7;">你可以这样回答 · 仅字幕，不朗读 · 示例可按实际情况调整</p>'
        html += paragraph(turn.suggested_answer, '#e4fff5', 25)
        if self.chinese.isChecked():
            html += paragraph(turn.answer_zh, '#aebecb', 15)
        if turn.feedback:
            html += '<hr><p style="color:#e7c991;">上一轮的小建议</p>'
            html += paragraph(turn.feedback, '#dfd7c9', 15)
        self.captions.setHtml(html)

    def set_controls(self, active, busy, recording, has_turn, allowed, engaged=False):
        self.start_button.setEnabled(allowed and not engaged and not busy)
        self.stop_button.setEnabled(engaged or active or busy)
        self.options.setEnabled(not engaged and not busy)
        self.options.setVisible(not engaged)
        self.settings_button.setEnabled(not engaged and not busy)
        ready = allowed and active and has_turn and not busy
        self.listen_button.setEnabled(ready)
        self.simplify_button.setEnabled(ready)
        self.answer.setEnabled(ready)
        self.submit_button.setEnabled(ready)
        self.record_button.setEnabled(ready or recording)
        self.record_button.setText('说完了' if recording else '开始说话')

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class PracticeSettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle('口语练习设置')
        self.resize(620, 380)
        layout = QFormLayout(self)
        note = QLabel('文字生成沿用主助手的API。语音地址和密钥都留空时也沿用主助手；\n若服务不支持语音，可单独配置。录音与对话只留在本次会话内存中。')
        note.setWordWrap(True)
        layout.addRow(note)
        self.fields = {}
        for name, label in (('model', '文字模型（留空沿用主助手）'), ('audio_model', '语音识别模型'),
                            ('speech_model', '题目朗读模型'), ('audio_base_url', '独立语音API地址'),
                            ('audio_api_key', '独立语音API密钥')):
            field = QLineEdit(getattr(settings, name))
            if name == 'audio_api_key':
                field.setEchoMode(QLineEdit.Password)
            self.fields[name] = field
            layout.addRow(label, field)
        self.auto_audio = QCheckBox('自动朗读新题目（AI合成语音，始终不读参考回答）')
        self.auto_audio.setChecked(settings.auto_question_audio)
        layout.addRow(self.auto_audio)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addRow(self.error)
        save = QPushButton('保存')
        save.clicked.connect(self.accept)
        layout.addRow(save)

    def updated(self, current):
        return replace(current, **{name: widget.text().strip() for name, widget in self.fields.items()},
                       auto_question_audio=self.auto_audio.isChecked())
