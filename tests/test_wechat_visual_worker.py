"""Offline worker loop tests: fake native window, capture, OCR, parent and IPC only."""
from contextlib import ExitStack
import io
import json
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from integrations.wechat.visual import worker
from integrations.wechat.visual.types import Rect, TARGET_GROUP, VisualMessage
from integrations.wechat.visual.window import Win32Windows
from integrations.wechat.visual.desktop import DesktopStatus


WINDOW = dict(hwnd=123, pid=456, process_started=100.0, client_rect=(0, 0, 400, 400),
              client_screen=(0, 0), dpi=168, foreground=False)
CALIBRATION = dict(header=[100, 0, 300, 50], message=[100, 51, 300, 250],
                   input=[100, 302, 300, 98], source="calibrated")


def confirmation(**changes):
    value = dict(bottom_confirmed=True, confirmed_at=1000.0, window=dict(WINDOW))
    value.update(changes)
    return json.loads(json.dumps(value))  # Real coordinator -> worker pipe.


def message(text, y):
    return VisualMessage(text, Rect(150, y, 160, 30), side="left", kind="text",
                         confidence=.98, complete=True, self_state="incoming", source="synthetic")


class WorkerHarness:
    def __init__(self, *, loops=7, confirmed=True, wrong_header=False, pause_in_header=False,
                 hover=False, capture_gap=False, move_window=False, ocr_error=False,
                 empty_body=False, new_message=False, foreground_after=False, pointer_target=True,
                 wall_jump=0,locked_after=0,unlock_after=0,reset_commands=()):
        self.loops = loops
        self.confirmed = confirmed
        self.wrong_header = wrong_header
        self.pause_in_header = pause_in_header
        self.hover = hover
        self.capture_gap = capture_gap
        self.move_window = move_window
        self.ocr_error = ocr_error
        self.empty_body = empty_body
        self.new_message = new_message
        self.foreground_after = foreground_after
        self.pointer_target = pointer_target
        self.wall_jump,self.locked_after,self.unlock_after=wall_jump,locked_after,unlock_after
        self.reset_commands=reset_commands
        self.iterations = 0
        self.captures = 0
        self.body_calls = 0
        self.header_calls = 0
        self.cpu_calls = 0
        self.process_self_instances = 0
        self.clock = 100.0
        self.events = []
        self.closed = 0
        self.output = io.StringIO()

    def run(self):
        harness = self
        commands = queue.Queue()
        commands.put(dict(action="start", paused=False,
                          confirmation=confirmation() if self.confirmed else None,
                          calibration=CALIBRATION))
        for command in self.reset_commands:commands.put(worker.parse_command(json.dumps(command)))
        class Event:
            def __init__(self):
                self.value = False
                harness.events.append(self)
            def set(self): self.value = True
            def clear(self): self.value = False
            def is_set(self): return self.value
            def wait(self, delay):
                harness.clock += delay
                return self.value
        class Parent:
            def create_time(self): return 20.0
            def is_running(self):
                harness.iterations += 1
                if harness.capture_gap and harness.iterations == 4:
                    harness.clock += 6.0
                return harness.iterations <= harness.loops
        class SelfProcess:
            def cpu_percent(self, interval):
                harness.cpu_calls += 1
                return 0.0 if harness.cpu_calls == 1 else 12.5
            def memory_info(self): return SimpleNamespace(rss=123456)
        def process(pid=None):
            if pid is None:
                harness.process_self_instances += 1
                return SelfProcess()
            return Parent()
        class NativeReads:
            def GetCursorPos(self, pointer):
                pointer._obj.x, pointer._obj.y = ((200, 200) if harness.hover else (-1000, -1000))
                return True
        class Windows:
            def __init__(self): self.u = NativeReads()
            def discover(self): return [SimpleNamespace(hwnd=123, minimized=False)]
            def pointer_over_window(self, hwnd): return harness.pointer_target
        class Capture:
            def __init__(self, hwnd, backend): pass
            def capture(self):
                harness.captures += 1
                current = dict(WINDOW)
                if harness.move_window and harness.captures >= 4:
                    current["client_screen"] = [1, 0]
                image = Image.new("RGB", (400, 400), (30, 30, 31))
                if harness.new_message and harness.captures >= 5:
                    image.putpixel((200, 200), (47, 47, 48))
                return image, dict(window=current, duration_ms=1.25,target_foreground_after=harness.foreground_after)
            def close(self): harness.closed += 1
        class OCR:
            def __init__(self, resource_root, provider): pass
            def recognize(self, image, roi):
                if tuple(roi) == tuple(CALIBRATION["header"]):
                    harness.header_calls += 1
                    if harness.ocr_error:
                        raise RuntimeError("PRIVATE_OCR_BODY_MUST_NOT_APPEAR")
                    if harness.pause_in_header and harness.header_calls == 3:
                        harness.events[1].set()
                    return [dict(text="wrong group" if harness.wrong_header else TARGET_GROUP,
                                 box=[120, 10, 180, 25], score=.99)]
                harness.body_calls += 1
                if tuple(roi) != tuple(CALIBRATION["message"]):
                    raise AssertionError("body_OCR_ROI_mismatch")
                return []
        def extract(*args, **kwargs):
            messages = () if harness.empty_body else (message("BASELINE_PRIVATE_A", 90), message("BASELINE_PRIVATE_B", 140))
            if harness.new_message and harness.captures >= 5:
                messages += (message("@Ativia SYNTHETIC_NEW_BODY", 200),)
            return SimpleNamespace(messages=messages)
        arguments = SimpleNamespace(parent_pid=789, parent_start=20.0, backend="win32_printwindow",
                                    resource_root="synthetic-resource", session="synthetic-worker-session")
        with ExitStack() as stack:
            def desktop():
                locked=self.locked_after and self.iterations>=self.locked_after and not (self.unlock_after and self.iterations>=self.unlock_after)
                return DesktopStatus(not locked,'desktop_unavailable' if locked else 'desktop_available')
            stack.enter_context(patch.object(worker,'probe_desktop',side_effect=desktop))
            stack.enter_context(patch.dict(worker.os.environ, {}, clear=True))
            stack.enter_context(patch.object(worker.psutil, "Process", side_effect=process))
            stack.enter_context(patch.object(worker.queue, "Queue", return_value=commands))
            stack.enter_context(patch.object(worker.threading, "Thread"))
            stack.enter_context(patch.object(worker.threading, "Event", Event))
            stack.enter_context(patch.object(worker, "Win32Windows", Windows))
            stack.enter_context(patch.object(worker, "WindowCaptureProvider", Capture))
            stack.enter_context(patch.object(worker, "LocalOCR", OCR))
            stack.enter_context(patch.object(worker, "scan_dark_bubbles", return_value=SimpleNamespace(detections=(), metadata=())))
            stack.enter_context(patch.object(worker, "extract_messages", side_effect=extract))
            stack.enter_context(patch.object(worker.sys, "stdout", self.output))
            stack.enter_context(patch.object(worker.time, "time", return_value=1000.0))
            stack.enter_context(patch.object(worker.time, "monotonic", side_effect=lambda: self.clock))
            # Increase wall timestamp per frame, as the real clock does; other time
            # uses retain a fresh confirmation age without advancing the loop budget.
            stack.enter_context(patch.object(worker.time, "time", side_effect=lambda: 1000.0 + self.captures / 10 + (self.wall_jump if self.captures>=4 else 0)))
            worker.run(arguments)
        return [json.loads(line) for line in self.output.getvalue().splitlines()]


