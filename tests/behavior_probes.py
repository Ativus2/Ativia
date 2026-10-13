"""Fixed offline behavior probes, including not-yet-supported acceptance criteria.

Synthetic screenshots and scripted model output do NOT evaluate real AI accuracy.
Run through tools/evaluate_iterations.py; pending probes are not regression tests.
"""
import io
import os
import time
import unittest
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch, Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PIL import Image
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import overlay
from runtime_settings import RuntimeSettings


def jpeg(color):
    stream = io.BytesIO()
    Image.new("RGB", (100, 80), color).save(stream, "JPEG")
    return stream.getvalue()


class AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory=self.stack.enter_context(tempfile.TemporaryDirectory())
        self.mock("companion_runtime.data_directory",return_value=Path(directory))
        self.mock("screen_events.foreground_application",return_value=dict(application="test.exe",window=123,owned=False))
        self.mock("overlay.SettingsStore.load", return_value=RuntimeSettings())
        self.mock("overlay.SettingsStore.save")
        self.mic = self.mock("overlay.MicRecorder").return_value
        self.mic.sample_rate = 16000
        self.mic.snapshot.return_value = np.zeros(0, dtype=np.float32)
        self.system = self.mock("overlay.SystemAudioRecorder").return_value
        self.system.sample_rate = 16000
        self.system.snapshot.return_value = np.zeros(0, dtype=np.float32)
        self.player = self.mock("overlay.SpeechPlayer").return_value
        self.player.is_busy.return_value = False
        self.mock("overlay.OverlayApp._setup_hotkey")
        self.capture = self.mock("overlay.capture_primary_jpeg", return_value=jpeg("red"))
        self.live_region = self.mock("overlay.capture_region_jpeg", return_value=jpeg("red"), create=True)
        self.image_hash = self.mock("overlay.screenshot_hash", return_value=42)
        self.clock = self.mock("overlay.monotonic", return_value=100.0, create=True)
        self.ask = self.mock("overlay.comment_on_screenshot", return_value="这一步已经完成了。")
        self.tts = self.mock("overlay.synthesize_speech", return_value="")
        self.app = overlay.OverlayApp()
        self.app.control._on_speak_clicked()
        self.show_text = Mock()
        self.app.control.reply_displayed.connect(self.show_text)

    def mock(self, target, **kwargs):
        return self.stack.enter_context(patch(target, **kwargs))

    def wait_idle(self):
        until = time.monotonic() + 5
        while self.app._busy() and time.monotonic() < until:
            self.qt.processEvents();time.sleep(.01)
        self.assertFalse(self.app._busy(), "request stuck busy")

    def manual(self):
        self.app.control.observe_button.click()
        self.wait_idle()

    def automatic(self, at):
        self.clock.return_value = at
        self.app._auto = True
        entry = getattr(self.app, "_start_auto_observe", None)
        if entry is None:
            self.app._start_observe(force=False)
        else:
            entry()
        self.wait_idle()

    def region(self, change_after_selection=False):
        self.app.control._on_mode_clicked()
        self.app.region_overlay.setGeometry(QRect(-40, 0, 100, 80))
        pixmap = QPixmap(200, 160)  # 2x pixels, negative virtual-desktop origin.
        pixmap.fill(QColor("red"))
        self.app.region_overlay._bg = pixmap
        self.app._selecting = True
        self.app._on_region_selected(QRect(-30, 10, 30, 20))
        if change_after_selection:
            self.live_region.return_value = jpeg("blue")
        self.app.action_menu._choose("解释所选报错")
        self.wait_idle()
        return Image.open(io.BytesIO(self.ask.call_args.args[0]))

    def tearDown(self):
        self.app._voice_timer.stop();self.app._scheduler_timer.stop()
        workers=[self.app._worker,self.app._voice_worker,self.app._capture_probe,*self.app._retired_workers,*self.app._retired_voice]
        for worker in workers:
            if worker is not None:
                worker.requestInterruption()
                worker.wait(5000)
        self.qt.processEvents()
        self.app.companion.close()
        self.app.companion.memory.close_future.result(3)
        self.app._timer.stop()
        if self.app._worker is not None:
            self.app._worker.wait(5000)
        self.qt.processEvents()
        for widget in (self.app.control, self.app.translation.window, self.app.region_overlay, self.app.action_menu):
            widget.close()
            widget.deleteLater()
        self.qt.processEvents()


