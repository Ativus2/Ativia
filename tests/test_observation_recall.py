import continuity_probes as probes
import character_probes as scenes


class ObservationRecallTests(probes.ContinuityProbes):
    def test_reading_memory_does_not_mark_a_silent_event_as_announced(self):
        self.remember_quiet_error()
        before = tuple(self.app._character_memory.entries)
        self.value = scenes.scene(reply="刚才缺少 numpy。")
        self.app._on_chat_submitted("刚才是什么？")
        self.wait_idle()
        self.assertEqual(tuple(self.app._character_memory.entries), before)
        self.assertFalse(self.app._character_memory.entries[0].announced)

    def test_expired_observations_are_not_sent_to_user_questions(self):
        self.remember_quiet_error()
        self.clock.return_value = 701
        self.value = scenes.scene(reply="那条已经没有记录了。")
        self.app._on_chat_submitted("刚才的错误是什么？")
        self.wait_idle()
        self.assertNotIn("terminal:numpy", self.context())
        self.assertNotIn("近期观察", self.context())

    def test_learning_and_explanation_do_not_receive_companion_observations(self):
        self.remember_quiet_error()
        self.app.control._set_mode("learning")
        bilingual = "作答（韩语）\n정답은 4입니다.\n中文翻译\n答案是4。"
        self.value = scenes.scene(reply=bilingual)
        self.app._pending_command="帮我做这道题"
        self.app._start_observe()
        self.wait_idle()
        self.assertNotIn("terminal:numpy", self.context())
        self.assertEqual(self.app.control.learning_text.toPlainText(), bilingual)
        self.assertEqual(self.create.call_args.kwargs["max_completion_tokens"], 4096)
        self.app.control.explain_button.click()
        self.wait_idle()
        self.assertNotIn("terminal:numpy", self.context())
        self.assertEqual(self.create.call_args.kwargs["max_completion_tokens"], 6144)

    def test_fresh_session_has_no_observation_boilerplate(self):
        self.value = scenes.scene(reply="嗯，在呢。")
        self.app._on_chat_submitted("在吗")
        self.wait_idle()
        self.assertNotIn("近期观察", self.context())
        self.assertEqual(self.create.call_args.kwargs["max_completion_tokens"], 480)

    def test_plain_question_uses_past_observation_without_implicit_capture(self):
        import base64
        self.remember_quiet_error()
        self.capture.reset_mock()
        self.value = scenes.scene(reply="刚才缺少 numpy，现在页面已经换了。")
        self.app._on_chat_submitted("刚才和现在有什么变化？")
        self.wait_idle()
        self.assert_background_available()
        content = self.create.call_args.kwargs["messages"][1]["content"]
        self.assertIsInstance(content,str)
        self.capture.assert_not_called()
        self.assertIn("本轮没有新截图",content)

    def test_recalled_observations_keep_confidence_and_age(self):
        self.observe(subject="uncertain-task", confidence="low", intent="WAIT", reply="")
        self.clock.return_value = 170
        self.value = scenes.scene(reply="刚才那处我也没看清。")
        self.app._on_chat_submitted("刚才写了什么？")
        self.wait_idle()
        self.assertIn('"confidence":"low"', self.context())
        self.assertIn('"age_s":70', self.context())