class CommandAndConfirmationTests(unittest.TestCase):
    def test_worker_echoes_reset_ack_before_any_new_binding(self):
        commands=(dict(action='resume',reset_id='resume-1'),
                  dict(action='confirm',reset_id='confirm-2',confirmation=confirmation()))
        packets=WorkerHarness(reset_commands=commands).run()
        acks=[(i,p['reset_id']) for i,p in enumerate(packets) if p['stage']=='unbound' and p.get('reset_id')]
        self.assertEqual([value for _,value in acks],['resume-1','confirm-2'])
        bound=next(i for i,p in enumerate(packets) if p['stage']=='group_bound')
        self.assertLess(acks[-1][0],bound)
        for command in (dict(action='pause',reset_id=''),dict(action='resume',reset_id=123),dict(action='start',reset_id='x')):
            with self.assertRaises(ValueError):worker.parse_command(json.dumps(command))
    def test_native_tuple_coordinates_match_json_confirmation_but_moved_window_does_not(self):
        value=confirmation()
        self.assertIsInstance(WINDOW['client_rect'],tuple)
        self.assertIsInstance(value['window']['client_rect'],list)
        with patch.object(worker.time,'time',return_value=1000):
            self.assertTrue(worker.matches_confirmation(WINDOW,value))
            moved=dict(WINDOW,client_screen=(1,0))
            self.assertFalse(worker.matches_confirmation(moved,value))

    def test_confirmation_requires_fresh_finite_nonfuture_timestamp_and_geometry(self):
        with patch.object(worker.time, "time", return_value=1000):
            self.assertTrue(worker.matches_confirmation(WINDOW, confirmation()))
            for value in (float("inf"), float("nan"), 1001, -1000, "invalid"):
                self.assertFalse(worker.matches_confirmation(WINDOW, confirmation(confirmed_at=value)))
            self.assertFalse(worker.matches_confirmation(WINDOW, confirmation(bottom_confirmed=False)))
            changed = dict(WINDOW, client_screen=[1, 0])
            self.assertFalse(worker.matches_confirmation(changed, confirmation()))

    def test_ipc_rejects_action_unknown_fields_and_oversize(self):
        self.assertEqual(worker.parse_command('{"action":"pause"}')["action"], "pause")
        for line in ('{"action":"send"}', '{"action":"start","api_key":"x"}', "x" * (worker.MAX_PACKET + 1)):
            with self.assertRaises(ValueError):
                worker.parse_command(line)


