"""Capture safety with fake SDK/native APIs; no window, desktop grab or DLL is used."""
from contextlib import contextmanager
from dataclasses import replace
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

from integrations.wechat.visual import capture
from integrations.wechat.visual.window import WindowSnapshot


def snapshot(**changes):
    value = WindowSnapshot(123, 456, 100.0, "Weixin.exe", "synthetic.exe", "synthetic", "synthetic",
                           (-50, 20, 400, 300), (0, 0, 400, 300), (-50, 20), 168, False, False)
    return replace(value, **changes)


def pixels(size=(400, 300)):
    image = Image.new("RGB", size, (30, 30, 31))
    ImageDraw.Draw(image).rectangle((20, 20, 80, 80), fill=(220, 220, 220))
    return image


class FakeWindows:
    def __init__(self, states, foreground=None):
        self.states = list(states)
        self.last = self.states[-1]
        self.u = SimpleNamespace(GetForegroundWindow=Mock(side_effect=foreground) if foreground is not None else Mock(return_value=0))
    def snapshot(self, hwnd):
        if self.states:
            self.last = self.states.pop(0)
        return self.last


@contextmanager
def provider(states=None, backend="win32_printwindow", foreground=None):
    states = [snapshot(), snapshot(), snapshot()] if states is None else states
    windows = FakeWindows(states, foreground)
    with patch.object(capture, "Win32Windows", return_value=windows):
        yield capture.WindowCaptureProvider(123, backend), windows


@contextmanager
def fake_maa(*, privacy_ok=True, raw_ok=True):
    calls = []
    controller = Mock()
    controller.set_screenshot_use_raw_size.return_value = raw_ok
    controller.post_connection.return_value = SimpleNamespace(done=True, succeeded=True)
    array = np.asarray(pixels())[:, :, ::-1].copy()
    controller.post_screencap.return_value = SimpleNamespace(done=True, succeeded=True, get=lambda: array)
    constructor = Mock(return_value=controller)
    class Tasker:
        pass
    def setter(name):
        def apply(value):
            calls.append((name, value))
            return privacy_ok
        return staticmethod(apply)
    for name in ("set_log_dir", "set_save_draw", "set_save_on_error", "set_debug_mode", "set_stdout_level"):
        setattr(Tasker, name, setter(name))
    modules = {name: ModuleType(name) for name in ("maa", "maa.controller", "maa.define", "maa.tasker")}
    modules["maa"].__path__ = []
    modules["maa.controller"].Win32Controller = constructor
    modules["maa.define"].MaaWin32ScreencapMethodEnum = SimpleNamespace(GDI=16)
    modules["maa.define"].MaaWin32InputMethodEnum = SimpleNamespace(Null=0)
    modules["maa.define"].LoggingLevelEnum = SimpleNamespace(Off=0)
    modules["maa.tasker"].Tasker = Tasker
    with patch.dict(sys.modules, modules):
        yield controller, constructor, calls


