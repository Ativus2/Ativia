"""Offline regression checks; no microphone, screen capture or API calls."""
import base64
import os
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import overlay
import vision
from runtime_settings import RuntimeSettings
from region_select import LEARNING_ACTIONS


SUMMARY = (
    "这页介绍二次函数的图像与性质。函数的图像是一条抛物线，二次项系数决定开口方向和宽窄，"
    "顶点表示函数的最大值或最小值，对称轴则帮助判断增减区间。解题时可以先把一般式化为顶点式，"
    "再确定顶点、对称轴和与坐标轴的交点。学习时建议把公式与草图对照，尤其注意开口方向对最值和单调性的影响。"
)
DETAIL = "先观察抛物线的开口方向，再找顶点。例如 y=x² 的顶点是原点，最小值为零。"


class LearningFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.patches = []
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        isolation=patch('companion_runtime.data_directory',return_value=Path(self.directory.name))
        self.patches.append(isolation);isolation.start()

        def mock_at(name, **kwargs):
            p = patch(name, **kwargs)
            self.patches.append(p)
            return p.start()

        mock_at("overlay.SettingsStore.load", return_value=RuntimeSettings())
        mock_at("overlay.SettingsStore.save")
        self.mic = mock_at("overlay.MicRecorder").return_value
        self.mic.sample_rate = 16000
        self.mic.snapshot.return_value = np.zeros(0, dtype=np.float32)
        self.system = mock_at("overlay.SystemAudioRecorder").return_value
        self.system.snapshot.return_value = np.zeros(0, dtype=np.float32)
        self.player = mock_at("overlay.SpeechPlayer").return_value
        self.player.is_busy.return_value = False
        mock_at("overlay.OverlayApp._setup_hotkey")
        self.capture = mock_at("overlay.capture_primary_jpeg", return_value=b"screen-A")
        self.region_capture = mock_at("overlay.capture_region_jpeg", return_value=b"region-A")
        mock_at("overlay.screenshot_hash", return_value=42)
        self.ask = mock_at("overlay.comment_on_screenshot", side_effect=self.answer)
        self.app = overlay.OverlayApp()
        selected = patch.object(self.app.region_overlay, "selected_jpeg", return_value=b"region-A")
        self.patches.append(selected)
        self.selected_capture = selected.start()
        self.app.control._on_mode_clicked()
        self.app.control._on_speak_clicked()

    @staticmethod
    def answer(*args, **kwargs):
        return DETAIL if kwargs.get("previous_summary") else SUMMARY

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.app._busy() and time.monotonic() < deadline:
            self.qt.processEvents();time.sleep(.01)
        self.assertFalse(self.app._busy(), "worker did not return to idle")

    def tearDown(self):
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
        for p in reversed(self.patches):
            p.stop()

    def capture_summary(self):
        self.app.control.observe_button.click()
        self.wait_idle()

    def test_summary_then_explanation_reuses_image_and_context(self):
        self.assertFalse(self.app.control.explain_button.isEnabled())
        self.capture_summary()
        self.assertEqual(self.app.control.learning_text.toPlainText(), SUMMARY)
        self.assertTrue(self.app.control.explain_button.isEnabled())
        self.capture.return_value = b"different-screen"
        self.app._auto = True
        self.app.control.set_auto(True)
        self.app._timer.start()
        self.app.control.explain_button.click()
        self.assertFalse(self.app.control.explain_button.isEnabled())
        self.wait_idle()
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.ask.call_args.args[0], b"screen-A")
        self.assertEqual(self.ask.call_args.kwargs["previous_summary"], SUMMARY)
        self.assertIn(DETAIL, self.app.control.learning_text.toPlainText())
        self.assertIn(SUMMARY, self.app.control.learning_text.toPlainText())
        self.assertFalse(self.app._auto)
        self.assertFalse(self.app._timer.isActive())

    def test_unchanged_learning_screenshot_still_gets_summary(self):
        self.capture_summary()
        self.capture_summary()
        self.assertEqual(self.ask.call_count, 2)

    def test_duplicate_capture_is_blocked_during_delay(self):
        self.app._start_observe()
        self.app._start_observe()
        self.wait_idle()
        self.assertEqual(self.capture.call_count, 1)

    def test_region_summary_and_explanation(self):
        command = dict(LEARNING_ACTIONS)["帮我做这道题"]
        bilingual = (
            "作答（韩语）\n정답은 4입니다. 양변에 2를 더하면 2x=8이므로 x=4입니다."
            "\n中文翻译\n答案是4。等式两边加2得到2x=8，所以x=4。"
            "\n\n作答（英语）\nThe answer is 4. Add 2 to both sides to get 2x=8, then divide by 2."
            "\n中文翻译\n答案是4。等式两边加2得到2x=8，再除以2。"
        )
        self.ask.side_effect = lambda *args, **kwargs: DETAIL if kwargs.get("previous_summary") else bilingual
        self.app._selecting = True
        self.app._on_region_selected(QRect(10, 20, 100, 80))
        self.assertTrue(self.app._busy())
        self.app.action_menu._choose(command)
        self.wait_idle()
        self.assertEqual(self.ask.call_args.kwargs["user_command"], command)
        self.assertEqual(self.app.control.learning_text.toPlainText(), bilingual)
        self.app.control.explain_button.click()
        self.wait_idle()
        self.selected_capture.assert_called_once_with(QRect(10, 20, 100, 80))
        self.region_capture.assert_not_called()
        self.capture.assert_not_called()
        self.assertEqual(self.ask.call_args.args[0], b"region-A")
        self.assertEqual(self.ask.call_args.kwargs["user_command"], command)
        self.assertEqual(self.ask.call_args.kwargs["previous_summary"], bilingual)
        self.assertIn(bilingual, self.app.control.learning_text.toPlainText())

    def test_failed_new_capture_cannot_explain_old_summary(self):
        self.capture_summary()
        self.ask.side_effect = vision.ScreenshotAnalysisError("网络错误")
        self.capture_summary()
        self.assertFalse(self.app.control.explain_button.isEnabled())
        self.assertIsNone(self.app._learning_image)
        self.assertIn("网络错误", self.app.control.learning_text.toPlainText())

    def test_explanation_failure_keeps_context_for_retry(self):
        self.capture_summary()
        self.ask.side_effect = vision.ScreenshotAnalysisError("网络错误")
        self.app.control.explain_button.click()
        self.wait_idle()
        self.assertTrue(self.app.control.explain_button.isEnabled())
        self.assertEqual(self.app.control.learning_text.toPlainText(), SUMMARY)
        self.ask.side_effect = self.answer
        self.app.control.explain_button.click()
        self.wait_idle()
        self.assertIn(DETAIL, self.app.control.learning_text.toPlainText())

    def test_capture_failure_restores_controls(self):
        self.capture.side_effect = RuntimeError("capture unavailable")
        self.capture_summary()
        self.assertTrue(self.app.control.observe_button.isEnabled())
        self.assertFalse(self.app.control.explain_button.isEnabled())
        self.ask.assert_not_called()

    def test_entertainment_keeps_duplicate_skip_and_hides_panel(self):
        self.app.control._on_mode_clicked()
        self.app._auto = True
        with patch("overlay.monotonic", return_value=100) as clock:
            self.app._start_auto_observe()
            self.wait_idle()
            clock.return_value = 170
            self.app._start_auto_observe()
            self.wait_idle()
        self.assertEqual(self.ask.call_count, 1)
        self.assertTrue(self.app.control.learning_panel.isHidden())


class VisionRequestTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"})
    @patch("vision.OpenAI")
    def test_explanation_request_includes_original_image_and_summary(self, client):
        create = client.return_value.chat.completions.create
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=SUMMARY))])
        vision.comment_on_screenshot(b"original", mode="learning")
        summary_request = create.call_args.kwargs
        vision.comment_on_screenshot(b"original", mode="learning", previous_summary=SUMMARY)
        detail_request = create.call_args.kwargs
        self.assertGreater(detail_request["max_completion_tokens"], summary_request["max_completion_tokens"])
        self.assertIn("100～200", summary_request["messages"][0]["content"])
        content = detail_request["messages"][1]["content"]
        self.assertIn(SUMMARY, content[0]["text"])
        self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64," + base64.b64encode(b"original").decode())

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"})
    @patch("vision.OpenAI")
    def test_empty_response_is_failure(self, client):
        client.return_value.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )
        with self.assertRaises(vision.ScreenshotAnalysisError):
            vision.comment_on_screenshot(b"image", mode="learning")


if __name__ == "__main__":
    unittest.main()
