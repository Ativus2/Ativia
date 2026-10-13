"""Observable character policy contracts; not subjective personality ratings."""
import numpy as np
import continuity_probes as probes
import character_probes as scenes


class PresenceFlowTests(probes.ObservationCase):
    def three_reactions(self):
        self.app.control._on_speak_clicked()
        for index in range(3):
            self.observe(scene="game", event="surprise", subject=f"ending-{index}",
                         evidence=f"当前结局出现转折{index}", reply=f"竟然还有第{index}层。")
        self.assertEqual(self.show_text.call_count, 3)

    def test_unanswered_new_events_leave_space_but_are_observed(self):
        self.three_reactions()
        self.observe(scene="game", event="success", subject="new-win", reply="又赢了。")
        self.assertEqual(self.show_text.call_count, 3)
        self.assertEqual(self.tts.call_count, 3)
        self.assertEqual(self.create.call_count, 4)
        self.assertEqual(self.app._character_memory.entries[-1].observation.subject, "new-win")
        self.assertEqual(self.app._character_memory.entries[-1].action, "WAIT")
        self.assertFalse(self.app._character_memory.entries[-1].announced)

    def test_typed_engagement_reopens_conversation(self):
        self.three_reactions()
        self.observe(subject="quiet", reply="还有一个变化。")
        self.assertEqual(self.show_text.call_count, 3)
        self.value = scenes.scene(reply="嗯，刚才那下挺突然。")
        self.app._on_chat_submitted("刚才那个转折还挺有意思")
        self.wait_idle()
        self.observe(subject="next", reply="它还有后手。")
        self.assertEqual(self.show_text.call_count, 5)

    def test_voice_engagement_reopens_conversation(self):
        self.three_reactions()
        self.observe(subject="quiet", reply="还有一个变化。")
        self.assertEqual(self.show_text.call_count, 3)
        self.mic.snapshot.return_value = np.full(16000, .1, dtype=np.float32)
        self.mock("overlay.transcribe_speech", return_value="刚才发生了什么")
        self.value = scenes.scene(reply="它把结局又改了一遍。")
        self.automatic(322)
        self.mic.snapshot.return_value = np.zeros(0, dtype=np.float32)
        # Past the voice-triggered cooldown, with a genuinely changed hash,
        # but still inside the initial three-comment window (100..392).
        self.value = scenes.scene(subject="next", reply="它还有后手。")
        self.image_hash.return_value = (1 << 64) - 1
        self.automatic(392)
        self.assertEqual(self.show_text.call_count, 5)

    def test_silence_budget_recovers_without_demanding_a_reply(self):
        self.three_reactions()
        self.observe(subject="quiet", reply="还有一个变化。")
        self.assertEqual(self.show_text.call_count, 3)
        self.step = 6
        self.observe(subject="later", reply="这一段倒是清爽。")
        self.assertEqual(self.show_text.call_count, 4)

    def test_character_does_not_tease_an_active_error(self):
        self.observe(intent="TEASE", reply="你又把它弄炸了。")
        self.show_text.assert_not_called()
        self.assertEqual(self.app._character_memory.entries[-1].action, "WAIT")
        self.observe(scene="game", event="surprise", subject="cat-ending", intent="TEASE",
                     evidence="结局主角变成猫", reply="这结局有猫腻。")
        self.assertEqual(self.show_text.call_count, 1)
        self.assertEqual(self.show_text.call_args.args[0], "这结局有猫腻。")

    def test_silent_observations_do_not_spend_conversation_budget(self):
        for index in range(3):
            self.observe(subject=f"quiet-{index}", intent="WAIT", reply="")
        self.observe(subject="new", reply="终于有点变化。")
        self.assertEqual(self.show_text.call_count, 1)

    def test_switching_mode_does_not_reset_unanswered_reactions(self):
        self.three_reactions()
        self.app.control._set_mode("learning")
        self.app.control._set_mode("entertainment")
        self.observe(subject="new", reply="再说一件事。")
        self.assertEqual(self.show_text.call_count, 3)

    def test_configured_identity_reaches_text_and_voice_requests(self):
        from unittest.mock import patch
        import audio
        from runtime_settings import RuntimeSettings
        from tts_fixtures import audio_file
        from persona import PROFILE
        self.value = scenes.scene(reply="嗯，在呢。")
        self.app._on_chat_submitted("在吗")
        self.wait_idle()
        self.assertIn(PROFILE.introduction(), self.create.call_args.kwargs["messages"][0]["content"])
        with patch("audio.run_backend", return_value=audio_file(self)) as backend:
            audio.synthesize_speech("嗯，在呢。", RuntimeSettings(speech_quirk_enabled=False))
            request = backend.call_args.kwargs
            self.assertEqual(request["instructions"], PROFILE.voice_direction)
            self.assertEqual(request["text"], "嗯，在呢。")

    def test_conversation_state_is_sent_before_candidate_generation(self):
        self.three_reactions()
        self.observe(subject="quiet", reply="又有变化了。")
        self.assertIn("这次只观察", self.context())
        self.assertIn("不代表用户拒绝", self.context())