class OfflineWorkerTests(unittest.TestCase):
    def test_no_confirmation_means_header_only(self):
        harness = WorkerHarness(confirmed=False)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 0)
        self.assertNotIn("group_bound", [packet["stage"] for packet in packets])
        self.assertNotIn("listener_ready", [packet["stage"] for packet in packets])

    def test_wrong_group_means_header_only(self):
        harness = WorkerHarness(wrong_header=True)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 0)
        self.assertFalse(any(packet["stage"] == "event" for packet in packets))

    def test_baseline_then_ready_then_only_new_stable_event(self):
        harness = WorkerHarness(new_message=True)
        packets = harness.run()
        stages = [packet["stage"] for packet in packets]
        self.assertLess(stages.index("group_bound"), stages.index("baseline_complete"))
        self.assertLess(stages.index("baseline_complete"), stages.index("listener_ready"))
        events = [packet["event"] for packet in packets if packet["stage"] == "event"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "@Ativia SYNTHETIC_NEW_BODY")
        self.assertEqual(harness.body_calls, 2)
        metadata = json.dumps([packet for packet in packets if packet["stage"] != "event"])
        self.assertNotIn("SYNTHETIC_NEW_BODY", metadata)
        self.assertNotIn("BASELINE_PRIVATE", metadata)

    def test_empty_unchanged_frame_reuses_successful_empty_ocr(self):
        harness = WorkerHarness(empty_body=True)
        harness.run()
        self.assertEqual(harness.body_calls, 1)

    def test_pause_during_header_does_not_admit_body_ocr(self):
        harness = WorkerHarness(pause_in_header=True)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 0)
        self.assertNotIn("listener_ready", [packet["stage"] for packet in packets])

    def test_hover_revokes_bottom_and_never_admits_body(self):
        harness = WorkerHarness(hover=True)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 0)
        self.assertTrue(any(packet.get("reason") == "manual_interaction_bottom_uncertain" for packet in packets))
        self.assertEqual(sum(packet["stage"] == "group_bound" for packet in packets), 1)

    def test_own_overlay_at_same_screen_coordinates_does_not_revoke_binding(self):
        harness = WorkerHarness(hover=True, pointer_target=False)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 1)
        self.assertIn("listener_ready", [packet["stage"] for packet in packets])
        self.assertFalse(any(packet.get("reason") == "manual_interaction_bottom_uncertain" for packet in packets))

    def test_changed_flag_and_message_count_report_no_pixels_or_text(self):
        packets = WorkerHarness(new_message=True).run()
        frames = [packet for packet in packets if packet["stage"] == "frame"]
        self.assertEqual(sum(packet["changed"] for packet in frames), 2)
        self.assertEqual(frames[0]["message_count"], 2)
        self.assertEqual(frames[-1]["message_count"], 3)
        self.assertFalse(any("sha256" in key or "text" in key or "image" in key for packet in frames for key in packet))

    def test_target_gaining_focus_during_capture_blocks_body_ocr(self):
        harness=WorkerHarness(foreground_after=True)
        packets=harness.run()
        self.assertEqual(harness.body_calls,0)
        self.assertTrue(any(p.get('reason')=='manual_interaction_bottom_uncertain' for p in packets))

    def test_long_capture_gap_revokes_confirmation(self):
        harness = WorkerHarness(capture_gap=True)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 1)
        self.assertTrue(any(packet.get("reason") == "capture_gap" for packet in packets))
        self.assertEqual(sum(packet["stage"] == "group_bound" for packet in packets), 1)

    def test_window_move_revokes_binding_and_prevents_further_body_ocr(self):
        harness = WorkerHarness(move_window=True)
        packets = harness.run()
        self.assertEqual(harness.body_calls, 1)
        self.assertTrue(any(packet.get("reason") == "window_geometry_or_confirmation_changed" for packet in packets))

    def test_provider_error_text_never_reaches_metadata(self):
        harness = WorkerHarness(ocr_error=True, loops=3)
        packets = harness.run()
        self.assertNotIn("PRIVATE_OCR_BODY", harness.output.getvalue())
        self.assertTrue(any(packet.get("reason") == "RuntimeError" for packet in packets))

    def test_cpu_uses_one_primed_process_object_with_declared_scope(self):
        harness = WorkerHarness()
        packets = harness.run()
        frames = [packet for packet in packets if packet["stage"] == "frame"]
        self.assertTrue(frames)
        self.assertEqual(harness.process_self_instances, 1)
        self.assertEqual(harness.cpu_calls, len(frames) + 1)
        self.assertTrue(all(packet["cpu_percent"] == 12.5 for packet in frames))
        self.assertTrue(all(packet["cpu_scope"] == "worker_process_one_logical_core_100_percent" for packet in frames))


