"""Device calibration and real TTS controls; no simulated pitch slider."""
from dataclasses import asdict
from PySide6.QtCore import Signal, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QScrollArea, QWidget, QPlainTextEdit)
from runtime_settings import RuntimeSettings
from local_tts_client import voice_choices
from gpu_policy import effective_mode, choose_route


class VoiceSettingsDialog(QDialog):
    settings_changed = Signal(object)
    preview_requested = Signal(object)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("麦克风与声音")
        self.resize(590, 660)
        self._base = settings
        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        self.mic_enabled = QCheckBox("自动接收用户语音（无需开启自动观察）")
        self.mic_enabled.setChecked(settings.mic_enabled)
        layout.addWidget(self.mic_enabled)
        form = QFormLayout()
        self.values = {}
        specs = [("麦克风灵敏度", "mic_sensitivity", .5, 4, .1, "×"),
                 ("开始说话阈值（RMS）", "start_threshold", .001, .08, .001, ""),
                 ("停止说话阈值（RMS）", "stop_threshold", .0005, .06, .0005, ""),
                 ("说完后的静音等待", "silence_timeout", .3, 1.5, .1, " 秒"),
                 ("最短有效语音", "min_speech_seconds", .12, .8, .02, " 秒"),
                 ("语速", "voice_speed", .75, 1.35, .05, "×"),
                 ("音量", "voice_volume", 0, 1, .05, "")]
        for title, key, low, high, step, suffix in specs:
            widget = QDoubleSpinBox()
            widget.setDecimals(4 if "threshold" in key else 2)
            widget.setRange(low, high)
            widget.setSingleStep(step)
            widget.setSuffix(suffix)
            widget.setValue(getattr(settings, key))
            self.values[key] = widget
            form.addRow(title, widget)
        self.backend = QComboBox()
        for title, mode in (("Auto · 自动保护", "auto"), ("Local Voice · 优先本地音色", "local"),
                            ("Gaming Mode · 游戏模式", "gaming"), ("OpenAI TTS Only · 仅云端", "openai")):
            self.backend.addItem(title, mode)
        self.backend.setCurrentIndex(self.backend.findData(effective_mode(settings)))
        form.addRow("语音资源模式", self.backend)
        self.gpu_status = QLabel("正在读取 GPU 状态…")
        self.gpu_status.setWordWrap(True)
        form.addRow(self.gpu_status)
        self.games = QPlainTextEdit('\n'.join(settings.game_processes))
        self.games.setMaximumHeight(75)
        self.games.setPlaceholderText('游戏进程名，每行一个，例如 helldivers2.exe')
        form.addRow("游戏进程", self.games)
        advanced_toggle = QCheckBox("展开 GPU 保护参数")
        form.addRow(advanced_toggle)
        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        for title, key, low, high, suffix in (
            ('进入保护的 GPU 占用', 'gpu_enter_percent', 50, 95, ' %'),
            ('恢复本地的 GPU 占用', 'gpu_resume_percent', 10, 80, ' %'),
            ('高负载持续时间', 'gpu_high_seconds', 1, 15, ' 秒'),
            ('恢复前保持空闲', 'gpu_recovery_seconds', 3, 60, ' 秒'),
            ('显存使用保护线', 'gpu_vram_percent', 70, 95, ' %'),
            ('至少保留空闲显存', 'gpu_reserve_mb', 512, 4096, ' MiB')):
            widget = QDoubleSpinBox(); widget.setRange(low, high); widget.setDecimals(0)
            widget.setSuffix(suffix); widget.setValue(getattr(settings, key))
            self.values[key] = widget; advanced_form.addRow(title, widget)
        advanced.setVisible(False)
        advanced_toggle.toggled.connect(advanced.setVisible)
        form.addRow(advanced)
        protection_note = QLabel('Auto 和 Local Voice 都遵守 GPU／游戏保护。保护时直接使用 OpenAI，不先尝试本地推理；游戏模式全程使用 OpenAI。Steam 启动器不算游戏。')
        protection_note.setWordWrap(True)
        form.addRow(protection_note)
        self.local_voice = QComboBox()
        for key, label in voice_choices():
            self.local_voice.addItem(label, key)
        selected = self.local_voice.findData(settings.local_voice)
        if selected < 0:
            self.local_voice.addItem(settings.local_voice, settings.local_voice)
            selected = self.local_voice.count() - 1
        self.local_voice.setCurrentIndex(selected)
        form.addRow("本地音色", self.local_voice)
        self.fallback = QCheckBox("本地合成失败时回退 OpenAI（不影响 GPU 保护主动切换）")
        self.fallback.setChecked(settings.tts_fallback)
        form.addRow(self.fallback)
        self.voice = QComboBox()
        for name in ("nova", "coral", "shimmer", "sage", "marin"):
            self.voice.addItem(name, name)
        self.voice.setCurrentIndex(self.voice.findData(settings.voice_name))
        form.addRow("原有／备用音色", self.voice)
        self.tone = QComboBox()
        for title, value in (("原有风格", "original"), ("年轻、轻快（成人声线）", "light"), ("轻柔、放松", "soft")):
            self.tone.addItem(title, value)
        self.tone.setCurrentIndex(self.tone.findData(settings.voice_tone))
        form.addRow("声音风格 / Tone", self.tone)
        self.speech_quirk = QCheckBox("中文句尾口癖（喵）")
        self.speech_quirk.setChecked(settings.speech_quirk_enabled)
        form.addRow(self.speech_quirk)
        self.quirk_probability = QDoubleSpinBox()
        self.quirk_probability.setRange(0, 100)
        self.quirk_probability.setDecimals(1)
        self.quirk_probability.setSingleStep(5)
        self.quirk_probability.setSuffix(" %")
        self.quirk_probability.setValue(settings.speech_quirk_probability * 100)
        self.quirk_probability.setEnabled(settings.speech_quirk_enabled)
        self.speech_quirk.toggled.connect(self.quirk_probability.setEnabled)
        form.addRow("口癖触发概率（每句）", self.quirk_probability)
        quirk_note = QLabel("仅影响朗读。学习模式、识别到的严肃回答，以及含代码、链接、路径或结构化内容的回复会跳过。")
        quirk_note.setWordWrap(True)
        form.addRow(quirk_note)
        self.backend.currentIndexChanged.connect(self._update_backend_controls)
        self.fallback.toggled.connect(self._update_backend_controls)
        self._update_backend_controls()
        self.expertise = QComboBox()
        for title, value in (("未设置（不默认教学）", "unknown"), ("初学者", "beginner"), ("熟练", "experienced"), ("精通", "expert")):
            self.expertise.addItem(title, value)
        self.expertise.setCurrentIndex(self.expertise.findData(settings.user_expertise))
        form.addRow("游戏熟悉程度", self.expertise)
        layout.addLayout(form)
        self.echo_guard = QCheckBox("播放角色声音时抑制麦克风回声（戴耳机可关闭以便插话）")
        self.echo_guard.setChecked(settings.echo_guard)
        layout.addWidget(self.echo_guard)
        note = QLabel("日常引擎支持本地训练音色；服务缺失时按需启动，冷启动会较慢。原生语音实验使用模型内置声音，不加载此音色。声音风格仅作用于 OpenAI TTS。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.meter = QProgressBar()
        self.meter.setRange(0, 1000)
        self.meter.setFormat("输入音量（已应用灵敏度）")
        layout.addWidget(self.meter)
        self.mic_status = QLabel("点击“测试麦克风”查看实时判断；请先保存参数。测试期间不提交语音请求。")
        self.mic_status.setWordWrap(True)
        layout.addWidget(self.mic_status)
        actions = QHBoxLayout()
        self.test_button = QPushButton("测试麦克风")
        self.test_button.setCheckable(True)
        self.preview_button = QPushButton("试听当前声音")
        self.preview_button.clicked.connect(lambda: self.preview_requested.emit(self.current()))
        self.save_button = QPushButton("保存并应用")
        self.save_button.clicked.connect(lambda: self.settings_changed.emit(self.current()))
        for widget in (self.test_button, self.preview_button, self.save_button):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.latency_label = QLabel("语音路径计时：尚无测量")
        self.latency_label.setWordWrap(True)
        layout.addWidget(self.latency_label)
        self._gpu_timer = QTimer(self)
        self._gpu_timer.setInterval(1000)
        self._gpu_timer.timeout.connect(self._refresh_gpu)
        self._gpu_timer.start()

    def _update_backend_controls(self):
        local = self.backend.currentData() in ("local", "auto")
        self.local_voice.setEnabled(local)
        self.fallback.setEnabled(local)
        self.tone.setEnabled(True)

    def set_input_available(self, available):
        self.test_button.setEnabled(available);self.preview_button.setEnabled(available)
        if not available:
            self.test_button.setChecked(False)
            self.mic_status.setText('已暂停：无采集。可保存偏好，恢复后生效；试听也需先恢复。')

    def _refresh_gpu(self):
        if not self.isVisible():
            return
        route = choose_route(self.current())
        if route.gpu_percent is None:
            status = 'GPU 状态暂不可用；保护期间使用 OpenAI'
        else:
            status = f'GPU {route.gpu_percent:.0f}% · 显存 {route.used_mb/1024:.1f}/{route.total_mb/1024:.1f} GiB'
        target = '本地音色' if route.backend == 'local' else 'OpenAI'
        from daily_voice_status import reason_text
        self.gpu_status.setText(status + '\n准入预览（不是已播放声音）→ ' + target + ' · ' + reason_text(route.reason) + (' · ' + route.game if route.game else ''))

    def current(self):
        data = asdict(self._base)
        data.update({key: widget.value() for key, widget in self.values.items()})
        data.update(mic_enabled=self.mic_enabled.isChecked(), echo_guard=self.echo_guard.isChecked(),
                    voice_name=self.voice.currentData(), voice_tone=self.tone.currentData(),
                    tts_mode=self.backend.currentData(),
                    voice_backend='openai' if self.backend.currentData() == 'openai' else 'local',
                    local_voice=self.local_voice.currentData(), game_processes=self.games.toPlainText(),
                    tts_fallback=self.fallback.isChecked(),
                    speech_quirk_enabled=self.speech_quirk.isChecked(),
                    speech_quirk_probability=self.quirk_probability.value() / 100,
                    user_expertise=self.expertise.currentData())
        return RuntimeSettings.parse(data)

    def update_meter(self, level, threshold, speaking):
        if self.test_button.isChecked():
            self.meter.setValue(min(1000, round(level * 10000)))
            self.mic_status.setText(f"当前 RMS：{level:.4f}　有效开始阈值：{threshold:.4f}　"
                                   + ("正在说话" if speaking else "静音／未触发"))
