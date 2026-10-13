"""Explicit self-test only: synthetic pixels and fake worker, no real input/model."""
import builtins
from dataclasses import replace
import importlib.util
import sys
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from integrations.wechat.visual import self_capture, worker
from integrations.wechat.visual.self_capture import (
    SELF_FILL, SelfTestCapture, SelfTextCandidate, SelfScanResult,
    find_self_candidates, normalize_self_text, self_test_text, validate_self_token,
)
from integrations.wechat.visual.layout import Layout, OCRRow
from integrations.wechat.visual.types import Rect


TOKEN = "ABCDEF123456"


class SelfProtocolTests(unittest.TestCase):
    def test_fixed_text_and_token_validation(self):
        self.assertEqual(self_test_text(TOKEN), "@Ativia 自测 ABCDEF123456 请用一句话回应你好。")
        for value in ("abcdef123456", "ABCDEF12345", "ABCDEF1234567", "GBCDEF123456", None, 123):
            with self.assertRaises(ValueError):
                validate_self_token(value)

    def test_protocol_import_does_not_require_visual_or_process_packages(self):
        original = builtins.__import__
        def restricted(name, *args, **kwargs):
            if name.split(".")[0] in {"PIL", "psutil", "numpy", "maa"}:
                raise AssertionError("heavy protocol dependency")
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location("self_protocol_test_only", self_capture.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}), patch.object(builtins, "__import__", restricted):
            spec.loader.exec_module(module)
            self.assertEqual(module.self_test_text(TOKEN), self_test_text(TOKEN))


