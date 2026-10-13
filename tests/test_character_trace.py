"""Offline collection contracts, not character-quality evaluations."""
import ast
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from character_trace import TraceCollector, TraceStore, capture_model
from character import Observation
import vision


class TraceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TraceStore(Path(self.tmp.name) / "trace.sqlite3", max_traces=3)
        self.collector = TraceCollector(self.store)

    def draft(self, response="又是它。", status="displayed"):
        self.collector.set_enabled(True) if not self.collector.enabled else None
        trace = self.collector.begin("automatic", "entertainment", {"recent_context": "之前出错"}, {"memory": []})
        trace.update(decision="REACT" if response else "WAIT", delivery=dict(status=status, response=response))
        return trace

    def save(self, **kwargs):
        trace = self.draft(**kwargs)
        self.assertTrue(self.collector.finish(trace))
        return trace

    def test_disabled_creates_no_database(self):
        self.assertIsNone(self.collector.begin("automatic", "entertainment", {}, {}))
        self.assertEqual(self.store.recent(), [])
        self.assertFalse(self.store.path.exists())

    def test_sequence_links_and_reopen_keep_payload(self):
        first, second, third = self.save(), self.save(response=""), self.save(response="总算。")
        self.assertEqual(second["previous_trace_id"], first["id"])
        self.assertEqual(third["previous_trace_id"], second["id"])
        row = TraceStore(self.store.path).get(second["id"])
        self.assertEqual(row["trace"]["session_id"], first["session_id"])
        self.assertIsNone(row["feedback"])
        self.assertFalse(TraceCollector(self.store).enabled)

    def test_stop_restart_discards_inflight(self):
        draft = self.draft()
        self.collector.set_enabled(False)
        self.collector.set_enabled(True)
        self.assertFalse(self.collector.finish(draft))
        self.assertEqual(self.store.recent(), [])

    def test_latest_explicit_labels_only_and_retraction(self):
        first, silent, error = self.save(), self.save(response="", status="silent"), self.save(response="", status="error")
        self.store.label(first["id"], "failure", "continuity", "像第一次见")
        self.store.label(first["id"], "success", "continuity", "重新核对后合适")
        self.store.label(silent["id"], "success", "timing", "该安静")
        destination = self.store.export(self.tmp.name)
        lines = [json.loads(line) for line in (destination / "success.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({line["trace"]["id"] for line in lines}, {first["id"], silent["id"]})
        self.assertEqual((destination / "failure.jsonl").read_text(), "")
        self.assertEqual(len(lines[0]["feedback_history"]), 2)
        self.store.label(first["id"], "unrated")
        self.store.label(silent["id"], "failure", "timing", "其实需要提醒")
        updated = self.store.export(self.tmp.name)
        manifest = json.loads((updated / "manifest.json").read_text())
        self.assertEqual(manifest["counts"], dict(traces=3, success=0, failure=1, unrated=2))
        self.assertNotEqual(updated, destination)
        for name, fingerprint in manifest["files"].items():
            self.assertEqual(hashlib.sha256((updated / name).read_bytes()).hexdigest(), fingerprint)
        self.assertIsNone(self.store.get(error["id"])["feedback"])

    def test_empty_export_has_empty_datasets_not_synthetic_examples(self):
        destination = self.store.export(self.tmp.name)
        for name in ("traces", "success", "failure"):
            self.assertEqual((destination / f"{name}.jsonl").read_text(), "")

    def test_retention_cascades_labels_and_feedback_history_is_bounded(self):
        first = self.save()
        for _ in range(25):
            self.store.label(first["id"], "success")
        self.assertEqual(len(self.store.get(first["id"])["feedback_history"]), 20)
        for _ in range(3):
            self.save()
        self.assertEqual(len(self.store.recent()), 3)
        with self.assertRaises(KeyError):
            self.store.get(first["id"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0], 0)

    def test_feedback_validation_and_nonexistent_ids(self):
        trace = self.save()
        for args in ((trace["id"], "automatic_success"), (trace["id"], "success", "invented"),
                     (trace["id"], "success", "other", "字" * 1001)):
            with self.assertRaises(ValueError):
                self.store.label(*args)
        with self.assertRaises(KeyError):
            self.store.label("missing", "failure")

    def test_bounded_records_mark_truncation(self):
        trace = self.draft(response="字" * 15000)
        self.assertTrue(self.collector.finish(trace))
        saved = self.store.get(trace["id"])["trace"]
        self.assertEqual(len(saved["delivery"]["response"]), 12000)
        self.assertIn(".delivery.response", saved["truncated_fields"])

    def test_clear_removes_feedback_and_pending_record_cannot_reappear(self):
        trace = self.save()
        self.store.label(trace["id"], "failure")
        pending = self.draft()
        self.collector.set_enabled(False)
        self.store.clear()
        self.assertFalse(self.collector.finish(pending))
        self.assertEqual(self.store.recent(), [])

    def test_persistence_error_does_not_raise(self):
        with patch.object(self.store, "add", side_effect=OSError("disk full")):
            self.assertFalse(self.collector.finish(self.draft()))
        self.assertIn("未保存", self.collector.last_error)


def model_reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], model="fake-model",
                           usage=SimpleNamespace(prompt_tokens=50, completion_tokens=8, total_tokens=58))


class ModelTraceTests(unittest.TestCase):
    def test_no_api_payload_or_output_change_and_no_media_or_credentials_stored(self):
        with patch.dict("os.environ", OPENAI_API_KEY="test-secret"), patch("vision.OpenAI") as client:
            create = client.return_value.chat.completions.create
            create.return_value = model_reply("嗯。")
            plain = vision.comment_on_screenshot(b"private-image", user_command="读这句韩语")
            original_payload = create.call_args
            trace = {}
            with capture_model(trace):
                recorded = vision.comment_on_screenshot(b"private-image", user_command="读这句韩语")
            self.assertEqual(recorded, plain)
            self.assertEqual(create.call_args, original_payload)
            self.assertEqual(create.call_count, 2)
            self.assertEqual(trace["model"]["usage"]["total_tokens"], 58)
            raw = json.dumps(trace)
            for forbidden in ("test-secret", "private-image", "image_url", "base64", "api_key"):
                self.assertNotIn(forbidden, raw)
            self.assertEqual(trace["raw_response"], "嗯。")

    def test_invalid_observation_raw_retained_without_changing_exception(self):
        with patch.dict("os.environ", OPENAI_API_KEY="test-secret"), patch("vision.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = model_reply('{"intent":"WAIT"}')
            trace = {}
            with capture_model(trace), self.assertRaisesRegex(vision.ScreenshotAnalysisError, "格式不完整"):
                vision.comment_on_screenshot(b"fake", automatic=True, observe_scene=True)
            self.assertEqual(trace["raw_response"], '{"intent":"WAIT"}')
            untouched = dict(trace)
            client.return_value.chat.completions.create.return_value = model_reply("新内容")
            vision.comment_on_screenshot(b"fake")
            self.assertEqual(trace, untouched)

    def test_iteration4_keeps_character_memory_and_benchmark_sources_frozen(self):
        root=Path(__file__).resolve().parents[1]
        baseline=json.loads((root/"tests/fixtures/iteration4-protected.json").read_text())
        for name,digest in baseline.items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest,name)
