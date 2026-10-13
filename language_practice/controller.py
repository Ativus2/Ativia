"""Practice lifecycle, with generation guards and explicit learner turns."""
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
from .core import PracticeSettings, PracticeState, PracticeStore, connection
from .ui import PracticeSettingsDialog, PracticeWindow
from .worker import PracticeWorker


class PracticeController(QObject):
    changed = Signal()

    def __init__(self, owner, store=None, worker_factory=None):
        super().__init__(owner.control)
        self.owner, self.control, self.gate = owner, owner.control, owner._pause_gate
        self.store = store or PracticeStore()
        self.settings = self.store.load()
        self.worker_factory = worker_factory or PracticeWorker
        self.state = PracticeState()
        self.window = None
        self.active = self.engaged = False
        self.workers = {}
        self.token = None
        self.operation = None
        self.recording = False
        self.started_at = None
        self.elapsed = 0
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._tick)
        self.control.practice_requested.connect(self.open)
        self.control.lifecycle_changed.connect(self._lifecycle)
        self.control.mode_changed.connect(self._mode)
        self.control.stop_reply_requested.connect(self.stop)
        QApplication.instance().aboutToQuit.connect(self.stop)

    @property
    def busy(self):
        return self.token is not None

    @property
    def uses_microphone(self):
        return self.recording

    def quiescent(self):
        return not self.workers and not self.engaged

    def open(self):
        if self.window is None:
            self.window = PracticeWindow(self.settings)
            self.window.start_button.clicked.connect(self.start)
            self.window.stop_button.clicked.connect(self.stop)
            self.window.settings_button.clicked.connect(self.open_settings)
            self.window.record_button.clicked.connect(self.record)
            self.window.submit_button.clicked.connect(self.submit)
            self.window.simplify_button.clicked.connect(self.simplify)
            self.window.listen_button.clicked.connect(self.read_question)
            self.window.chinese.toggled.connect(self._redraw)
            self.window.closed.connect(self.stop)
        self._refresh()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _message(self, message):
        if self.window:
            self.window.status.setText(message)

    def start(self):
        if not self.gate.allows() or self.engaged or self.busy:
            return
        try:
            settings = self.window.read_settings(self.settings)
            settings.validate()
            connection(settings)
            self.store.save(settings)
        except (ValueError, OSError) as exc:
            self._message(str(exc) if isinstance(exc, ValueError) else '练习设置未能保存，请检查目录权限。')
            return
        self.settings = settings
        self.owner._stop_current_reply()
        self.owner.translation.stop()
        self.control._set_mode('learning')
        self.engaged = True
        self.control._practice_active = True
        try:
            if hasattr(self.owner, '_hold_for_practice'):
                self.owner._hold_for_practice(True)
        except Exception:
            self._message('无法释放原有音频设备，请完全暂停后恢复，再尝试练习。')
            self.stop()
            return
        self.active = True
        self.state = PracticeState()
        self.started_at, self.elapsed = monotonic(), 0
        self.window.captions.clear()
        self.window.answer.clear()
        self.timer.start()
        self._generate('start')

    def _launch(self, kind, payload, operation=None):
        if not self.active or not self.gate.allows() or self.busy:
            return
        self.token = uuid4().hex
        self.operation = operation or (kind, '')
        worker = self.worker_factory(self.token, kind, self.settings, payload, self)
        self.workers[self.token] = worker
        worker.result.connect(self._result)
        worker.failed.connect(self._failed)
        worker.status.connect(self._status)
        worker.finished.connect(self._finished)
        self.recording = kind == 'record'
        self._refresh()
        worker.start()

    def _generate(self, action, answer=''):
        self._message('正在准备题目和参考字幕…' if action != 'simplify' else '正在换成更简单的说法…')
        self._launch('generate', self.state.messages(self.settings, action, answer), (action, answer))

    def submit(self):
        if not self.active or self.busy or not self.state.current:
            return
        answer = self.window.answer.toPlainText().strip()
        if not answer or len(answer) > 4000:
            self._message('请先说出或输入自己的回答（最多4000字）。参考字幕不会自动提交。')
            return
        self._generate('answer', answer)

    def simplify(self):
        if self.active and not self.busy and self.state.current:
            self._generate('simplify')

    def read_question(self):
        if self.active and not self.busy and self.state.current:
            try:
                connection(self.settings, audio=True)
            except ValueError as exc:
                self._message(str(exc))
                return
            self._message('正在准备题目音频…')
            # This is the only path to speech output; no reference text can enter it.
            self._launch('question_audio', self.state.current.question)

    def record(self):
        if self.recording and self.token in self.workers:
            self.workers[self.token].stop_recording()
            self.recording = False
            self._message('正在结束录音并识别…')
            self._refresh()
            return
        if not self.active or self.busy or not self.state.current:
            return
        if self.owner.translation.worker:
            self._message('正在等待实时翻译释放麦克风，请稍后再点击“开始说话”。')
            return
        try:
            connection(self.settings, audio=True)
        except ValueError as exc:
            self._message(str(exc))
            return
        self._message('正在打开默认麦克风…')
        self._launch('record', None)

    def _valid(self, token):
        return self.active and self.gate.allows() and token == self.token

    def _result(self, token, result):
        if not self._valid(token):
            return
        action, answer = self.operation
        self.token, self.operation, self.recording = None, None, False
        auto_audio = False
        if action in ('start', 'answer', 'simplify'):
            self.state.accept(result, action, answer)
            self._redraw()
            if action != 'simplify':
                self.window.answer.clear()
            auto_audio = self.settings.auto_question_audio and action != 'simplify'
            self._message('轮到你说 · 可以照着参考字幕念，也可以自由回答')
        elif action == 'record':
            # A transcription is still reviewable, not silently treated as submitted speech.
            previous = self.window.answer.toPlainText().strip()
            self.window.answer.setPlainText((previous + '\n' if previous else '') + result)
            self._message('已识别你的回答；核对后点击“提交回答 / 下一问”')
        else:
            self._message('题目朗读结束 · 轮到你说，参考回答只显示字幕')
        self._refresh()
        if auto_audio:
            self.read_question()

    def _failed(self, token, message):
        if self._valid(token):
            self.token, self.operation, self.recording = None, None, False
            self._message(message)
            # A first-question failure must allow starting again, not strand an empty session.
            if not self.state.current:
                self._end()
            self._refresh()

    def _status(self, token, message):
        if self._valid(token):
            if message.startswith('录音已结束'):
                self.recording = False
                self._refresh()
            self._message(message)

    def _finished(self, token):
        worker = self.workers.get(token)
        if worker is None:
            return
        if worker.thread and worker.thread.is_alive():
            QTimer.singleShot(20, lambda: self._finished(token))
            return
        self.workers.pop(token)
        worker.deleteLater()
        if token == self.token:
            self.token, self.operation, self.recording = None, None, False
        self._release_if_idle()
        self._refresh()

    def _end(self):
        if self.started_at is not None:
            self.elapsed = monotonic() - self.started_at
        self.started_at = None
        self.active = self.recording = False
        self.token = self.operation = None
        for worker in tuple(self.workers.values()):
            worker.stop()
        self._release_if_idle()

    def stop(self):
        was_active = self.active or self.engaged
        self._end()
        if was_active:
            self._message('练习已结束 · 参考字幕保留；再次开始会开启新对话')
        self._refresh()

    def _release_if_idle(self):
        if self.active or self.workers or not self.engaged:
            return
        self.engaged = False
        self.control._practice_active = False
        if hasattr(self.owner, '_hold_for_practice'):
            try:
                self.owner._hold_for_practice(False)
            except Exception:
                self.control.set_failure('练习已结束，但原有音频未恢复；请完全暂停后恢复。')
        self.timer.stop()

    def _redraw(self, *_):
        if self.window and self.state.current:
            self.window.display(self.state.current)

    def _tick(self):
        seconds = int(monotonic() - self.started_at if self.started_at is not None else self.elapsed)
        if self.window:
            self.window.progress.setText(f'{seconds // 60:02}:{seconds % 60:02} · 已回答{self.state.answer_count}题')

    def _refresh(self):
        self.control._practice_mic = self.recording
        self.control._refresh_status()
        if self.window:
            self.window.set_controls(self.active, self.busy, self.recording, bool(self.state.current),
                                     self.gate.allows(), self.engaged)
        self._tick()
        self.changed.emit()

    def _lifecycle(self, state):
        if state != 'RUNNING':
            self.stop()
        else:
            self._refresh()

    def _mode(self, mode):
        if mode != 'learning':
            self.stop()

    def open_settings(self):
        if self.engaged or self.busy:
            return
        dialog = PracticeSettingsDialog(self.settings, self.window)
        if dialog.exec() != dialog.Accepted:
            return
        try:
            updated = dialog.updated(self.settings)
            self.store.save(updated)
            self.settings = updated
            self._message('练习设置已保存。')
        except (ValueError, OSError) as exc:
            self._message(str(exc) if isinstance(exc, ValueError) else '设置保存失败，请检查目录权限。')
        finally:
            dialog.deleteLater()
