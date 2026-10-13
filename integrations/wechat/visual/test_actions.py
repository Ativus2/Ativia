"""Real input backend exclusively for task-owned, nonce-labelled local fixtures.

Do not import this backend from the visual listener. The normal capture module
uses Null mouse/keyboard methods. No clipboard or global SendInput is used here.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from .safety import OwnedWindowRegistry, SafetyError, TestWindowTarget, WindowState


def read_fixture_json(path: Path, attempts: int = 8):
    """Bounded read-only reobservation of an atomically replaced local file.

    Windows may transiently deny opening the destination during replacement.
    This retries observation only, never a click, text input, or submission.
    """
    if type(attempts) is not int or not 1 <= attempts <= 8:
        raise ValueError("fixture read attempts must be between 1 and 8")
    for index in range(attempts):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise SafetyError("fixture state is not an object")
            return data
        except (PermissionError, FileNotFoundError, json.JSONDecodeError):
            if index + 1 == attempts:
                raise
            time.sleep(.015)


def recognize_blue_input(image):
    """Locate the fixture's blue rectangular border from captured pixels."""
    import numpy as np
    pixels = np.asarray(image.convert("RGB"))
    mask = ((pixels[:, :, 0] >= 25) & (pixels[:, :, 0] <= 90)
            & (pixels[:, :, 1] >= 70) & (pixels[:, :, 1] <= 145)
            & (pixels[:, :, 2] >= 155) & (pixels[:, :, 2] <= 235))
    # ClearType glyph fringes can share blue pixels. Require long border lines,
    # rather than treating every colored pixel in the window as part of the ROI.
    ys = np.flatnonzero(mask.sum(axis=1) >= max(150, image.width * .4))
    xs = np.flatnonzero(mask.sum(axis=0) >= max(80, image.height * .25))
    if not len(xs) or not len(ys):
        raise SafetyError("fixture blue input border was not observed")
    left, top, right, bottom = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    if right - left < 150 or bottom - top < 80:
        raise SafetyError("fixture input border is too small")
    edges = [mask[top:min(top+4, bottom), left:right].any(axis=0).mean(),
             mask[max(top, bottom-4):bottom, left:right].any(axis=0).mean(),
             mask[top:bottom, left:min(left+4, right)].any(axis=1).mean(),
             mask[top:bottom, max(left, right-4):right].any(axis=1).mean()]
    if min(edges) < .95:
        raise SafetyError("fixture input border is not a complete rectangle")
    return (left, top, right, bottom)


