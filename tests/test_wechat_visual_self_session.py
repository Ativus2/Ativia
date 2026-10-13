"""Synthetic self observations and a fake endpoint; no capture, send or real model."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from assistant_pause import PauseGate
from integrations.wechat.visual.self_capture import self_test_text
from integrations.wechat.visual.session import VisualSession
from integrations.wechat.visual.types import TARGET_GROUP, VisualEvent
from memory.gate import MemoryGate
from runtime_settings import RuntimeSettings


TOKEN = "012345ABCDEF"


def observation(token=TOKEN):
    return dict(token=token, text=self_test_text(token), group_name=TARGET_GROUP,
                binding_id="self-binding", capture_session_id="self-capture", capture_epoch=0,
                observed_at=time.time(), source="local_ocr", self_state="self", side="right",
                confidence=.98, box=[400, 500, 250, 80], evidence_kind="real_self_test",
                complete=True, kind="text", frame_sequence=2, stable_frames=2)


class SelfSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.sessions = []
        self.contexts = []
        self.gate = PauseGate()

        def model(*args, **kwargs):
            self.contexts.append(kwargs["companion_context"])
            return "SELF_REPLY_CANARY：你好。"

        self.model_patch = patch("vision.respond_to_voice", side_effect=model)
        self.model = self.model_patch.start()
        self.adapter_patch = patch("integrations.wechat.adapter.WechatAdapter.start",
                                   side_effect=AssertionError("real adapter forbidden"))
        self.adapter_patch.start()
        self.send_patch = patch("integrations.wechat.adapter.WechatAdapter.send_message",
                                side_effect=AssertionError("real send forbidden"))
        self.send = self.send_patch.start()
        self.session = self.make()

    def make(self, **kwargs):
        options = dict(model_enabled=True, self_test_mode=True)
        options.update(kwargs)
        session = VisualSession(self.root, self.gate, RuntimeSettings(), **options)
        self.sessions.append(session)
        session.bind("self-binding", "self-capture", 0)
        return session

    def tearDown(self):
        for session in reversed(self.sessions):
            session.close()
        self.send_patch.stop()
        self.adapter_patch.stop()
        self.model_patch.stop()
        self.directory.cleanup()

    def finish(self, session=None):
        session = session or self.session
        job = session._self_job
        if job is not None and job.future is not None:
            try:
                job.future.result(timeout=5)
            except Exception:
                pass  # tick records expected failures without retrying them.
        session.tick()

    def test_self_preview_uses_real_core_personality_shadow_recall_with_no_ingress_or_send(self):
        session = self.session
        memory = session.runtime.memory
        private = replace(MemoryGate().judge("记住我喜欢秘密旅行"), content="PRIVATE_CANARY 你好")
        memory.change("save", private).result(3)
        public = replace(private, content="SELF_SHADOW_CANARY 你好", visibility="group",
                         group_id="wechat:visual:self-test:group:self-capture",
                         subject_id="wechat:visual:self-test:self:self-capture")
        memory.change("save", public).result(3)
        with patch.object(session.service, "receive_event", side_effect=AssertionError("ordinary ingress forbidden")), \
                patch.object(session.runtime.planner, "plan", wraps=session.runtime.planner.plan) as planner, \
                patch.object(memory, "recall", wraps=memory.recall) as recall:
            self.assertTrue(session.receive_self_test(observation(), TOKEN))
            event = session._self_job.event
            self.finish()
        self.assertEqual(event.platform, "wechat_self_test")
        self.assertEqual(event.raw_message["self_state"], "self")
        self.assertFalse(event.raw_message["ordinary_incoming"])
        self.assertNotEqual(event.sender_id, "owner:local")
        self.assertFalse(event.is_at_me)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(recall.call_count, 1)
        self.assertEqual(self.model.call_count, 1)
        self.assertIn("SELF_SHADOW_CANARY", self.contexts[0]["memory"])
        self.assertNotIn("PRIVATE_CANARY", str(self.contexts[0]))
        self.assertFalse(self.contexts[0]["is_owner"])
        preview = session.preview
        self.assertEqual(preview["evidence_kind"], "real_self_test")
        self.assertEqual(preview["self_state"], "self")
        self.assertFalse(preview["acceptance_evidence"])
        self.assertEqual(preview["send_blocked"], "local_preview_sink")
        self.assertTrue(preview["personality_called"])
        counts = session.snapshot()
        self.assertEqual([counts[k] for k in ("self_test_observations", "self_test_model_calls", "self_test_previews")], [1, 1, 1])
        self.assertEqual([counts[k] for k in ("received", "model_calls", "sent", "dry_runs", "memory_written")], [0] * 5)
        self.assertEqual(counts["model_admissions"], 1)
        self.assertEqual(counts["self_test_status"], "SELF_TEST_PREVIEW_READY")
        self.assertIsNone(session.service.adapter)
        self.assertFalse(session.service.candidates.pending)
        self.send.assert_not_called()
        self.assertNotIn(self_test_text(TOKEN), str(session.log.rows))
        self.assertNotIn("SELF_REPLY_CANARY", str(session.log.rows))

    def test_explicit_mode_model_and_binding_required(self):
        for kwargs in ({"self_test_mode": False}, {"model_enabled": False}):
            session = self.make(**kwargs)
            self.assertFalse(session.receive_self_test(observation(), TOKEN))
        self.session.invalidate()
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.model.assert_not_called()
        self.assertEqual(self.session.budget.used, 0)

    def test_rejects_foreign_partial_stale_forged_or_low_confidence_observations(self):
        variants = (("group_name", "other"), ("token", "ABCDEF123456"), ("text", "@Ativia ordinary"),
                    ("text", self_test_text(TOKEN) + "别的内容"), ("self_state", "incoming"), ("side", "left"),
                    ("source", "synthetic"), ("evidence_kind", "group_message"), ("complete", False),
                    ("complete", 1), ("kind", "quote"), ("binding_id", "old"), ("capture_epoch", 1),
                    ("capture_epoch", False), ("capture_session_id", "other"), ("confidence", .899),
                    ("confidence", float("nan")), ("confidence", True), ("box", [-1, 2, 4, 4]),
                    ("box", [1, 2, 0, 4]), ("frame_sequence", True), ("stable_frames", 1),
                    ("stable_frames", True), ("observed_at", time.time() - 301), ("observed_at", time.time() + 60))
        for key, value in variants:
            row = observation()
            row[key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(self.session.receive_self_test(row, TOKEN))
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN.lower()))
        self.model.assert_not_called()
        self.assertEqual(self.session.snapshot()["self_test_observations"], 0)

    def test_accepts_only_whitespace_normalization(self):
        row = observation()
        row["text"] = "\n".join(row["text"].split())
        self.assertTrue(self.session.receive_self_test(row, TOKEN))
        self.finish()
        self.assertIsNotNone(self.session.preview)

    def test_expired_session_or_disabled_dry_run_never_admits(self):
        self.session._self_test_started_at -= 301
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.session._self_test_started_at = time.monotonic()
        self.session.service.config = replace(self.session.service.config, dry_run=False)
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.model.assert_not_called()

    def test_success_is_one_shot_and_never_retries_after_binding_change(self):
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.finish()
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.session.invalidate()
        self.assertIsNone(self.session.preview)
        self.session.bind("self-binding", "self-capture", 0)
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.assertEqual(self.model.call_count, 1)

    def test_model_error_consumes_once_and_keeps_error_body_private(self):
        self.model.side_effect = RuntimeError("SECRET_MODEL_ERROR_BODY")
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.finish()
        self.assertEqual(self.session.snapshot()["self_test_status"], "SELF_TEST_FAILED")
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.budget.used, 1)
        self.assertEqual(self.model.call_count, 1)
        self.assertNotIn("SECRET_MODEL_ERROR_BODY", str(self.session.log.rows))

    def test_empty_reply_is_never_published(self):
        self.model.side_effect = lambda *a, **k: "  "
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.finish()
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.snapshot()["self_test_status"], "SELF_TEST_EMPTY_OR_INVALID_REPLY")
        self.assertEqual(self.session.snapshot()["self_test_previews"], 0)

    def test_pause_cancels_active_model_and_late_reply_cannot_publish(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_model(*args, **kwargs):
            entered.set()
            release.wait(3)
            return "LATE_REPLY_CANARY"
        self.model.side_effect = blocked_model
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.assertTrue(entered.wait(2))
        try:
            self.gate.transition("PAUSING")
            self.gate.transition("PAUSED")
        finally:
            release.set()
        self.finish()
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.snapshot()["self_test_previews"], 0)
        self.assertIn("CANCELLED", self.session.snapshot()["self_test_status"])
        self.gate.transition("RESUMING")
        self.gate.transition("RUNNING")
        self.session.bind("self-binding", "self-capture", 0)
        self.assertFalse(self.session.receive_self_test(observation(), TOKEN))
        self.assertEqual(self.model.call_count, 1)

    def test_privacy_epoch_revokes_completed_future_before_preview(self):
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.session._self_job.future.result(5)
        self.session.runtime.memory.privacy_epoch += 1
        self.finish()
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.snapshot()["self_test_previews"], 0)

    def test_pause_during_memory_recall_prevents_model_endpoint_invocation(self):
        memory = self.session.runtime.memory
        original = memory.recall
        def pause_during_recall(*args, **kwargs):
            self.gate.transition("PAUSING")
            self.gate.transition("PAUSED")
            return original(*args, **kwargs)
        with patch.object(memory, "recall", side_effect=pause_during_recall):
            self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
            self.finish()
        self.model.assert_not_called()
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.budget.used, 1)
        self.assertEqual(self.session.snapshot()["self_test_previews"], 0)
        self.assertEqual(self.session.snapshot()["self_test_model_calls"], 0)

    def test_shared_budget_and_replayed_token_survive_session_restart(self):
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.finish()
        second = self.make()
        self.assertTrue(second.receive_self_test(observation(), TOKEN))
        self.finish(second)
        self.assertIsNone(second.preview)
        self.assertEqual(second.snapshot()["self_test_model_calls"], 0)
        self.assertEqual(second.budget.used, 1)
        self.assertEqual(self.model.call_count, 1)
        for number in range(5):
            self.session.budget.consume("synthetic_prior", str(number))
        third = self.make()
        another = "ABCDEF123456"
        self.assertTrue(third.receive_self_test(observation(another), another))
        self.finish(third)
        self.assertEqual(third.budget.used, 6)
        self.assertEqual(third.snapshot()["self_test_model_calls"], 0)
        self.assertIsNone(third.preview)
        self.assertEqual(self.model.call_count, 1)

    def test_normal_visual_self_ignore_remains_in_force(self):
        ordinary = self.make(self_test_mode=False)
        event = VisualEvent(observation_id="self-capture:t00000001", binding_id="self-binding",
                            group_name=TARGET_GROUP, speaker_session_id="wechat:visual:person:self-capture:1",
                            sender_name="Synthetic", text="@Ativia 普通消息", observed_at=time.time(),
                            capture_session_id="self-capture", capture_epoch=0, sequence=1,
                            confidence=.99, source="synthetic").to_chat_event()
        self_event = replace(event, raw_message={**event.raw_message, "self_state": "self"})
        self.assertFalse(ordinary.receive(self_event))
        self.assertFalse(ordinary.receive_self_test(observation(), TOKEN))
        ordinary.tick()
        self.model.assert_not_called()
        self.assertEqual(ordinary.service.counts["received"], 0)

    def test_close_clears_self_preview_without_changing_normal_counters(self):
        self.assertTrue(self.session.receive_self_test(observation(), TOKEN))
        self.finish()
        self.assertIsNotNone(self.session.preview)
        self.session.close()
        self.assertIsNone(self.session.preview)
        self.assertEqual(self.session.snapshot()["received"], 0)
        self.assertEqual(self.session.snapshot()["self_test_previews"], 1)


if __name__ == "__main__":
    unittest.main()
