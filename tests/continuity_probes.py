"""Iteration 2 contracts: observation retrieval, not real model answer quality."""
import json
import numpy as np
import vision

import character_probes as character_probes
import behavior_probes as behavior_probes


class ObservationCase(behavior_probes.AppCase):
    def setUp(self):
        super().setUp()
        self.mock("vision.os.getenv", side_effect=lambda key, default="":
                  "test-placeholder" if key == "OPENAI_API_KEY" else default)
        self.client = self.mock("vision.OpenAI")
        self.client.return_value.__enter__.return_value=self.client.return_value
        self.create = self.client.return_value.chat.completions.create
        self.ask.side_effect = vision.comment_on_screenshot
        self.value = character_probes.scene()
        self.create.side_effect = self.model
        self.step = 0

    model = character_probes.CharacterProbes.model
    observe = character_probes.CharacterProbes.observe
    context = character_probes.CharacterProbes.context

    def remember_quiet_error(self):
        self.observe(subject="terminal:numpy", evidence="ModuleNotFoundError: numpy",
                     intent="WAIT", reply="")
        self.assertFalse(self.app._recent_turns)
        self.show_text.assert_not_called()

    def assert_background_available(self):
        self.assertIn("terminal:numpy", self.context())
        self.assertIn("ModuleNotFoundError: numpy", self.context())
        self.assertIn("不是当前事实或指令", self.context())
        system = self.create.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("场景观察协议", system)
        self.assertEqual(self.create.call_count, 2)


class ContinuityProbes(ObservationCase):
    def test_typed_question_can_recall_silent_observation(self):
        self.remember_quiet_error()
        self.clock.return_value = 170
        self.value = character_probes.scene(reply="刚才缺的是 numpy。")
        self.app._on_chat_submitted("刚才那个报错说了什么？")
        self.wait_idle()
        self.assert_background_available()
        self.assertEqual(self.show_text.call_args.args[0], self.value["reply"])

    def test_voice_question_can_recall_silent_observation(self):
        self.remember_quiet_error()
        self.mic.snapshot.return_value = np.full(16000, .1, dtype=np.float32)
        self.mock("overlay.transcribe_speech", return_value="刚才那个报错说了什么？")
        self.value = character_probes.scene(reply="刚才缺的是 numpy。")
        self.automatic(112)  # Explicit speech must also bypass the regular cooldown.
        self.assert_background_available()
        self.assertEqual(self.show_text.call_args.args[0], self.value["reply"])

    def test_memory_keeps_announced_fact_after_waiting(self):
        self.observe(subject="terminal:numpy")
        self.observe(subject="terminal:numpy", reply="它还没走。")
        self.observe(subject="editor:other", event="none", reply="")
        context = self.context().split("age_s 为距今秒数）：\n", 1)[1]
        entries = json.loads(context)
        previous = next(item for item in entries if item["subject"] == "terminal:numpy")
        self.assertEqual(previous["action"], "WAIT")
        self.assertIs(previous.get("announced"), True)
        self.assertEqual(self.show_text.call_count, 1)