class SelfCaptureStateTests(unittest.TestCase):
    def setUp(self):
        self.wall = 1000.0
        self.mono = 100.0
        self.capture = SelfTestCapture(TOKEN, self.wall, clock=lambda: self.wall, monotonic=lambda: self.mono)
        self.candidate = SelfTextCandidate(self_test_text(TOKEN), (700, 200, 600, 80), .98)
        self.sequence = 0

    def observe(self, candidates=None, **changes):
        self.sequence += 1
        self.wall += .1
        self.mono += .1
        context = dict(binding_id="manual-binding", capture_session_id="self-session", capture_epoch=1,
                       frame_sequence=self.sequence, observed_at=self.wall)
        context.update(changes)
        return self.capture.observe([self.candidate] if candidates is None else candidates, **context)

    def test_two_distinct_frames_emit_one_self_observation_never_visual_event(self):
        self.assertEqual(self.observe().status, "self_test_waiting")
        result = self.observe()
        self.assertIsNotNone(result.observation)
        self.assertEqual(result.observation["stable_frames"], 2)
        self.assertEqual(result.observation["self_state"], "self")
        self.assertEqual(result.observation["evidence_kind"], "real_self_test")
        self.assertNotIn("message_id", result.observation)
        for _ in range(5):
            self.assertIsNone(self.observe().observation)

    def test_only_whitespace_is_normalized(self):
        spaced = replace(self.candidate, text="@Ativia\n自测\u2005ABCDEF 123456\n请用一句话回应你好。")
        self.assertEqual(normalize_self_text(spaced.text), normalize_self_text(self_test_text(TOKEN)))
        self.observe([spaced])
        self.assertIsNotNone(self.observe([spaced]).observation)

    def test_case_missing_character_punctuation_and_extra_text_all_rejected(self):
        variants = [self_test_text(TOKEN).replace("Ativia", "ativia"),
                    self_test_text(TOKEN).replace("ABCDEF", "ABCDE"),
                    self_test_text(TOKEN).replace("。", "."),
                    self_test_text(TOKEN) + "额外正文",
                    self_test_text(TOKEN).replace("自测", "测试")]
        for text in variants:
            self.capture.invalidate()
            for _ in range(3):
                self.assertIsNone(self.observe([replace(self.candidate, text=text)]).observation)

    def test_self_source_side_completeness_and_quality_are_required(self):
        for changes in (dict(side="left"), dict(self_state="incoming"), dict(source="synthetic"),
                        dict(complete=False), dict(kind="quote"), dict(confidence=.89)):
            self.capture.invalidate()
            for _ in range(3):
                self.assertIsNone(self.observe([replace(self.candidate, **changes)]).observation)

    def test_duplicate_matching_bubbles_are_ambiguous(self):
        second = replace(self.candidate, box=(700, 310, 600, 80))
        for _ in range(3):
            self.assertIsNone(self.observe([self.candidate, second]).observation)

    def test_replayed_frame_does_not_add_stability(self):
        self.observe()
        result = self.observe(frame_sequence=1)
        self.assertEqual(result.reason, "self_test_stale_frame")
        self.assertIsNone(result.observation)
        self.assertIsNotNone(self.observe().observation)

    def test_epoch_binding_or_geometry_changes_require_two_fresh_frames(self):
        self.observe()
        self.assertIsNone(self.observe(capture_epoch=2).observation)
        self.assertIsNotNone(self.observe(capture_epoch=2).observation)

    def test_invalidation_never_rearms_or_extends_deadline(self):
        self.observe()
        original_deadline = self.capture.deadline
        self.capture.invalidate()
        self.assertIsNone(self.observe().observation)
        self.assertIsNotNone(self.observe().observation)
        self.capture.invalidate()
        self.assertIsNone(self.observe().observation)
        self.assertEqual(self.capture.deadline, original_deadline)

    def test_unused_token_expires_and_worker_restart_does_not_refresh_wall_budget(self):
        self.wall = 1299.5
        resumed = SelfTestCapture(TOKEN, 1000.0, clock=lambda: self.wall, monotonic=lambda: self.mono)
        self.assertAlmostEqual(resumed.deadline, self.mono + .5)
        self.wall = 1300.0
        self.assertEqual(self.observe().status, "self_test_expired")

    def test_monotonic_deadline_prevents_wall_clock_extension(self):
        self.mono = 401
        self.wall = 1100
        self.assertEqual(self.observe().status, "self_test_expired")

    def test_consumed_token_remains_captured_after_five_minutes(self):
        self.observe()
        self.observe()
        self.wall += 500
        self.mono += 500
        self.assertFalse(self.capture.expired())
        self.assertEqual(self.observe().status, "self_test_captured")

    def test_wrong_group_and_invalid_issue_timestamp_rejected(self):
        self.assertIsNone(self.observe(group_name="other group").observation)
        for value in (True, float("inf"), float("nan"), 1001):
            with self.assertRaises(ValueError):
                SelfTestCapture(TOKEN, value, clock=lambda: 1000, monotonic=lambda: 100)


class SelfGreenImageTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (1397, 1131), (30, 30, 31))
        self.layout = Layout(Rect(528, 0, 869, 138), Rect(528, 139, 869, 636), Rect(528, 825, 869, 224), "synthetic")
        self.box = Rect(850, 350, 450, 110)
        self.row = OCRRow(self_test_text(TOKEN), Rect(870, 372, 405, 55), .99)
        self.draw = ImageDraw.Draw(self.image)

    def draw_bubble(self, *, fill=SELF_FILL, box=None):
        box = self.box if box is None else box
        self.draw.rounded_rectangle((box.x, box.y, box.right - 1, box.bottom - 1), radius=7, fill=fill)
        for x in range(self.row.box.x + 3, self.row.box.right - 6, 15):
            self.draw.rectangle((x, self.row.box.y + 4, x + 6, self.row.box.bottom - 4), fill=(20, 35, 26))

    def test_complete_right_green_text_is_self_candidate(self):
        self.draw_bubble()
        result = find_self_candidates(self.image, self.layout, [self.row])
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].self_state, "self")
        self.assertEqual(result.candidates[0].source, "local_ocr")

    def test_gray_bubble_is_never_self_test_candidate(self):
        self.draw_bubble(fill=(47, 47, 48))
        self.assertEqual(find_self_candidates(self.image, self.layout, [self.row]).candidates, ())

    def test_left_green_picture_is_not_accepted_as_own_bubble(self):
        self.box = Rect(650, 350, 450, 110)
        self.row = replace(self.row, box=Rect(670, 372, 405, 55))
        self.draw_bubble()
        self.assertEqual(find_self_candidates(self.image, self.layout, [self.row]).candidates, ())

    def test_low_ocr_quality_and_incomplete_bubble_rejected(self):
        self.draw_bubble()
        self.assertEqual(find_self_candidates(self.image, self.layout, [replace(self.row, score=.89)]).candidates, ())
        self.image = Image.new("RGB", (1397, 1131), (30, 30, 31))
        self.draw = ImageDraw.Draw(self.image)
        self.box = Rect(850, 700, 450, 110)
        self.row = replace(self.row, box=Rect(870, 712, 405, 35))
        self.draw_bubble()
        self.assertEqual(find_self_candidates(self.image, self.layout, [self.row]).candidates, ())

    def test_secondary_green_quote_panel_inside_ocr_box_rejected(self):
        self.draw_bubble()
        self.draw.rectangle((880, 380, 1240, 411), fill=(43, 200, 131))
        self.assertEqual(find_self_candidates(self.image, self.layout, [self.row]).candidates, ())


