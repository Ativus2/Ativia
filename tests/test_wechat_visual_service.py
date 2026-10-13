"""Synthetic-only Ativia integration. Models and all possible send paths are fake."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from assistant_pause import PauseGate
from integrations.wechat.identity import group_id
from integrations.wechat.visual.session import VisualSession
from integrations.wechat.visual.safety import PersistentModelBudget, SafetyError
from integrations.wechat.visual.types import TARGET_GROUP, VisualEvent
from memory.gate import MemoryGate
from memory.memory_candidate import MemoryCandidateEvaluator
from runtime_settings import RuntimeSettings


def observation(text="@Ativia 你好", sequence=1, capture="capture-test", epoch=0):
    return VisualEvent(
        observation_id=f"{capture}:t{sequence:08d}", binding_id="manual-test-binding",
        group_name=TARGET_GROUP, speaker_session_id=f"wechat:visual:person:{capture}:member-1",
        sender_name="Synthetic member", text=text, observed_at=time.time(),
        capture_session_id=capture, capture_epoch=epoch, sequence=sequence,
        confidence=.98, source="synthetic")


class VisualServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.gate = PauseGate()
        self.contexts = []
        self.sessions = []

        def fake_model(*args, **kwargs):
            self.contexts.append(kwargs["companion_context"])
            return "这是测试窗口内的模拟预览。"

        self.model_patch = patch("vision.respond_to_voice", side_effect=fake_model)
        self.model = self.model_patch.start()
        self.adapter_patch = patch("integrations.wechat.adapter.WechatAdapter.start",
                                   side_effect=AssertionError("real adapter forbidden"))
        self.adapter_patch.start()
        self.send_patch = patch("integrations.wechat.adapter.WechatAdapter.send_message",
                                side_effect=AssertionError("send IPC forbidden"))
        self.send = self.send_patch.start()
        self.session = self.make_session(model_enabled=True)
        self.session.bind("manual-test-binding", "capture-test", 0)

    def make_session(self, **kwargs):
        session = VisualSession(self.root, self.gate, RuntimeSettings(), **kwargs)
        self.sessions.append(session)
        return session

    def tearDown(self):
        for session in reversed(self.sessions):
            session.close()
        self.send_patch.stop()
        self.adapter_patch.stop()
        self.model_patch.stop()
        self.directory.cleanup()

    def deliver(self, event=None):
        event = event or observation()
        now, wall = time.monotonic(), time.time()
        admitted = self.session.receive(event, now=now, wall=wall)
        self.session.tick(now=now+2, wall=wall+2)
        job = self.session.service.job
        if job and job.future:
            try:
                job.future.result(timeout=5)
            except Exception:
                pass  # finish_reply must turn expected budget failures into status.
            self.session.tick(now=now+3, wall=wall+3)
        return admitted

    def test_existing_personality_shadow_recall_and_local_preview(self):
        memory = self.session.runtime.memory
        seed = observation("我不吃香菜", 7).to_chat_event()
        candidate = MemoryCandidateEvaluator().evaluate(seed)
        self.assertIsNotNone(candidate)
        memory.change("save", candidate).result(3)
        private = replace(MemoryGate().judge("记住我喜欢秘密旅行"), content="PRIVATE_CANARY 香菜")
        memory.change("save", private).result(3)
        with patch.object(self.session.runtime.planner, "plan", wraps=self.session.runtime.planner.plan) as planner, \
                patch.object(memory, "recall", wraps=memory.recall) as recall:
            self.assertTrue(self.deliver(observation("@Ativia 香菜")))
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(recall.call_count, 1)
        self.assertEqual(self.model.call_count, 1)
        context = self.contexts[0]
        self.assertIn("不吃香菜", context["memory"])
        self.assertNotIn("PRIVATE_CANARY", str(context))
        self.assertFalse(context["is_owner"])
        self.assertEqual(context["group_id"], group_id(TARGET_GROUP))
        self.assertTrue(self.session.preview["personality_called"])
        self.assertGreater(self.session.preview["memory_recalled"], 0)
        self.assertEqual(self.session.preview["send_blocked"], "local_preview_sink")
        self.assertEqual(self.session.budget.used, 1)
        self.assertIsNone(self.session.service.adapter)
        self.send.assert_not_called()

    def test_no_default_memory_path_and_restart_has_fresh_shadow(self):
        original = self.session.runtime.memory.path
        with patch("companion_runtime.data_directory", side_effect=AssertionError("formal Memory access")):
            second = self.make_session()
        self.assertNotEqual(original, second.runtime.memory.path)
        self.assertTrue(original.is_relative_to(self.root / "shadow"))
        self.assertTrue(second.runtime.memory.path.is_relative_to(self.root / "shadow"))
        self.assertEqual(second.runtime.memory.snapshot, ())

    def test_strict_trigger_ignores_names_near_matches_and_questions(self):
        for sequence, text in enumerate(("Ativia 你好", "@ativia 你好", "@AtiviaX 你好", "@gpt 你好", "为什么？"), 1):
            with self.subTest(text=text):
                self.assertTrue(self.deliver(observation(text, sequence)))
        self.model.assert_not_called()
        self.assertIsNone(self.session.preview)

    def test_visual_never_calls_legacy_policy(self):
        policy = self.session.service.group(TARGET_GROUP).policy
        with patch.object(policy, "decide", side_effect=AssertionError("legacy policy forbidden")):
            self.assertTrue(self.deliver())
        self.assertEqual(self.model.call_count, 1)

    def test_forged_trigger_flag_is_rejected(self):
        event = replace(observation("Ativia 你好").to_chat_event(), is_at_me=True)
        self.assertFalse(self.deliver(event))
        self.model.assert_not_called()

    def test_quote_metadata_cannot_trigger_visual(self):
        event = replace(observation("普通正文").to_chat_event(), is_reply_to_me=True,
                        reply_to_name="Ativia", reply_to_text="@Ativia 旧引用")
        self.assertFalse(self.deliver(event))
        self.model.assert_not_called()

    def test_duplicate_observation_is_delivered_once(self):
        event = observation()
        self.assertTrue(self.deliver(event))
        self.assertFalse(self.deliver(event))
        self.assertEqual(self.session.service.counts["received"], 1)
        self.assertEqual(self.model.call_count, 1)

    def test_invalid_binding_epoch_identity_and_self_are_rejected(self):
        original = observation().to_chat_event()
        variants = [replace(original, platform="other"),
                    replace(original, conversation_type="private"),
                    replace(original, conversation_id=group_id("wrong group")),
                    replace(original, sender_id="owner:local"),
                    replace(original, timestamp_source="source")]
        for key, value in (("binding_id", "wrong"), ("capture_epoch", 1),
                           ("id_source", "wxauto"), ("self_state", "self"),
                           ("capture_session_id", "other"), ("confidence", float("nan"))):
            variants.append(replace(original, raw_message={**original.raw_message, key: value}))
        for event in variants:
            with self.subTest(event=event):
                self.assertFalse(self.deliver(event))
        self.model.assert_not_called()

    def test_invalidation_revokes_completed_preview_and_old_binding(self):
        self.deliver()
        self.assertIsNotNone(self.session.preview)
        self.session.invalidate("group_changed")
        self.assertIsNone(self.session.preview)
        self.assertFalse(self.deliver(observation(sequence=2)))

    def test_pause_requires_new_binding_before_resuming(self):
        self.deliver()
        self.gate.transition("PAUSING")
        self.gate.transition("PAUSED")
        self.assertIsNone(self.session.preview)
        self.assertFalse(self.deliver(observation(sequence=2)))
        self.gate.transition("RESUMING")
        self.gate.transition("RUNNING")
        self.session.tick()
        self.assertFalse(self.deliver(observation(sequence=3)))
        self.session.bind("manual-test-binding", "capture-test", 0)
        self.assertTrue(self.deliver(observation(sequence=4)))
        self.assertEqual(self.model.call_count, 2)

    def test_live_send_config_change_cannot_escape_visual_preview(self):
        event = observation()
        now, wall = time.monotonic(), time.time()
        self.session.receive(event, now=now, wall=wall)
        self.session.tick(now=now+2, wall=wall+2)
        self.session.service.job.future.result(5)
        self.session.service.config = replace(self.session.service.config, dry_run=False)
        self.session.tick(now=now+3, wall=wall+3)
        self.assertIsNone(self.session.preview)
        self.assertFalse(self.session.service.pending_sends)
        self.send.assert_not_called()

    def test_budget_shared_with_vision_and_survives_coordinator_restart(self):
        for number in range(5):
            self.session.budget.consume("vision", f"synthetic-vision-{number}")
        self.deliver()
        self.assertEqual(self.session.budget.remaining, 0)
        self.session.close()
        self.session = self.make_session(model_enabled=True)
        self.session.bind("manual-test-binding", "capture-test", 0)
        self.assertTrue(self.deliver(observation(sequence=2)))
        self.assertEqual(self.model.call_count, 1)
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.budget.used, 6)
        self.assertIn("BudgetExhausted", str(list(self.session.log.rows)))

    def test_listener_default_does_not_consume_model_budget(self):
        self.session.close()
        self.session = self.make_session()
        self.session.bind("manual-test-binding", "capture-test", 0)
        self.assertTrue(self.deliver())
        self.model.assert_not_called()
        self.assertEqual(self.session.budget.used, 0)

    def test_wrong_budget_path_is_rejected(self):
        other = PersistentModelBudget(self.root / "different-budget.sqlite3")
        with self.assertRaises(ValueError):
            VisualSession(self.root, self.gate, RuntimeSettings(), budget=other)

    def test_starting_legacy_coordinator_is_fail_closed(self):
        with self.assertRaises(SafetyError):
            self.session.service.adapter_factory(self.session.service.config)


if __name__ == "__main__":
    unittest.main()