class PointerHitTests(unittest.TestCase):
    def window(self, hit, root, cursor_ok=True):
        windows = Win32Windows.__new__(Win32Windows)
        def read_cursor(pointer):
            pointer._obj.x, pointer._obj.y = 200, 200
            return cursor_ok
        windows.u = SimpleNamespace(GetCursorPos=Mock(side_effect=read_cursor),
                                    WindowFromPoint=Mock(return_value=hit),
                                    GetAncestor=Mock(return_value=root))
        return windows

    def test_child_of_wechat_is_actual_hover(self):
        windows = self.window(124, 123)
        self.assertTrue(windows.pointer_over_window(123))
        windows.u.GetAncestor.assert_called_once_with(124, 2)

    def test_overlay_root_is_not_target_even_at_identical_coordinates(self):
        windows = self.window(999, 998)
        self.assertFalse(windows.pointer_over_window(123))
        windows.u.GetAncestor.assert_called_once_with(999, 2)

    def test_no_hit_is_not_wechat_and_failed_queries_are_uncertain(self):
        windows = self.window(0, 0)
        self.assertFalse(windows.pointer_over_window(123))
        windows.u.GetAncestor.assert_not_called()
        self.assertTrue(self.window(124, 0).pointer_over_window(123))
        self.assertTrue(self.window(124, 123, cursor_ok=False).pointer_over_window(123))


if __name__ == "__main__":
    unittest.main()
