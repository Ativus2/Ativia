"""Frozen, scripted scene contracts. These do not measure visual inference or taste."""
import json
import os
from types import SimpleNamespace

import behavior_probes as probes
import vision


def scene(**changes):
    value = dict(scene="coding", event="error", subject="terminal:missing numpy",
                 evidence="当前终端显示 ModuleNotFoundError: numpy", confidence="high",
                 focused=False, intent="REACT", reply="哦，炸了。")
    value.update(changes)
    return value


class CharacterProbes(probes.AppCase):
    def setUp(self):
        super().setUp()
        self.mock("vision.os.getenv", side_effect=lambda key, default="":
                  "test-placeholder" if key == "OPENAI_API_KEY" else default)
        self.client = self.mock("vision.OpenAI")
        self.client.return_value.__enter__.return_value=self.client.return_value
        self.create = self.client.return_value.chat.completions.create
        self.ask.side_effect = vision.comment_on_screenshot
        self.value = scene()
        self.create.side_effect = self.model
        self.step = 0

    def model(self, **request):
        # Both versions receive the same candidate. Old version's wire format
        # is plain text, new version also receives the scripted observations.
        structured = "场景观察协议" in request["messages"][0]["content"]
        text = json.dumps(self.value, ensure_ascii=False) if structured else (
            self.value["reply"] or "[SILENT]")
        if request.get('stream'):
            from unittest.mock import MagicMock
            stream=MagicMock();stream.__enter__.return_value=stream
            stream.__iter__.return_value=iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],usage=None)])
            return stream
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    def observe(self, **changes):
        self.value = scene(**changes)
        self.image_hash.return_value = 0 if self.step % 2 else (1 << 64) - 1
        self.automatic(100 + self.step * 70)
        self.step += 1

    def context(self):
        content=self.create.call_args.kwargs["messages"][1]["content"]
        return content if isinstance(content,str) else content[0]["text"]

    def test_silent_observation_is_remembered(self):
        self.observe(event="none", subject="editor:parser", evidence="正在编辑解析器", intent="WAIT", reply="")
        self.show_text.assert_not_called()
        self.observe()
        self.assertIn("editor:parser", self.context())
        self.assertIn("WAIT", self.context())

    def test_silence_can_track_a_transition(self):
        self.observe(event="error", intent="WAIT", reply="")
        self.observe(event="resolved", evidence="当前测试通过", intent="WAIT", reply="")
        self.observe(event="none", subject="editor:next task", reply="")
        self.assertIn('"event":"error"', self.context())
        self.assertIn('"event":"resolved"', self.context())
        self.show_text.assert_not_called()

    def test_same_event_different_words_stays_quiet(self):
        self.app.control._on_speak_clicked()
        self.observe()
        self.observe(reply="这个错误还在呢。", evidence="numpy 模块仍未找到")
        self.assertEqual(self.show_text.call_count, 1)
        self.assertEqual(self.tts.call_count, 1)
        self.assertEqual(self.create.call_count, 2)

    def test_focused_scenes_do_not_force_a_comment(self):
        for activity in ("coding", "video", "study", "chat", "game"):
            with self.subTest(activity=activity):
                self.observe(scene=activity, focused=True, subject=activity, event="surprise", reply="这里有个有趣的细节。")
        self.show_text.assert_not_called()

    def test_error_then_resolution_can_react(self):
        self.observe()
        self.observe(reply="怎么还在。")
        self.observe(event="resolved", evidence="同一任务测试通过", reply="这下顺眼了。")
        self.assertEqual(self.show_text.call_count, 2)
        self.observe(event="none", reply="")
        self.assertIn('"event":"resolved"', self.context())

    def test_memory_is_bounded_expires_and_survives_mode_switch(self):
        self.observe(subject="retained-task")
        self.app.control._set_mode("learning")
        self.app.control._set_mode("entertainment")
        self.observe(subject="task-1")
        self.assertIn("retained-task", self.context())
        for index in range(2, 10):
            self.observe(subject=f"task-{index}")
        context = self.context()
        self.assertNotIn("retained-task", context)
        self.assertLessEqual(len(context), 3100)
        self.step = 30
        self.observe(subject="fresh-task")
        self.assertNotIn("task-9", self.context())

    def test_grounded_non_helpful_reaction_is_allowed(self):
        self.observe(scene="game", event="surprise", subject="game:unexpected ending",
                     evidence="当前结局画面主角变成了一只猫", reply="它竟然是只猫。")
        self.show_text.assert_called_once()
        self.assertEqual(self.show_text.call_args.args[0], "它竟然是只猫。")

    def test_uncertain_and_empty_events_are_not_announced(self):
        for changes in (dict(confidence="low"), dict(evidence=""), dict(subject=""),
                        dict(scene="unknown"), dict(event="none")):
            with self.subTest(changes=changes):
                self.observe(**changes)
        self.show_text.assert_not_called()

    def test_manual_help_uses_original_channel(self):
        self.value = scene(reply="先安装依赖，再运行测试。")
        self.app._on_chat_submitted("请帮我解决报错")
        self.wait_idle()
        self.assertEqual(self.show_text.call_args.args[0], self.value["reply"])
        self.assertNotIn("场景观察协议", self.create.call_args.kwargs["messages"][0]["content"])
