import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from behavior_probes import AppCase
from character import Observation
from character_trace import TraceCollector, TraceStore
from test_character_trace import model_reply
import vision


def event(**changes):
    values = dict(scene="coding", event="error", subject="NameError: x", evidence="当前终端报错",
                  confidence="high", focused=False, intent="REACT", reply="哦，炸了。")
    values.update(changes)
    return Observation(**values)


class TraceIntegrationTests(AppCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TraceStore(Path(self.tmp.name) / "trace.sqlite3")
        self.app._trace = TraceCollector(self.store)

    def rows(self):
        return [self.store.get(row["id"]) for row in reversed(self.store.recent())]

    def enable(self):
        self.app._trace.set_enabled(True)

    def test_default_off_and_feedback_panel_does_not_enable_collection(self):
        self.app.control.trace_button.click()
        self.assertFalse(self.app._trace.enabled)
        self.manual()
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.ask.call_count, 1)

    def test_candidate_wait_and_display_are_separate_with_continuous_state(self):
        self.enable()
        self.ask.return_value = event()
        self.automatic(100)
        self.image_hash.return_value = (1 << 64) - 1
        self.ask.return_value = event(reply="又是它。")
        self.automatic(170)
        rows = self.rows()
        first, second = [row["trace"] for row in rows]
        self.assertEqual(first["delivery"]["response"], "哦，炸了。")
        self.assertEqual(second["candidate"]["reply"], "又是它。")
        self.assertEqual(second["decision"], "WAIT")
        self.assertEqual(second["delivery"], dict(status="silent", response=""))
        self.assertEqual(second["previous_trace_id"], first["id"])
        self.assertTrue(second["state_before"]["memory"][0]["announced"])
        self.assertTrue(all(row["feedback"] is None for row in rows))
        self.assertEqual(self.ask.call_count, 2)

    def test_persona_gate_is_logged_without_changing_reply(self):
        self.enable()
        self.ask.return_value = event(intent="TEASE")
        self.automatic(100)
        trace = self.rows()[0]["trace"]
        self.assertEqual(trace["gates"], ["persona_boundary"])
        self.assertEqual(trace["accepted_observation"]["intent"], "WAIT")
        self.assertEqual(trace["delivery"]["status"], "silent")

    def test_unchanged_screen_skip_has_no_new_model_call(self):
        self.enable()
        self.automatic(100)
        self.automatic(170)
        trace = self.rows()[1]["trace"]
        self.assertEqual(trace["reason"], "unchanged_screen_without_user_speech")
        self.assertFalse(trace["model_called"])
        self.assertEqual(self.ask.call_count, 1)

    def test_known_format_failure_records_raw_error_and_original_user_input(self):
        self.enable()
        self.ask.side_effect = vision.comment_on_screenshot
        with patch.dict("os.environ", OPENAI_API_KEY="test-secret"), patch("vision.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = model_reply('{"intent":"WAIT"}')
            self.automatic(100)
        trace = self.rows()[0]["trace"]
        self.assertEqual(trace["raw_response"], '{"intent":"WAIT"}')
        self.assertEqual(trace["delivery"]["status"], "error")
        self.assertEqual(trace["error"]["kind"], "analysis_error")
        self.assertEqual(trace["model"]["usage"]["total_tokens"], 58)
        self.assertIsNone(self.rows()[0]["feedback"])
        self.ask.side_effect = vision.ScreenshotAnalysisError("这次没有生成有效内容，请再试一次。")
        with patch("voice_pipeline.respond_to_voice",side_effect=vision.ScreenshotAnalysisError("这次没有生成有效内容，请再试一次。")):
            self.app._on_chat_submitted("暂时别主动说话")
            self.wait_idle()
            import time
            deadline=time.monotonic()+3
            while len(self.rows())<2 and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.01)
        self.assertEqual(self.rows()[1]["trace"]["context"]["user_command"],"暂时别主动说话")

    def test_feedback_pins_selection_while_new_trace_arrives(self):
        self.enable()
        self.manual()
        original = self.rows()[0]["trace"]["id"]
        self.app.control.trace_button.click()
        dialog = self.app._trace_dialog
        dialog.records.setCurrentRow(0)
        self.assertIn("实际结果 · 已发言", dialog.details.toPlainText())
        self.assertIn("这一步已经完成了。", dialog.details.toPlainText())
        self.assertNotIn("production_sha256", dialog.details.toPlainText())
        dialog.raw_view.setChecked(True)
        self.assertIn("production_sha256", dialog.details.toPlainText())
        dialog.raw_view.setChecked(False)
        dialog.rating.setCurrentIndex(dialog.rating.findData("success"))
        dialog.comment.setPlainText("这次合适")
        self.manual()
        self.assertEqual(dialog._selected_id, original)
        self.assertEqual(dialog.comment.toPlainText(), "这次合适")
        before = list(self.app._recent_turns), list(self.app._conversation._reactions)
        dialog.save_button.click()
        self.assertEqual(self.store.get(original)["feedback"]["label"], "success")
        self.assertIsNone(self.rows()[1]["feedback"])
        self.assertEqual(before, (list(self.app._recent_turns), list(self.app._conversation._reactions)))
        self.assertEqual(self.ask.call_count, 2)

    def test_storage_failure_does_not_stick_busy_or_change_display(self):
        self.enable()
        with patch.object(self.store, "add", side_effect=OSError("disk full")):
            self.manual()
        self.assertEqual(self.app.control.companion_text.text(), "这一步已经完成了。")
        self.assertFalse(self.app._busy())
        self.assertEqual(self.app.control.trace_button.text(), "记录异常")

    def test_learning_further_explanation_trace_retains_frozen_context(self):
        self.enable()
        self.app.control._on_mode_clicked()
        self.manual()
        self.app.control.explain_button.click()
        self.wait_idle()
        trace = self.rows()[1]["trace"]
        self.assertEqual(trace["source"], "further_explanation")
        self.assertEqual(trace["context"]["previous_summary"], "这一步已经完成了。")
        self.assertNotIn("image_jpeg", json.dumps(trace))
        self.assertEqual(self.capture.call_count, 1)

    def test_capture_failure_is_unrated_and_does_not_call_model(self):
        self.enable()
        self.capture.side_effect = OSError("screen unavailable")
        self.manual()
        trace = self.rows()[0]
        self.assertEqual(trace["trace"]["reason"], "capture_failed")
        self.assertIsNone(trace["feedback"])
        self.ask.assert_not_called()

    def _delayed_observation(self, during):
        entered, release = threading.Event(), threading.Event()

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return event()

        self.ask.side_effect = delayed
        self.app._auto = True
        self.app._start_auto_observe()
        try:
            import time
            deadline=time.monotonic()+2
            while not entered.is_set() and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.005)
            self.assertTrue(entered.is_set())
            during()
        finally:
            release.set()
        self.wait_idle()

    def test_cancelled_candidate_is_not_displayed_or_added_to_character_memory(self):
        self.enable()
        self._delayed_observation(lambda: self.app._on_auto_toggled(False))
        trace = self.rows()[0]["trace"]
        self.assertEqual(trace["candidate"]["reply"], "哦，炸了。")
        self.assertTrue(trace["interrupted"])
        self.assertEqual(trace["delivery"]["status"], "cancelled")
        self.assertEqual(trace["state_after"]["memory"], [])
        self.show_text.assert_not_called()

    def test_stop_collection_during_request_discards_trace_but_not_reply(self):
        self.enable()
        self._delayed_observation(lambda: self.app._trace.set_enabled(False))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.app.control.companion_text.text(), "哦，炸了。")
        self.assertTrue(self.app._character_memory.entries)

    def test_export_and_clear_ui_are_local_and_preserve_character_state(self):
        self.enable()
        self.manual()
        self.app.control.trace_button.click()
        dialog = self.app._trace_dialog
        dialog.records.setCurrentRow(0)
        dialog.rating.setCurrentIndex(dialog.rating.findData("failure"))
        dialog.save_button.click()
        before = list(self.app._recent_turns), list(self.app._conversation._reactions)
        with patch("trace_feedback.QFileDialog.getExistingDirectory", return_value=self.tmp.name):
            dialog.export_button.click()
        exports = list(Path(self.tmp.name).glob("character-dataset-*"))
        self.assertEqual(len(exports), 1)
        self.assertEqual(len((exports[0] / "failure.jsonl").read_text(encoding="utf-8").splitlines()), 1)
        with patch("trace_feedback.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            dialog.clear_button.click()
        self.assertFalse(self.app._trace.enabled)
        self.assertEqual(self.store.recent(), [])
        self.assertIsNone(dialog._selected_id)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertTrue(exports[0].exists())
        self.assertEqual(before, (list(self.app._recent_turns), list(self.app._conversation._reactions)))
        self.assertEqual(self.ask.call_count, 1)
