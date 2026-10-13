"""Synthetic geometry/text only. No screenshot, real user data, model or input calls."""
from dataclasses import replace
import unittest

from integrations.wechat.visual.binding import BindingManager
from integrations.wechat.visual.tracker import VisualMessageTracker
from integrations.wechat.visual.types import (
    Rect, TARGET_GROUP, VisualFrame, VisualMessage, WindowIdentity, has_explicit_trigger,
)


def message(text, y, **changes):
    values = dict(text=text, box=Rect(40, y, 200, 30), sender_name="合成群友",
                  speaker_hint="synthetic-avatar-a", side="left", kind="text",
                  confidence=.98, complete=True, self_state="incoming", source="synthetic")
    values.update(changes)
    return VisualMessage(**values)


def frame(sequence, messages=(), **changes):
    values = dict(capture_session_id="synthetic-session", epoch=1,
                  window=WindowIdentity(123, 456, "synthetic-start", Rect(10, 20, 800, 600), 96),
                  image_width=800, image_height=600, observed_at=1000.0 + sequence,
                  header_text=TARGET_GROUP, messages=tuple(messages), at_bottom=True,
                  layout_stable=True, context_token="synthetic-local-header-layout",
                  frame_sequence=sequence, header_complete=True)
    values.update(changes)
    return VisualFrame(**values)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.manager = BindingManager()

    def bind(self):
        for index in range(1, 4):
            observation = self.manager.observe(frame(index))
        self.assertEqual(observation.status, "READY_FOR_CONFIRMATION")
        return self.manager.confirm(frame(3))

    def test_exact_header_requires_distinct_frames_and_manual_confirmation(self):
        self.assertEqual(self.manager.observe(frame(1)).status, "WAITING_TITLE")
        self.assertEqual(self.manager.observe(frame(1)).status, "STALE_FRAME")
        with self.assertRaises(ValueError):
            self.manager.confirm(frame(1))
        self.manager.observe(frame(2))
        self.assertEqual(self.manager.observe(frame(3)).status, "READY_FOR_CONFIRMATION")
        self.assertIsNone(self.manager.binding)
        binding = self.manager.confirm(frame(3))
        self.assertTrue(binding.matches(frame(4)))
        self.assertEqual(binding.identity_quality, "manual_session_binding")

    def test_wrong_or_partial_header_cannot_bind(self):
        for title in ("其他测试群", TARGET_GROUP + "2", "巨大历史机舍", " " + TARGET_GROUP):
            self.assertEqual(self.manager.observe(frame(1, header_text=title)).status, "UNBOUND")
        self.assertEqual(self.manager.observe(frame(1, header_complete=False)).status, "UNBOUND")
        with self.assertRaises(ValueError):
            BindingManager("other-group")

    def test_switch_context_window_or_epoch_revokes_binding(self):
        mutations = [dict(header_text="another-group"), dict(context_token="changed"),
                     dict(epoch=2), dict(capture_session_id="new-session"),
                     dict(window=replace(frame(4).window, pid=987)),
                     dict(window=replace(frame(4).window, client_rect=Rect(11, 20, 800, 600))),
                     dict(image_width=799)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.manager = BindingManager()
                binding = self.bind()
                changed = frame(4, **mutation)
                self.assertFalse(binding.matches(changed))
                self.manager.observe(changed)
                self.assertIsNone(self.manager.binding)

    def test_pause_capture_failure_and_minimize_invalidate(self):
        for mutation in (dict(paused=True), dict(capture_ok=False), dict(minimized=True)):
            self.manager = BindingManager()
            self.bind()
            self.assertEqual(self.manager.observe(frame(4, **mutation)).status, "UNBOUND")
            self.assertIsNone(self.manager.binding)

    def test_confirm_requires_last_observed_frame(self):
        self.bind()
        with self.assertRaises(ValueError):
            self.manager.confirm(frame(2))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.binding_manager = BindingManager()
        for index in range(1, 4):
            self.binding_manager.observe(frame(index))
        self.binding = self.binding_manager.confirm(frame(3))
        self.tracker = VisualMessageTracker()
        self.old = [message("旧消息 A", 80), message("旧消息 B", 140)]
        self.index = 4

    def step(self, messages, **changes):
        result = self.tracker.process(frame(self.index, messages, **changes), self.binding)
        self.index += 1
        return result

    def baseline(self):
        result = self.step(self.old)
        self.assertEqual(result.status, "BASELINE_COMPLETE")
        self.assertEqual(result.events, ())

    def test_initial_frame_and_repeated_history_never_emit(self):
        self.baseline()
        for _ in range(5):
            self.assertEqual(self.step(self.old).events, ())

    def test_new_bubble_waits_two_frames_and_delivers_once(self):
        self.baseline()
        rows = self.old + [message("@Ativia 你好", 200)]
        self.assertEqual(self.step(rows).events, ())
        event, = self.step(rows).events
        self.assertTrue(event.is_at_me)
        self.assertEqual(event.observation_id, "synthetic-session:t00000003")
        self.assertEqual(event.timestamp_source, "first_observed")
        self.assertEqual(event.observed_at, 1005.0)
        self.assertEqual(self.step(rows).events, ())
        self.assertEqual(self.tracker.ledger_size, 1)

    def test_two_distinct_identical_bubbles_have_distinct_ids(self):
        self.baseline()
        rows = self.old + [message("同文", 200)]
        self.step(rows)
        first, = self.step(rows).events
        rows.append(message("同文", 260))
        self.step(rows)
        second, = self.step(rows).events
        self.assertEqual(first.text, second.text)
        self.assertNotEqual(first.observation_id, second.observation_id)

    def test_shared_upward_displacement_preserves_tracks(self):
        self.baseline()
        rows = [replace(row, box=replace(row.box, y=row.box.y - 30)) for row in self.old]
        rows.append(message("新气泡", 190))
        self.assertEqual(self.step(rows).status, "LISTENER_READY")
        event, = self.step(rows).events
        self.assertEqual(event.sequence, 3)

    def test_scrolled_out_prefix_with_two_surviving_anchors(self):
        rows = self.old + [message("旧消息 C", 200)]
        self.step(rows)
        next_rows = [message("旧消息 B", 50), message("旧消息 C", 110), message("新消息", 170)]
        self.assertEqual(self.step(next_rows).status, "LISTENER_READY")
        self.assertEqual(len(self.step(next_rows).events), 1)

    def test_ocr_correction_of_delivered_track_does_not_emit_again(self):
        self.baseline()
        rows = self.old + [message("@Ativia 测式一下", 200)]
        self.step(rows)
        first, = self.step(rows).events
        rows[-1] = message("@Ativia 测试一下", 200)
        for _ in range(4):
            self.assertEqual(self.step(rows).events, ())
        self.assertEqual(first.sequence, 3)
        self.assertEqual(self.tracker.ledger_size, 1)

    def test_ocr_correction_before_delivery_restarts_stability(self):
        self.baseline()
        rows = self.old + [message("@Ativia 测式", 200)]
        self.step(rows)
        rows[-1] = message("@Ativia 测试", 200)
        self.assertEqual(self.step(rows).events, ())
        event, = self.step(rows).events
        self.assertEqual(event.text, "@Ativia 测试")

    def test_half_bubble_waits_for_complete_stable_body(self):
        self.baseline()
        rows = self.old + [message("@Ativia 半截", 200, complete=False)]
        self.step(rows)
        self.assertEqual(self.step(rows).events, ())
        rows[-1] = message("@Ativia 半截", 200)
        self.assertEqual(self.step(rows).events, ())
        self.assertEqual(len(self.step(rows).events), 1)

    def test_half_bubble_present_at_baseline_stays_history(self):
        rows = self.old + [message("@Ativia 半截", 200, complete=False)]
        self.step(rows)
        rows[-1] = message("@Ativia 半截", 200)
        for _ in range(4):
            self.assertEqual(self.step(rows).events, ())

    def test_gap_rebuilds_baseline_without_replaying_unknown_period(self):
        self.baseline()
        completely_different = [message("突发替换 X", 80), message("突发替换 Y", 140)]
        self.assertEqual(self.step(completely_different).status, "GAP")
        self.assertEqual(self.step(completely_different).status, "BASELINE_COMPLETE")
        self.assertEqual(self.step(completely_different).events, ())

    def test_one_surviving_anchor_in_busy_chat_is_insufficient(self):
        self.baseline()
        rows = [message("旧消息 B", 50), message("不同新内容", 140)]
        self.assertEqual(self.step(rows).status, "GAP")

    def test_scroll_bottom_uncertainty_capture_failure_rebaseline(self):
        for mutation in (dict(scroll_detected=True), dict(at_bottom=False), dict(capture_ok=False), dict(minimized=True)):
            with self.subTest(mutation=mutation):
                self.tracker = VisualMessageTracker()
                self.baseline()
                self.assertEqual(self.step(self.old, **mutation).status, "GAP")
                self.assertEqual(self.step(self.old).status, "BASELINE_COMPLETE")

    def test_pause_epoch_and_group_binding_reject_old_work(self):
        self.baseline()
        self.assertEqual(self.step(self.old, paused=True).status, "PAUSED")
        self.assertEqual(self.step(self.old).status, "BASELINE_COMPLETE")
        self.assertEqual(self.step(self.old, epoch=2).status, "GROUP_UNBOUND")
        self.assertEqual(self.step(self.old, header_text="other").status, "GROUP_UNBOUND")

    def test_repeated_frame_cannot_increment_stability(self):
        self.baseline()
        rows = self.old + [message("@Ativia 测试", 200)]
        current = frame(self.index, rows)
        self.assertEqual(self.tracker.process(current, self.binding).events, ())
        for _ in range(5):
            self.assertEqual(self.tracker.process(current, self.binding).status, "STALE_FRAME")
        self.index += 1
        self.assertEqual(len(self.step(rows).events), 1)

    def test_self_system_unknown_low_quality_and_unseparated_quote_rejected(self):
        variants = [dict(side="right", self_state="self"), dict(kind="system", side="center"),
                    dict(self_state="unknown"), dict(confidence=.6),
                    dict(kind="quote", quote_separated=False), dict(kind="image")]
        for variant in variants:
            with self.subTest(variant=variant):
                self.tracker = VisualMessageTracker()
                self.baseline()
                rows = self.old + [message("@Ativia 旧触发", 200, **variant)]
                for _ in range(4):
                    self.assertEqual(self.step(rows).events, ())

    def test_quote_trigger_only_in_new_separated_body(self):
        self.baseline()
        rows = self.old + [message("@Ativia 引用的旧正文", 200, kind="quote", quote_separated=True, body_text="只是引用")]
        self.step(rows)
        event, = self.step(rows).events
        self.assertEqual(event.text, "只是引用")
        self.assertFalse(event.is_at_me)

    def test_unknown_speakers_are_not_merged(self):
        self.baseline()
        rows = self.old + [message("消息一", 200, speaker_hint="", sender_name=""),
                           message("消息二", 260, speaker_hint="", sender_name="")]
        self.step(rows)
        first, second = self.step(rows).events
        self.assertNotEqual(first.speaker_session_id, second.speaker_session_id)
        self.assertEqual(first.identity_quality, "visual_observation")
        self.assertNotEqual(first.speaker_session_id, "owner:local")

    def test_bounded_ledger_and_non_reused_ids_after_reset(self):
        self.baseline()
        rows = self.old + [message("新消息", 200)]
        self.step(rows)
        before, = self.step(rows).events
        self.tracker.reset("explicit_gap")
        self.step(self.old)
        self.step(rows)
        after, = self.step(rows).events
        self.assertGreater(after.sequence, before.sequence)
        self.assertLessEqual(self.tracker.ledger_size, 2048)

    def test_delivered_ledger_really_evicts_at_capacity(self):
        self.tracker = VisualMessageTracker(ledger_limit=256)
        self.baseline()
        rows = self.old
        observations = set()
        for number in range(270):
            rows = [replace(row, box=replace(row.box, y=80 + index * 60)) for index, row in enumerate(rows[-2:])]
            rows.append(message(f"合成新消息 {number:04d}", 200))
            self.step(rows)
            event, = self.step(rows).events
            observations.add(event.observation_id)
        self.assertEqual(len(observations), 270)
        self.assertEqual(self.tracker.ledger_size, 256)

    def test_chat_event_preserves_scope_and_explicit_visual_metadata(self):
        self.baseline()
        rows = self.old + [message("@Ativia 合成测试", 200)]
        self.step(rows)
        event, = self.step(rows).events
        normalized = event.to_chat_event()
        self.assertEqual(normalized.platform, "wechat")
        self.assertEqual(normalized.conversation_id, "wechat:group:" + TARGET_GROUP)
        self.assertEqual(normalized.raw_message["transport"], "visual")
        self.assertEqual(normalized.raw_message["binding_id"], self.binding.binding_id)
        self.assertNotIn("attr", normalized.raw_message)
        self.assertNotEqual(normalized.identity_quality, "stable_id")
        self.assertTrue(normalized.is_at_me)
        with self.assertRaises(ValueError):
            replace(event, speaker_session_id="owner:local")
        with self.assertRaises(ValueError):
            replace(event, source="forged")

    def test_confirmed_empty_chat_can_observe_first_new_bubble(self):
        self.step([], empty_chat_confirmed=True)
        rows = [message("第一条", 100)]
        self.assertEqual(self.step(rows).events, ())
        self.assertEqual(len(self.step(rows).events), 1)

    def test_unconfirmed_empty_view_never_makes_visible_messages_new(self):
        self.step([])
        rows = [message("不能证明是新消息", 100)]
        self.assertEqual(self.step(rows).status, "GAP")


class ValidationTests(unittest.TestCase):
    def test_explicit_trigger_boundaries(self):
        for text in ("@Ativia", "你好 @Ativia 测试", "（@Ativia）", "请@Ativia 帮忙"):
            self.assertTrue(has_explicit_trigger(text), text)
        for text in ("Ativia", "@ativia", "@AtiviaBot", "x@Ativia", "@@Ativia", "@Ativla", "@gpt"):
            self.assertFalse(has_explicit_trigger(text), text)

    def test_types_ranges_bounds_sources_and_order_are_validated(self):
        factories = [lambda: message("x" * 4001, 80), lambda: message("x", 80, confidence=float("nan")),
                     lambda: message("x", 80, complete=1), lambda: message("x", 80, source="untrusted"),
                     lambda: frame(1, [message("x", 590)]),
                     lambda: frame(1, [message("x", 100), message("y", 50)]),
                     lambda: frame(True), lambda: frame(1, observed_at=float("inf")),
                     lambda: frame(1, [message("x", 100)], empty_chat_confirmed=True)]
        for factory in factories:
            with self.assertRaises(ValueError):
                factory()


if __name__ == "__main__":
    unittest.main()