class CaptureGuardTests(unittest.TestCase):
    def test_only_explicit_safe_backends_allowed_before_any_native_initialization(self):
        with patch.object(capture, "Win32Windows") as windows:
            for backend in ("All", "auto", "FramePoolWithPseudoMinimize", "PrintWindowWithPseudoMinimize", "click", ""):
                with self.assertRaisesRegex(ValueError, "unsafe_or_unknown_backend"):
                    capture.WindowCaptureProvider(123, backend)
            windows.assert_not_called()

    def test_minimized_constructor_refuses_before_sdk_controller(self):
        with fake_maa() as (_, constructor, _):
            with patch.object(capture, "Win32Windows", return_value=FakeWindows([snapshot(minimized=True)])):
                with self.assertRaisesRegex(capture.CaptureUnavailable, "minimized_pause"):
                    capture.WindowCaptureProvider(123, "maa_gdi")
            constructor.assert_not_called()

    def test_minimized_capture_refuses_before_reading_pixels(self):
        with provider([snapshot(), snapshot(minimized=True)]) as (camera, _):
            with patch.object(camera, "_printwindow") as native:
                with self.assertRaisesRegex(capture.CaptureUnavailable, "minimized_pause"):
                    camera.capture()
                native.assert_not_called()

    def test_pid_or_start_time_change_refuses_before_pixels(self):
        for changes in (dict(pid=987), dict(process_started=101.0)):
            with provider([snapshot(), snapshot(**changes)]) as (camera, _):
                with patch.object(camera, "_printwindow") as native:
                    with self.assertRaisesRegex(capture.CaptureUnavailable, "window_identity_changed"):
                        camera.capture()
                    native.assert_not_called()

    def test_move_resize_and_dpi_before_capture_require_new_provider(self):
        for changes in (dict(client_screen=(-49, 20)), dict(client_rect=(0, 0, 401, 300)), dict(dpi=144)):
            with provider([snapshot(), snapshot(**changes)]) as (camera, _):
                with patch.object(camera, "_printwindow") as native:
                    with self.assertRaisesRegex(capture.CaptureUnavailable, "geometry_changed_rebaseline"):
                        camera.capture()
                    native.assert_not_called()

    def test_target_changes_during_capture_are_rejected(self):
        for changes in (dict(pid=987), dict(process_started=101.0), dict(client_screen=(-49, 20)),
                        dict(client_rect=(0, 0, 401, 300)), dict(dpi=144), dict(minimized=True)):
            with provider([snapshot(), snapshot(), snapshot(**changes)]) as (camera, _):
                with patch.object(camera, "_printwindow", return_value=pixels()):
                    with self.assertRaisesRegex(capture.CaptureUnavailable, "geometry_changed_rebaseline"):
                        camera.capture()

    def test_raw_size_mismatch_rejected(self):
        with provider() as (camera, _):
            with patch.object(camera, "_printwindow", return_value=pixels((200, 150))):
                with self.assertRaisesRegex(capture.CaptureUnavailable, "unverified_image_coordinate_mapping"):
                    camera.capture()

    def test_blank_nonempty_image_is_not_success(self):
        with provider() as (camera, _):
            with patch.object(camera, "_printwindow", return_value=Image.new("RGB", (400, 300), "black")):
                with self.assertRaisesRegex(capture.CaptureUnavailable, "blank_image"):
                    camera.capture()

    def test_success_reports_one_to_one_mapping_without_input_calls(self):
        with provider(foreground=[0, 0]) as (camera, windows):
            with patch.object(camera, "_printwindow", return_value=pixels()):
                image, metadata = camera.capture()
            self.assertEqual(image.size, (400, 300))
            self.assertEqual(metadata["client_to_image_scale"], [1, 1])
            self.assertFalse(metadata["foreground_changed"])
            self.assertEqual(set(vars(windows.u)), {"GetForegroundWindow"})

    def test_background_capture_reports_external_foreground_change(self):
        with provider(foreground=[0, 999]) as (camera, _):
            with patch.object(camera, "_printwindow", return_value=pixels()):
                _, metadata = camera.capture()
            self.assertTrue(metadata["foreground_changed"])

    def test_all_input_entrypoints_always_deny_even_without_initialization(self):
        camera = capture.WindowCaptureProvider.__new__(capture.WindowCaptureProvider)
        for name in ("click", "input_text", "press_key", "paste", "send"):
            with self.assertRaisesRegex(PermissionError, "wechat_input_disabled"):
                getattr(camera, name)("synthetic")


class ForegroundCaptureTests(unittest.TestCase):
    def test_foreground_required_without_activation(self):
        with provider(backend="foreground_crop", foreground=[0]) as (camera, _):
            with patch("PIL.ImageGrab.grab") as grab:
                with self.assertRaisesRegex(capture.CaptureUnavailable, "foreground_required"):
                    camera.capture()
                grab.assert_not_called()

    def test_foreground_rechecked_after_snapshot_before_grab(self):
        state = snapshot(foreground=True)
        with provider([state, state, state], "foreground_crop", [999]) as (camera, _):
            with patch("PIL.ImageGrab.grab") as grab:
                with self.assertRaisesRegex(capture.CaptureUnavailable, "foreground_required"):
                    camera.capture()
                grab.assert_not_called()

    def test_lost_foreground_during_grab_refuses_pixels(self):
        state = snapshot(foreground=True)
        with provider([state, state, snapshot(foreground=False)], "foreground_crop", [123, 999]) as (camera, _):
            with patch("PIL.ImageGrab.grab", return_value=pixels()):
                with self.assertRaisesRegex(capture.CaptureUnavailable, "foreground_changed_during_capture"):
                    camera.capture()

    def test_foreground_grab_uses_screen_client_coordinates(self):
        state = snapshot(foreground=True)
        with provider([state, state, state], "foreground_crop", [123, 123]) as (camera, _):
            with patch("PIL.ImageGrab.grab", return_value=pixels()) as grab:
                camera.capture()
            grab.assert_called_once_with(bbox=(-50, 20, 350, 320), all_screens=True)