class OwnedFixtureBackend:
    """Public MaaFramework SendMessage input guarded by native owned identity."""
    def __init__(self, registry: OwnedWindowRegistry, target: TestWindowTarget, directory: Path,
                 method: str = "SendMessage"):
        if method not in ("SendMessage", "PostMessage"):
            raise SafetyError("only directed message input is allowed for this fixture")
        self.registry, self.target, self.directory = registry, target, Path(directory)
        self.method = method
        self.controller = None
        self.after_input = lambda: None
        self.after_click = lambda: None
        self.calls = []
        self._last_text = None
        self._submits_before = None
        self._action_gate = None
        from .window import Win32Windows
        self.windows = Win32Windows()
        self._identity(require_focus=False)
        from maa.controller import Win32Controller
        from maa.define import MaaWin32ScreencapMethodEnum as S, MaaWin32InputMethodEnum as I
        from .capture import wait_job
        self.controller = Win32Controller(target.hwnd, screencap_method=S.GDI,
                                          mouse_method=getattr(I, method), keyboard_method=getattr(I, method))
        self.controller.set_screenshot_use_raw_size(True)
        wait_job(self.controller.post_connection())

    def set_action_gate(self, gate, epoch):
        self._action_gate = (gate, epoch)

    def _input_admission(self):
        self._identity()
        if self._action_gate is None or not self._action_gate[0].allows(self._action_gate[1]):
            raise SafetyError("fixture action admission was cancelled")

    def _identity(self, require_focus=True):
        if self.target.kind != "owned_test" or not self.registry.owns(self.target):
            raise SafetyError("unregistered fixture; input rejected")
        native = self.windows.snapshot(self.target.hwnd)
        actual = (native.hwnd, native.pid, str(native.process_started), native.title)
        expected = (self.target.hwnd, self.target.pid, self.target.process_start,
                    "Ativia Visual Test " + self.target.nonce)
        if actual != expected or native.process_name.casefold() in ("weixin.exe", "wechat.exe"):
            raise SafetyError("fixture native identity or nonce mismatch")
        if native.minimized or (require_focus and not native.foreground):
            raise SafetyError("fixture is minimized or unfocused")
        return native

    def _state(self):
        path = self.directory / "fixture.json"
        if time.time() - path.stat().st_mtime > 2:
            raise SafetyError("fixture state is stale")
        state = read_fixture_json(path)
        if (state.get("pid"), state.get("hwnd"), state.get("nonce")) != (self.target.pid, self.target.hwnd, self.target.nonce):
            raise SafetyError("fixture state ownership mismatch")
        if state.get("closed"):
            raise SafetyError("fixture is closed")
        return state

    def activate_for_test(self, wait_seconds=600):
        """Wait for user-selected fixture focus; never steal foreground focus."""
        if self._identity(require_focus=False).foreground:
            return True
        print(json.dumps(dict(event='FOCUS_NEEDED', title='Ativia Visual Test '+self.target.nonce,
                              hwnd=self.target.hwnd, directory=str(self.directory))), flush=True)
        # External test setup may activate this exact owned window. Never send
        # global shortcut keys or bypass the action-time foreground checks.
        end = time.monotonic() + wait_seconds
        while time.monotonic() < end:
            if self.windows.snapshot(self.target.hwnd).foreground:
                return True
            time.sleep(.05)
        raise SafetyError("cannot focus the task-owned test window")

    def capture(self):
        from .capture import wait_job
        from PIL import Image
        native = self._identity()
        array = wait_job(self.controller.post_screencap()).get()
        image = Image.fromarray(array[:, :, ::-1].copy())
        if image.size != native.client_size:
            raise SafetyError("SDK image is not native client size")
        self._identity()
        return image

    def mapping(self, image=None):
        native = self._identity()
        state = self._state()
        image = self.capture() if image is None else image
        border = recognize_blue_input(image)
        sx, sy = native.client_size[0] / state["window"][2], native.client_size[1] / state["window"][3]
        x, y, width, height = state["input_client"]
        expected = (round(x*sx), round(y*sy), round((x+width)*sx), round((y+height)*sy))
        if max(abs(a-b) for a, b in zip(border, expected)) > 3:
            raise SafetyError("pixel border does not match verified DPI mapping")
        return dict(input_rect=border, logical_input=state["input_client"],
                    logical_to_client_scale=[sx, sy], image_to_client_scale=[1, 1],
                    client_size=list(native.client_size), image_size=list(image.size), dpi=native.dpi)

    def inspect(self, target):
        if target != self.target:
            raise SafetyError("unexpected target")
        native = self._identity(require_focus=False)
        state = self._state()
        # Geometry is obtained from the fixture; the action runner separately
        # checks the pixel-recognized border against this mapping before input.
        sx, sy = native.client_size[0] / state["window"][2], native.client_size[1] / state["window"][3]
        x, y, width, height = state["input_client"]
        rect = (round(x*sx), round(y*sy), round((x+width)*sx), round((y+height)*sy))
        return WindowState(native.hwnd, native.pid, str(native.process_started), state["nonce"],
                           native.process_name, True, native.foreground, state["text"], native.client_size, rect)

    def click(self, target, x, y):
        from .capture import wait_job
        self._input_admission()
        mapping = self.mapping()
        left, top, right, bottom = mapping["input_rect"]
        if not left < x < right or not top < y < bottom:
            raise SafetyError("click outside screenshot-recognized input region")
        self.calls.append("click")
        self._input_admission()
        wait_job(self.controller.post_click(x, y))
        end = time.monotonic() + 2
        while time.monotonic() < end and not self._state()["input_focused"]:
            self._identity()
            time.sleep(.05)
        if not self._state()["input_focused"]:
            raise SafetyError("SDK click did not focus the observed editable region")
        self.after_click()

    def input_text(self, target, text):
        from .capture import wait_job
        self._input_admission()
        if not self._state()["input_focused"]:
            raise SafetyError("fixture input is not focused")
        self.calls.append("input_text")
        # MessageInput emits literal WM_CHAR: Qt drops LF instead of creating a
        # paragraph. Use the public VK_RETURN down/up methods between lines.
        for index, line in enumerate(text.split("\n")):
            self._input_admission()
            if not self._state()["input_focused"]:
                raise SafetyError("fixture input focus changed during text entry")
            if index:
                self.calls.append("key_down:13")
                wait_job(self.controller.post_key_down(0x0D))
                # A key-up is cleanup for the already admitted down, even if
                # pause arrived meanwhile; still verify the owned native PID.
                self._identity(require_focus=False)
                self.calls.append("key_up:13")
                wait_job(self.controller.post_key_up(0x0D))
            if line:
                self._input_admission()
                wait_job(self.controller.post_input_text(line), timeout=max(5, len(line)*.08+2))
        end = time.monotonic() + 2
        while time.monotonic() < end and self._state()["text"] != text:
            self._identity()
            time.sleep(.05)
        self._last_text = text
        self.after_input()

    def submit(self, target):
        from .capture import wait_job
        self._input_admission()
        native = self._identity()
        state = self._state()
        if state["text"] != self._last_text:
            raise SafetyError("content changed before fixture submission")
        sx, sy = native.client_size[0] / state["window"][2], native.client_size[1] / state["window"][3]
        x, y, width, height = state["submit_client"]
        self._submits_before = state["submits"]
        self.calls.append("submit")
        self._input_admission()
        wait_job(self.controller.post_click(round((x+width/2)*sx), round((y+height/2)*sy)))

    def observe_submission(self, target, action_id):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            self._identity()
            state = self._state()
            if state["submits"] == self._submits_before + 1:
                return state["last_submitted"] == self._last_text
            time.sleep(.05)
        return None

    def close(self):
        self.controller = None