class SelfWorkerModeTests(unittest.TestCase):
    def run_worker(self, *, issued=1000.0, guard_after=False, **options):
        from test_wechat_visual_worker import WorkerHarness
        harness = WorkerHarness(**options)
        original_run, original_observe = worker.run, SelfTestCapture.observe
        candidate = SelfTextCandidate(self_test_text(TOKEN), (250, 160, 120, 60), .99)
        def run(arguments):
            arguments.self_test_token = TOKEN
            arguments.self_test_issued_at = issued
            return original_run(arguments)
        def observe(instance, *args, **kwargs):
            result = original_observe(instance, *args, **kwargs)
            if guard_after and result.observation is not None:
                harness.foreground_after = True
            return result
        with patch.object(worker, "run", side_effect=run), \
             patch.object(worker, "find_self_candidates", return_value=SelfScanResult((candidate,), ())) as scan, \
             patch.object(worker.VisualMessageTracker, "process", side_effect=AssertionError("normal tracker used in self mode")), \
             patch.object(SelfTestCapture, "observe", observe):
            packets = harness.run()
        return harness, packets, scan.call_count

    def test_own_test_emits_once_without_normal_event_or_tracker(self):
        harness, packets, scans = self.run_worker(new_message=True)
        observations = [packet for packet in packets if packet["stage"] == "self_test_observation"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["observation"]["stable_frames"], 2)
        self.assertNotIn("event", [packet["stage"] for packet in packets])
        self.assertNotIn("listener_ready", [packet["stage"] for packet in packets])
        self.assertEqual(scans, 1)
        self.assertEqual(harness.body_calls, 1)

    def test_after_capture_still_runs_interaction_guard_without_rearming(self):
        harness, packets, scans = self.run_worker(guard_after=True)
        self.assertEqual(sum(packet["stage"] == "self_test_observation" for packet in packets), 1)
        self.assertTrue(any(packet.get("reason") == "manual_interaction_bottom_uncertain" for packet in packets))
        self.assertEqual(scans, 1)

    def test_without_confirmation_never_ocr_body_or_deliver_self(self):
        harness, packets, scans = self.run_worker(confirmed=False)
        self.assertEqual(scans, 0)
        self.assertEqual(harness.body_calls, 0)
        self.assertNotIn("self_test_observation", [packet["stage"] for packet in packets])

    def test_expired_token_does_not_start_capture(self):
        harness, packets, scans = self.run_worker(issued=700.0)
        self.assertEqual(harness.captures, 0)
        self.assertEqual(scans, 0)
        self.assertEqual(sum(packet["stage"] == "self_test_expired" for packet in packets), 1)


if __name__ == "__main__":
    unittest.main()
