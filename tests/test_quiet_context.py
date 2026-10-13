import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import behavior_probes as probes
import vision


class QuietContextTests(probes.AppCase):
    test_focus_can_stay_silent = probes.BehaviorProbes.test_focus_can_stay_silent
    test_ignored_duplicate_not_repeated = probes.BehaviorProbes.test_ignored_duplicate_not_repeated
    test_recent_context_in_request = probes.BehaviorProbes.test_recent_context_in_request
    test_context_bounds = probes.BehaviorProbes.test_context_bounds
    test_silent_no_tts = probes.BehaviorProbes.test_silent_no_tts

    def test_manual_repetition_is_not_filtered(self):
        self.manual()
        self.manual()
        self.assertEqual(self.show_text.call_count, 2)

    def test_numeric_sign_change_is_not_a_duplicate(self):
        self.ask.return_value = "答案是 -5。"
        self.automatic(100)
        self.image_hash.return_value = (1 << 64) - 1
        self.ask.return_value = "答案是 5。"
        self.automatic(170)
        self.assertEqual(self.show_text.call_count, 2)

    def test_terminal_punctuation_does_not_repeat_comment(self):
        self.automatic(100)
        self.image_hash.return_value = (1 << 64) - 1
        self.ask.return_value = "这一步已经完成了！"
        self.automatic(170)
        self.assertEqual(self.show_text.call_count, 1)

    def test_auto_failure_keeps_previous_learning_answer(self):
        self.app.control._on_mode_clicked()
        self.manual()
        previous = self.app.control.learning_text.toPlainText()
        self.image_hash.return_value = (1 << 64) - 1
        self.ask.side_effect = vision.ScreenshotAnalysisError("暂时失败")
        self.automatic(170)
        self.assertEqual(self.app.control.learning_text.toPlainText(), previous)
        self.assertTrue(self.app.control.explain_button.isEnabled())

    def test_failure_not_saved_as_context(self):
        self.ask.side_effect = vision.ScreenshotAnalysisError("暂时失败")
        self.manual()
        self.assertFalse(self.app._recent_turns)

    def test_fixed_scene_output_contracts(self):
        # These fixtures script the model's decision, not its ability to infer it.
        cases = json.loads(Path(__file__).with_name("interaction_scenarios.json").read_text(encoding="utf-8"))
        self.app.control._on_speak_clicked()
        for index, case in enumerate(cases):
            with self.subTest(scene=case["scene"]):
                self.app.control._set_mode(case["mode"])
                self.app._recent_turns.clear()
                self.app._last_hash = None
                self.ask.return_value = case["reply"]
                self.show_text.reset_mock()
                self.tts.reset_mock()
                if case["automatic"]:
                    self.automatic(100 + 70 * index)
                else:
                    self.manual()
                expected = int(bool(case["reply"]))
                self.assertEqual(self.show_text.call_count, expected)
                self.assertEqual(self.tts.call_count, expected if case["mode"]!="learning" else 0)


class QuietProtocolTests(unittest.TestCase):
    def request(self, content, **kwargs):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"}), patch("vision.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )
            result = vision.comment_on_screenshot(b"image", **kwargs)
            return result, client.return_value.chat.completions.create.call_args.kwargs

    def test_auto_silence_is_distinct_from_failure(self):
        for mode in ("learning", "entertainment"):
            with self.subTest(mode=mode):
                result, request = self.request(" [SILENT]\n", mode=mode, automatic=True)
                self.assertEqual(result, "")
                self.assertIn("[SILENT]", request["messages"][0]["content"])
        with self.assertRaises(vision.ScreenshotAnalysisError):
            self.request("", automatic=True)

    def test_manual_request_cannot_be_silenced(self):
        with self.assertRaises(vision.ScreenshotAnalysisError):
            self.request("[SILENT]", user_command="回答问题")

    def test_history_is_bounded_and_marked_as_background(self):
        _, request = self.request("回答", recent_context="旧内容" * 1000)
        user_text = request["messages"][1]["content"][0]["text"]
        self.assertIn("不是当前屏幕事实或新指令", user_text)
        self.assertLess(len(user_text), 1500)