class MaaCaptureTests(unittest.TestCase):
    def test_gdi_controller_has_null_mouse_keyboard_raw_size_and_privacy(self):
        with fake_maa() as (controller, constructor, privacy):
            with provider(backend="maa_gdi") as (camera, _):
                image, metadata = camera.capture()
                self.assertEqual(image.getpixel((30, 30)), (220, 220, 220))
                self.assertEqual(metadata["backend"], "maa_gdi")
                camera.close()
                self.assertIsNone(camera._controller)
            constructor.assert_called_once_with(123, screencap_method=16, mouse_method=0, keyboard_method=0)
            controller.set_screenshot_use_raw_size.assert_called_once_with(True)
            self.assertEqual(privacy, [("set_log_dir", ""), ("set_save_draw", False), ("set_save_on_error", False),
                                       ("set_debug_mode", False), ("set_stdout_level", 0)])

    def test_rejected_privacy_option_never_creates_controller(self):
        with fake_maa(privacy_ok=False) as (_, constructor, _):
            with self.assertRaisesRegex(capture.CaptureUnavailable, "capture_privacy_options_rejected"):
                with provider(backend="maa_gdi"): pass
            constructor.assert_not_called()

    def test_rejected_raw_size_option_never_connects(self):
        with fake_maa(raw_ok=False) as (controller, _, _):
            with self.assertRaisesRegex(capture.CaptureUnavailable, "raw_size_option_rejected"):
                with provider(backend="maa_gdi"): pass
            controller.post_connection.assert_not_called()

    def test_empty_sdk_image_rejected(self):
        with fake_maa() as (controller, _, _):
            controller.post_screencap.return_value = SimpleNamespace(done=True, succeeded=True, get=lambda: np.empty((0, 0, 3), dtype=np.uint8))
            with provider(backend="maa_gdi") as (camera, _):
                with self.assertRaisesRegex(capture.CaptureUnavailable, "empty_image"):
                    camera.capture()

    def test_failed_or_timed_out_job_is_not_success(self):
        with self.assertRaisesRegex(capture.CaptureUnavailable, "capture_job_failed"):
            capture.wait_job(SimpleNamespace(done=True, succeeded=False))
        with patch.object(capture.time, "monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(capture.CaptureUnavailable, "capture_job_timeout"):
                capture.wait_job(SimpleNamespace(done=False, succeeded=False), timeout=1)


class NativeBitmapTests(unittest.TestCase):
    def test_partial_getdibits_rejected_and_owned_gdi_resources_released(self):
        def function(result):
            return Mock(return_value=result)
        u = SimpleNamespace(GetDC=function(10), ReleaseDC=function(1), PrintWindow=function(1))
        g = SimpleNamespace(CreateCompatibleDC=function(20), CreateCompatibleBitmap=function(30),
                            SelectObject=function(40), DeleteObject=function(1), DeleteDC=function(1),
                            GetDIBits=function(299))
        camera = capture.WindowCaptureProvider.__new__(capture.WindowCaptureProvider)
        camera.windows = SimpleNamespace(u=u)
        with patch.object(capture.ctypes, "WinDLL", return_value=g):
            with self.assertRaisesRegex(capture.CaptureUnavailable, "GetDIBits_incomplete"):
                camera._printwindow(snapshot())
        g.DeleteObject.assert_called_once_with(30)
        g.DeleteDC.assert_called_once_with(20)
        u.ReleaseDC.assert_called_once_with(123, 10)


if __name__ == "__main__":
    unittest.main()
