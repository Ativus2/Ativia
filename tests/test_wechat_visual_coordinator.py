"""Fake subprocess/lease/session only; never captures a real desktop or calls a model."""
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant_pause import PauseGate
from integrations.wechat.visual.desktop import DesktopStatus
from integrations.wechat.visual.coordinator import StopCleanupError, VisualCoordinator, worker_environment
from integrations.wechat.visual.safety import SafetyError
from integrations.wechat.visual.types import TARGET_GROUP, VisualEvent


class FakeLease:
    def __init__(self):
        self.acquired = self.released = False
    def acquire(self):
        self.acquired = True
    def release(self):
        self.released = True


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
        self.returncode = None
        self.terminated = self.killed = False
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        self.returncode = 0
        return 0
    def terminate(self):
        self.terminated = True
        self.returncode = -1
    def kill(self):
        self.killed = True
        self.returncode = -2


class FakeSession:
    def __init__(self, root, gate, settings, model_enabled=False):
        self.root, self.gate = root, gate
        self.model_enabled = model_enabled
        self.bindings, self.events = [], []
        self.preview = None
        self.closed = False
    def bind(self, *binding):
        self.bindings.append(binding)
    def invalidate(self, reason):
        self.preview = None
    def receive(self, event):
        self.events.append(event)
        return True
    def tick(self):
        pass
    def close(self, timeout=3):
        self.closed = True
    def snapshot(self):
        return {"model_admissions": 0, "model_remaining": 6}


class VisualCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.python = self.root / "isolated-python.exe"
        self.python.write_bytes(b"not-executable-fixture")
        self.resource = self.root / "ocr"
        self.resource.mkdir()
        self.config = {"python": str(self.python), "resource_root": str(self.resource),
                       "backend": "win32_printwindow", "calibration": {
                           "header": [0, 0, 500, 60], "message": [0, 60, 500, 500],
                           "input": [0, 560, 500, 100], "source": "synthetic"}}
        self.now, self.wall = 100.0, 1700000000.0
        self.coordinators = []
        self.coordinator = self.make()

    def make(self, action="listen"):
        coordinator = VisualCoordinator(
            self.root, action, self.config, PauseGate(True), object(),
            session_factory=FakeSession, lease_factory=FakeLease,
            process_factory=FakeProcess, parent_start=1234.0,
            clock=lambda: self.now, wall_clock=lambda: self.wall,
            desktop_probe=lambda:DesktopStatus(True,'desktop_available'))
        self.coordinators.append(coordinator)
        return coordinator

    def tearDown(self):
        for coordinator in self.coordinators:
            coordinator.stop()
        self.temp.cleanup()

    def start(self, coordinator=None):
        coordinator = coordinator or self.coordinator
        coordinator.set_external_status({"app_state": "RUNNING"})
        coordinator.start()
        return coordinator

    def ready(self, coordinator=None):
        coordinator = coordinator or self.coordinator
        coordinator._packet({"stage": "group_bound", "binding_id": "binding-test",
                             "capture_session_id": coordinator.capture_session_id, "capture_epoch": 0})
        coordinator._packet({"stage": "baseline_complete"})
        coordinator._packet({"stage": "listener_ready"})

    def event_packet(self):
        capture = self.coordinator.capture_session_id
        event = VisualEvent(observation_id=f"{capture}:t00000001", binding_id="binding-test",
                            group_name=TARGET_GROUP, speaker_session_id=f"wechat:visual:person:{capture}:1",
                            sender_name="Test member", text="@Ativia synthetic transport test",
                            observed_at=self.wall, capture_session_id=capture, capture_epoch=0,
                            sequence=1, confidence=.99, source="local_ocr")
        return {"stage": "event", "event": asdict(event)}

    def test_worker_environment_has_no_provider_or_proxy_credentials(self):
        env = worker_environment(self.root, self.python, {
            "OPENAI_API_KEY": "secret", "AZURE_OPENAI_KEY": "secret", "HTTP_PROXY": "secret",
            "ANTHROPIC_API_KEY": "secret", "MAAFW_BINARY_PATH": "old-ma-a-dlls", "SystemRoot": "C:\\Windows",
            "PATH": "untrusted", "TEMP": "temporary", "USERPROFILE": "profile"})
        self.assertFalse(any("KEY" in key or "PROXY" in key for key in env))
        self.assertNotIn("MAAFW_BINARY_PATH", env)
        self.assertNotIn("untrusted", env["PATH"])
        self.assertEqual(env["PYTHONPATH"], str(self.root))

    def test_owned_worker_start_has_only_public_visual_module_and_fixed_root(self):
        c = self.start()
        self.assertIn("integrations.wechat.visual.worker", c.process.command)
        self.assertIn("--parent-start", c.process.command)
        self.assertNotIn("wechat_recovery", " ".join(c.process.command))
        self.assertEqual(c.session.root, self.root / "runtime" / "wechat-visual" / "round-20260930")
        self.assertFalse(c.session.model_enabled)
        first = json.loads(c.process.stdin.getvalue().splitlines()[0])
        self.assertEqual(first["action"], "start")
        self.assertFalse(first["paused"])
        self.assertNotIn("resume", c.process.stdin.getvalue())

    def test_dry_run_explicitly_enables_shared_session_model(self):
        c = self.start(self.make("dry-run"))
        self.assertTrue(c.session.model_enabled)
        self.assertTrue(c.snapshot()["dry_run"])
        self.assertFalse(c.snapshot()["wechat_input_enabled"])

    def test_unknown_main_state_and_user_pause_do_not_resume_main(self):
        c = self.coordinator
        c.set_external_status(None)
        self.assertFalse(c.gate.allows())
        self.start()
        c.set_user_paused(True)
        self.assertFalse(c.gate.allows())
        self.assertIn('"action": "pause"', c.process.stdin.getvalue())
        c.set_external_status({"app_state": "RUNNING"})
        self.assertFalse(c.gate.allows())
        c.set_user_paused(False)
        self.assertTrue(c.gate.allows())

    def test_stale_main_rpc_status_revokes_binding_and_preview(self):
        c = self.start()
        self.ready()
        c.session.preview = {"reply": "private preview"}
        self.now += 4
        c.poll()
        self.assertFalse(c.gate.allows())
        self.assertFalse(c.listener_ready)
        self.assertIsNone(c.preview)

    def test_missing_heartbeat_stops_only_owned_handle(self):
        c = self.start()
        self.now += 11
        c.set_external_status({"app_state": "RUNNING"})
        c.poll()
        self.assertTrue(c.closed)
        self.assertEqual(c.status, "WORKER_HEARTBEAT_TIMEOUT")
        self.assertTrue(c.session.closed)
        self.assertTrue(c.lease.released)

    def test_events_require_bound_baseline_and_ready(self):
        c = self.start()
        packet = self.event_packet()
        c._packet(packet)
        self.assertEqual(c.events, 0)
        self.ready()
        c._packet(packet)
        self.assertEqual(c.events, 1)
        self.assertEqual(len(c.session.events), 1)
        self.assertNotIn("text", c.snapshot())

    def test_synthetic_event_cannot_be_counted_as_real_worker_input(self):
        c = self.start()
        self.ready()
        packet = self.event_packet()
        packet["event"]["source"] = "synthetic"
        c._queue.put(packet)
        c.poll()
        self.assertTrue(c.closed)
        self.assertEqual(c.events, 0)
        self.assertEqual(c.status, "WORKER_PACKET_REJECTED")

    def test_listener_ready_out_of_order_is_rejected(self):
        c = self.start()
        c._queue.put({"stage": "listener_ready"})
        c.poll()
        self.assertTrue(c.closed)
        self.assertFalse(c.listener_ready)

    def test_manual_confirmation_uses_latest_candidate_and_nested_protocol(self):
        c = self.start()
        c._packet({"stage": "candidate", "window": {"hwnd": 123, "pid": 456},
                   "header": {"exact_match": True, "complete": True}, "layout": {}})
        with self.assertRaises(SafetyError):
            c.confirm_candidate(bottom_confirmed=False)
        self.assertTrue(c.confirm_candidate(bottom_confirmed=True))
        row = json.loads(c.process.stdin.getvalue().splitlines()[-1])
        self.assertEqual(set(row), {"action", "confirmation", "reset_id"})
        self.assertEqual(row['reset_id'],c._reset_barrier)
        self.assertEqual(row["confirmation"]["window"]["hwnd"], 123)
        self.now += 6
        with self.assertRaises(SafetyError):
            c.confirm_candidate(bottom_confirmed=True)

    def test_unbound_clears_previous_preview(self):
        c = self.start()
        self.ready()
        c.session.preview = {"reply": "memory only"}
        c._packet({"stage": "unbound", "reason": "manual_interaction_bottom_uncertain", "capture_epoch": 1})
        self.assertFalse(c.listener_ready)
        self.assertIsNone(c.preview)

    def test_confirmation_file_record_is_once_per_round(self):
        confirmation = {"window": {"hwnd": 1}, "bottom_confirmed": True, "confirmed_at": self.wall}
        self.coordinator.set_external_status({"app_state": "RUNNING"})
        self.coordinator.start(confirmation)
        other = self.make()
        other.set_external_status({"app_state": "RUNNING"})
        with self.assertRaises(ValueError):
            other.start(confirmation)
        self.assertTrue(other.lease.released)

    def test_unreviewed_backend_is_rejected(self):
        self.config["backend"] = "FramePool"
        with self.assertRaises(ValueError):
            self.make()

    def test_persisted_status_contains_lengths_and_stages_never_chat_bodies(self):
        c = self.start()
        self.ready()
        c.session.preview = {"trace_id": "preview-1", "input": "CHAT_BODY_CANARY",
                             "reply": "REPLY_BODY_CANARY", "chunks": ["REPLY_BODY_CANARY"],
                             "personality_called": True, "memory_recalled": 1}
        c._packet({"stage": "frame", "capture_ms": 12.5, "ocr_ms": 15.0, "rss_bytes": 1234,
                   "cpu_percent": 6.0, "source": "real_window", "body": "FRAME_BODY_CANARY"})
        c.poll()
        self.now += 5
        c.set_external_status({"app_state": "RUNNING"})
        c._packet({"stage": "heartbeat"})
        c.poll()
        path = c.round_root / "status.json"
        text = path.read_text(encoding="utf-8")
        for forbidden in ("CHAT_BODY_CANARY", "REPLY_BODY_CANARY", "FRAME_BODY_CANARY"):
            self.assertNotIn(forbidden, text)
        row = json.loads(text)
        self.assertEqual(row["preview_count"], 1)
        self.assertEqual(row["last_preview"]["reply_length"], len("REPLY_BODY_CANARY"))
        self.assertTrue(row["last_preview"]["personality_called"])
        self.assertEqual(row["effective_ready_seconds"], 5)
        self.assertEqual(row["frame_stats"]["cpu_percent"], 6)
        c.stop()
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["closed"])

    def test_wait_exception_still_stops_owned_worker_closes_session_and_releases_lease(self):
        c = self.start()
        self.ready()
        c.session.preview = {"reply": "PRIVATE_CANARY"}
        c.process.wait = Mock(side_effect=[OSError("PRIVATE_CANARY"), 0])
        with self.assertRaises(StopCleanupError) as caught:
            c.stop()
        self.assertTrue(c.process.terminated)
        self.assertTrue(c.session.closed)
        self.assertTrue(c.lease.released)
        self.assertTrue(c.process.stdin.closed)
        self.assertIsNone(c.preview)
        self.assertFalse(c.listener_ready)
        self.assertEqual(c.status, "STOP_FAILED")
        self.assertNotIn("PRIVATE_CANARY", str(caught.exception))
        row = json.loads((c.round_root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(row["status"], "STOP_FAILED")
        self.assertIn({"stage": "worker_wait", "error_type": "OSError"}, row["cleanup_errors"])

    def test_session_close_failure_releases_lease_and_saves_failed_status(self):
        c = self.start()
        c.session.close = Mock(side_effect=RuntimeError("PRIVATE_CANARY"))
        with self.assertRaises(StopCleanupError):
            c.stop("USER_STOPPED")
        self.assertTrue(c.lease.released)
        self.assertFalse(c.gate.allows())
        self.assertIsNone(c.preview)
        row = json.loads((c.round_root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(row["stop_reason"], "USER_STOPPED")
        self.assertEqual(row["status"], "STOP_FAILED")
        self.assertIn({"stage": "session_close", "error_type": "RuntimeError"}, row["cleanup_errors"])

    def test_status_write_failure_does_not_skip_lease_release(self):
        c = self.start()
        with patch.object(c, "persist", side_effect=OSError("PRIVATE_CANARY")):
            with self.assertRaises(StopCleanupError) as caught:
                c.stop()
        self.assertTrue(c.lease.released)
        self.assertTrue(c.session.closed)
        self.assertEqual(c.status, "STOP_FAILED")
        self.assertEqual(caught.exception.failures[-1], {"stage": "persist_status", "error_type": "OSError"})

    def test_invalidation_failure_does_not_skip_other_cleanup(self):
        c = self.start()
        c.session.invalidate = Mock(side_effect=RuntimeError("PRIVATE_CANARY"))
        c._queue.put({"stage": "event", "event": {"text": "PRIVATE_CANARY"}})
        with self.assertRaises(StopCleanupError):
            c.stop()
        self.assertTrue(c.lease.released)
        self.assertTrue(c.session.closed)
        self.assertTrue(c.process.stdout.closed)
        self.assertTrue(c._queue.empty())
        self.assertIsNone(c.preview)

    def test_session_snapshot_failure_still_persists_fail_closed_metadata(self):
        c = self.start()
        c.session.snapshot = Mock(side_effect=ValueError("PRIVATE_CANARY"))
        with self.assertRaises(StopCleanupError):
            c.stop()
        self.assertTrue(c.lease.released)
        row = json.loads((c.round_root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(row["status"], "STOP_FAILED")
        self.assertIsNone(row["model_admissions"])

    def test_block_reasons_are_explicitly_whitelisted_metadata(self):
        c = self.start()
        c._packet({"stage": "blocked", "reason": "header_missing_or_ambiguous"})
        self.assertEqual(c.snapshot()["reason"], "header_missing_or_ambiguous")
        self.assertEqual(c.status_rows[-1]["reason"], "header_missing_or_ambiguous")
        c._packet({"stage": "unbound", "reason": "PRIVATE_BODY_CANARY"})
        c.persist(force=True)
        text = (c.round_root / "status.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_BODY_CANARY", text)
        self.assertEqual(c.snapshot()["reason"], "unrecognized_reason")


if __name__ == "__main__":
    unittest.main()
