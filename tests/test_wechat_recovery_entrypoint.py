"""Entrypoint tests use a synthetic child script; no real WeChat/model is used."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import wechat_recovery as recovery


class RecoveryEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / recovery.CONFIG_RELATIVE
        self.config = dict(schema_version=1, dry_run=True,
                           allowed_groups=[recovery.TARGET_GROUP], send_enabled=False,
                           voice_enabled=False, model_enabled=False, transport="unverified")
        recovery.write_json(self.config_path, self.config)

    def fake_probe(self, status, code, *, malformed=False, sleep=0):
        script = self.root / "tools/wechat_accessibility_probe.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        payload = "not json" if malformed else json.dumps({"target": {"status": status}})
        script.write_text(
            "import sys,time\nfrom pathlib import Path\n"
            f"time.sleep({sleep!r})\n"
            f"Path(sys.argv[sys.argv.index('--output')+1]).write_text({payload!r},encoding='utf-8')\n"
            f"raise SystemExit({code!r})\n", encoding="utf-8")

    def test_rejects_unsafe_config_before_launching_child(self):
        cases = [dict(dry_run=False), dict(dry_run=1), dict(send_enabled=True),
                 dict(voice_enabled=True), dict(model_enabled=True),
                 dict(allowed_groups=["其他测试群"]),
                 dict(allowed_groups=[recovery.TARGET_GROUP, "other"]),
                 dict(allowed_groups=[recovery.TARGET_GROUP + " "]),
                 dict(transport="approved_by_configuration_alone")]
        for changes in cases:
            with self.subTest(changes=changes):
                recovery.write_json(self.config_path, {**self.config, **changes})
                with patch.object(recovery.subprocess, "Popen") as spawn:
                    with self.assertRaises(ValueError):
                        recovery.execute("start", self.root, self.config_path)
                    spawn.assert_not_called()

    def test_semantic_candidate_cannot_start_listener_model_or_send(self):
        self.fake_probe("SEMANTIC_CANDIDATE", 0)
        result = recovery.execute("start", self.root, self.config_path)
        self.assertEqual(result["status"], "PARTIAL_EXTERNAL")
        self.assertEqual(result["probe_path_status"], "SEMANTIC_CANDIDATE")
        self.assertFalse(result["group_identity_verified"])
        self.assertFalse(result["real_new_messages_verified"])
        self.assertFalse(result["sender_attribution_verified"])
        self.assertFalse(result["listener_started"])
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["real_outbound_actions"], 0)
        self.assertEqual(result["live_send_test"], "NOT_AUTHORIZED")

    def test_client_unavailable_is_external_condition_not_root_cause_failure(self):
        self.fake_probe("CLIENT_UNAVAILABLE", 2)
        result = recovery.execute("diagnose", self.root, self.config_path)
        self.assertEqual(result["status"], "PARTIAL_EXTERNAL")
        self.assertEqual(result["probe_path_status"], "CLIENT_UNAVAILABLE")
        self.assertEqual(result["exit_code"], 2)

    def test_fresh_empty_tree_evidence_does_not_claim_all_routes_exhausted(self):
        self.fake_probe("NO_SEMANTIC_STRUCTURE", 2)
        result = recovery.execute("diagnose", self.root, self.config_path)
        self.assertEqual(result["status"], "PARTIAL_EXTERNAL")
        self.assertEqual(result["reason"], "current_probe_path_has_no_usable_chat_semantics")
        self.assertTrue(Path(result["evidence"]).is_file())
        latest = json.loads((self.root / recovery.RUNTIME_RELATIVE / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["run_id"], result["run_id"])

    def test_invalid_report_does_not_turn_success_exit_into_transport_pass(self):
        self.fake_probe("SEMANTIC_CANDIDATE", 0, malformed=True)
        result = recovery.execute("start", self.root, self.config_path)
        self.assertEqual(result["probe_path_status"], "TOOL_ERROR")
        self.assertEqual(result["exit_code"], 1)

    def test_duplicate_owner_rejected_and_stop_token_scoped_to_current_run(self):
        directory = self.root / recovery.RUNTIME_RELATIVE
        with recovery.RunLease(directory) as first:
            with self.assertRaises(recovery.AlreadyRunning):
                with recovery.RunLease(directory):
                    self.fail("duplicate lease acquired")
            self.assertFalse(first.stop_requested())
            result = recovery.request_stop(directory)
            self.assertEqual(result["status"], "STOP_REQUESTED")
            self.assertTrue(first.stop_requested())
        with recovery.RunLease(directory) as second:
            self.assertFalse(second.stop_requested())
        self.assertEqual(recovery.request_stop(directory)["status"], "NOT_RUNNING")

    def test_cancel_only_terminates_child_spawned_by_this_entrypoint(self):
        self.fake_probe("SEMANTIC_CANDIDATE", 0, sleep=30)
        directory = self.root / recovery.RUNTIME_RELATIVE
        with recovery.RunLease(directory) as lease:
            recovery.request_stop(directory)
            output = directory / "cancelled.json"
            result = recovery.run_probe(self.root, output, lease, 5)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["exit_code"], 130)

    def test_timeout_is_tool_condition_not_negative_chat_capability_evidence(self):
        self.fake_probe("SEMANTIC_CANDIDATE", 0, sleep=30)
        result = recovery.execute("diagnose", self.root, self.config_path, timeout=.1)
        self.assertEqual(result["status"], "PARTIAL_EXTERNAL")
        self.assertEqual(result["reason"], "readonly_probe_timeout")

    def test_stop_never_spawns_or_kills_a_process(self):
        directory = self.root / recovery.RUNTIME_RELATIVE
        with recovery.RunLease(directory):
            with patch.object(recovery.subprocess, "Popen") as spawn:
                recovery.execute("stop", self.root, self.config_path)
                spawn.assert_not_called()
        with patch.object(recovery.os, "kill") as kill:
            recovery.request_stop(directory)
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