class BehaviorProbes(AppCase):
    def test_static_region(self):
        result = self.region()
        r, _, b = result.getpixel((0, 0))
        self.assertGreater(r, b + 150)

    def test_frozen_error_region(self):
        # Transient error disappears / video advances before choosing an action.
        result = self.region(change_after_selection=True)
        r, _, b = result.getpixel((0, 0))
        self.assertGreater(r, b + 150, "submitted a later screen, not the selected frame")
        self.assertEqual(result.size, (60, 40))
        self.live_region.assert_not_called()

    def test_manual_same_screen(self):
        # User explicitly asks again while coding or reading dense text.
        self.manual()
        self.manual()
        self.assertEqual(self.ask.call_count, 2)

    def test_rapid_switch_cooldown(self):
        for index, at in enumerate((100, 112, 124, 136, 148)):
            self.image_hash.return_value = (1 << (index * 10 + 10)) - 1
            self.automatic(at)
        self.assertEqual(self.ask.call_count, 1)
        self.assertEqual(self.capture.call_count, 1)

    def test_video_audio_same_screen(self):
        self.system.snapshot.return_value = np.full(16000, 0.1, dtype=np.float32)
        self.mock("overlay.transcribe_speech", return_value="视频里的旁白")
        self.automatic(100)
        self.automatic(170)
        self.assertEqual(self.ask.call_count, 1)

    def test_learning_auto_same_screen(self):
        self.app.control._set_mode("learning")
        self.automatic(100);self.automatic(170)
        self.ask.assert_not_called();self.tts.assert_not_called()


    def test_focus_can_stay_silent(self):
        # Scripted model says no useful intervention (coding/chat/dense/blank/game).
        self.ask.return_value = ""
        self.automatic(100)
        self.show_text.assert_not_called()

    def test_ignored_duplicate_not_repeated(self):
        self.app.control._on_speak_clicked()
        self.automatic(100)
        self.image_hash.return_value = (1 << 64) - 1
        self.automatic(170)
        self.assertEqual(self.show_text.call_count, 1)
        self.assertEqual(self.tts.call_count, 1)

    def test_explanation_keeps_original(self):
        self.app.control._on_mode_clicked()
        self.manual()
        self.capture.return_value = jpeg("blue")
        self.app.control.explain_button.click()
        self.wait_idle()
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.ask.call_args.args[0], jpeg("red"))
        self.assertEqual(self.ask.call_args.kwargs["previous_summary"], "这一步已经完成了。")

    def test_recent_context_in_request(self):
        self.manual()
        with patch('voice_pipeline.respond_to_voice',return_value='接上刚才那一句。') as reply:
            self.app._on_chat_submitted('刚才说了什么？');self.wait_idle()
        self.assertIn('这一步已经完成了',reply.call_args.args[1])


    def test_context_bounds(self):
        for index in range(5):
            self.clock.return_value = 100 + index
            self.ask.return_value = f"记录{index}:" + "内容" * 200
            self.manual()
        recent = self.ask.call_args.kwargs.get("recent_context", "")
        self.assertIn("记录3", recent)
        self.assertNotIn("记录0", recent)
        self.assertLessEqual(len(recent), 1200)
        self.clock.return_value = 1000
        self.manual()
        self.assertFalse(self.ask.call_args.kwargs.get("recent_context"))
        self.app.control._on_mode_clicked()
        self.manual()
        self.assertFalse(self.ask.call_args.kwargs.get("recent_context"))

    def test_pause_cancels_pending_reply(self):
        self.app._auto = True
        entry = getattr(self.app, "_start_auto_observe", self.app._start_observe)
        entry()
        self.app._on_auto_toggled(False)
        self.wait_idle()
        self.show_text.assert_not_called()

    def test_busy_blocks_duplicate(self):
        self.app._start_observe()
        self.app._start_observe()
        self.wait_idle()
        self.assertEqual(self.capture.call_count, 1)

    def test_idle_repeated_screen_no_call(self):
        for at in (100, 170, 240):
            self.automatic(at)
        self.assertEqual(self.ask.call_count, 1)

    def test_silent_no_tts(self):
        self.app.control._on_speak_clicked()
        self.ask.return_value = ""
        self.automatic(100)
        self.tts.assert_not_called()

    def test_snip_start_failure_recovers(self):
        self.stack.enter_context(patch.object(self.app.region_overlay, "begin", side_effect=RuntimeError("capture failed")))
        self.app._on_snip()
        QTest.qWait(120)
        self.assertFalse(self.app._busy())
        self.assertTrue(self.app.control.isVisible())
        self.assertTrue(self.app.control.snip_button.isEnabled())

    def test_capture_failure_recovers(self):
        self.capture.side_effect = RuntimeError("capture failed")
        self.manual()
        self.assertTrue(self.app.control.observe_button.isEnabled())
        self.ask.assert_not_called()
