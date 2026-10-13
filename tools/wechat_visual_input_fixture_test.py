"""M6 acceptance: public SDK input only into windows this runner creates.

Run with the isolated visual worker Python. Fixture GUI uses the existing main
venv Python, whose dependencies are not modified. No WeChat target is discovered.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))

from integrations.wechat.visual.safety import (
    ActionLedger, EpochGate, ExclusiveLease, OwnedWindowRegistry, SafeActionController,
)
from integrations.wechat.visual.test_actions import OwnedFixtureBackend, read_fixture_json


class OwnedChild:
    """Associate the Python venv launcher's verified descendant with its Popen."""
    def __init__(self, launcher, pid, started):
        self.launcher, self.pid, self.started = launcher, pid, started
    def poll(self):
        import psutil
        if self.launcher.poll() is not None:
            return self.launcher.returncode
        try:
            process = psutil.Process(self.pid)
            return None if process.is_running() and process.create_time() == self.started else 0
        except psutil.Error:
            return 0


class Fixture:
    def __init__(self, directory, python, script, registry, method):
        import psutil
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.nonce = registry.issue_nonce()
        environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        environment.pop("QT_QPA_PLATFORM", None)
        self.log = open(self.directory / "fixture.log", "w", encoding="utf-8")
        self.process = subprocess.Popen([str(python), "-B", str(script), "--directory", str(self.directory), "--nonce", self.nonce],
                                        stdout=self.log, stderr=subprocess.STDOUT, env=environment,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.backend = None
        try:
            deadline = time.monotonic() + 12
            while not (self.directory / "fixture.json").exists():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("owned fixture did not become ready")
                time.sleep(.05)
            state = self.state()
            descendants = [self.process.pid] + [p.pid for p in psutil.Process(self.process.pid).children(recursive=True)]
            if state["pid"] not in descendants or state["nonce"] != self.nonce:
                raise RuntimeError("fixture PID is not this runner's child")
            started = psutil.Process(state["pid"]).create_time()
            owned = OwnedChild(self.process, state["pid"], started)
            registry.record_created_process(owned, str(started))
            self.target = registry.register_window(state["hwnd"], state["pid"], str(started), self.nonce)
            self.backend = OwnedFixtureBackend(registry, self.target, self.directory, method)
            self.backend.activate_for_test()
        except BaseException:
            self.close()
            raise

    def state(self):
        return read_fixture_json(self.directory / "fixture.json")

    def close(self):
        if self.backend is not None:
            self.backend.close()
        if self.process.poll() is None:
            (self.directory / "stop.fixture").write_text("normal close", encoding="utf-8")
            self.process.wait(timeout=8)
        self.log.close()

    def reset_synthetic_input(self):
        """Only test scaffolding: clear our own prior synthetic text, never type."""
        (self.directory / "reset.fixture").write_text(self.nonce, encoding="utf-8")
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if self.state()["text"] == "":
                return
            time.sleep(.05)
        raise RuntimeError("fixture synthetic reset failed")

    def show_owned_dummy(self, registry):
        nonce = registry.issue_nonce()
        (self.directory / "dummy.fixture").write_text(json.dumps(dict(owner_nonce=self.nonce, nonce=nonce)), encoding="utf-8")
        end = time.monotonic() + 3
        while time.monotonic() < end:
            state = self.state()
            if state.get("dummy_nonce") == nonce:
                if not hasattr(self, 'dummy_target'):
                    self.dummy_target = registry.register_window(state["dummy_hwnd"], self.target.pid, self.target.process_start, nonce)
                if self.backend.windows.snapshot(self.dummy_target.hwnd).foreground:
                    return
            time.sleep(.05)
        raise RuntimeError("owned dummy did not receive foreground focus")


def record_image(backend, directory, name):
    image = backend.capture()
    image.save(directory / (name + ".png"))
    return image


def run(args):
    from PIL import ImageChops
    from importlib.metadata import version
    run_directory = args.output / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_directory.mkdir(parents=True)
    registry = OwnedWindowRegistry()
    fixtures = []
    rows = []
    report = dict(status="RUNNING", sdk=version("maafw"), method=args.method, cases=rows,
                  real_wechat_clicks=0, real_wechat_keys=0, real_wechat_pastes=0, real_wechat_sends=0,
                  clipboard_writes=0, model_requests=0, test_only=True, actions=[],
                  text_method="public post_input_text; line breaks use public VK_RETURN down/up",
                  scope="focus-only" if args.focus_only else "complete")
    def start(name):
        item = Fixture(run_directory / name, args.fixture_python, args.fixture_script, registry, args.method)
        fixtures.append(item)
        return item
    def result(name, passed, **evidence):
        rows.append(dict(case=name, status="PASS" if passed else "FAIL", **evidence))
        (run_directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(dict(event="CASE_RESULT", case=name, status=rows[-1]["status"])), flush=True)
    def preserve_action(label, action, fixture):
        # Persist before a subsequent screenshot can fail because focus moved.
        report["actions"].append(dict(label=label, action=asdict(action), fixture=fixture.state(),
                                      sdk_calls=list(fixture.backend.calls)))
        (run_directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = "中文 English 123\n第二行 / line two"
    try:
        fixture = start("unicode-submit")
        gate = EpochGate()
        backend = fixture.backend
        before = record_image(backend, fixture.directory, "before")
        mapping = backend.mapping(before)
        rect = mapping["input_rect"]
        point = ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
        controller = SafeActionController(backend, gate, registry, fixture.target,
                                          ActionLedger(fixture.directory / "actions.sqlite3"))
        if args.focus_only:
            baseline_submits = fixture.state()["submits"]
            def switch_to_owned_dummy():
                record_image(backend, fixture.directory, "typed-before-focus-change")
                fixture.show_owned_dummy(registry)
            backend.after_input = switch_to_owned_dummy
            lost = controller.execute("focus-only-recheck", text, point, gate.token())
            preserve_action("focus-only-recheck", lost, fixture)
            dummy = getattr(fixture, "dummy_target", None)
            dummy_focused = dummy is not None and registry.owns(dummy) and backend.windows.snapshot(dummy.hwnd).foreground
            state = fixture.state()
            passed = (lost.status == "rejected" and "not focused" in lost.reason and dummy_focused
                      and state["text"] == text and state["submits"] == baseline_submits
                      and "submit" not in backend.calls)
            result("focus_moves_to_other_owned_window_submit_blocked", passed,
                   action=asdict(lost), mapping=mapping, observed_text=state["text"],
                   submit_count_before=baseline_submits, submit_count_after=state["submits"],
                   dummy_target=asdict(dummy) if dummy is not None else None,
                   dummy_focused=bool(dummy_focused), sdk_calls=list(backend.calls),
                   window_identity=asdict(fixture.target))
            report["status"] = "PASS" if passed else "FAIL"
            return 0 if passed else 2
        action = controller.execute("single-submit", text, point, gate.token())
        preserve_action("single-submit", action, fixture)
        after = record_image(backend, fixture.directory, "after")
        state = fixture.state()
        baseline_submits = state["submits"]
        changed = ImageChops.difference(before.crop(rect), after.crop(rect)).getbbox() is not None
        passed = action.status == "observed" and state["text"] == text and state["last_submitted"] == text and state["submits"] == 1 and changed
        result("unicode_newline_input_visible_and_single_submit", passed, action=asdict(action),
               mapping=mapping, observed_text=state["text"], submit_count=state["submits"], input_pixels_changed=changed,
               window_identity=asdict(fixture.target), sdk_calls=list(backend.calls))
        previous_calls = len(backend.calls)
        duplicate = controller.execute("single-submit", text, point, gate.token())
        draft = controller.execute("different-action", "不可覆盖的草稿", point, gate.token())
        result("duplicate_submit_and_existing_draft_blocked", duplicate.status == "rejected" and draft.status == "rejected" and
               fixture.state()["submits"] == baseline_submits and len(backend.calls) == previous_calls,
               duplicate=asdict(duplicate), draft=asdict(draft), submit_count=fixture.state()["submits"])
        fixture.reset_synthetic_input()
        gate = EpochGate()
        backend = fixture.backend
        backend.calls.clear()
        mapping = backend.mapping()
        rect = mapping["input_rect"]
        point = ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
        controller = SafeActionController(backend, gate, registry, fixture.target)
        old = gate.token()
        gate.pause()
        paused = controller.execute("already-paused", text, point, old)
        result("paused_before_action_zero_input", paused.status == "rejected" and not backend.calls, action=asdict(paused))
        gate.resume()
        backend.after_click = gate.pause
        interrupted = controller.execute("pause-after-click", text, point, gate.token())
        result("pause_after_real_click_stops_text", interrupted.status == "rejected" and backend.calls == ["click"] and fixture.state()["text"] == "",
               action=asdict(interrupted), sdk_calls=list(backend.calls))
        gate.resume()
        backend.after_click = lambda: None
        backend.after_input = gate.pause
        interrupted = controller.execute("pause-after-input", text, point, gate.token())
        preserve_action("pause-after-input", interrupted, fixture)
        record_image(backend, fixture.directory, "paused-after-input")
        result("pause_after_real_input_stops_submit", interrupted.status == "rejected" and fixture.state()["submits"] == baseline_submits and fixture.state()["text"] == text,
               action=asdict(interrupted), submit_count=fixture.state()["submits"], observed_text=fixture.state()["text"])
        fixture.reset_synthetic_input()
        gate = EpochGate()
        backend = fixture.backend
        rect = backend.mapping()["input_rect"]
        point = ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
        backend.after_input = lambda: fixture.show_owned_dummy(registry)
        controller = SafeActionController(backend, gate, registry, fixture.target)
        lost = controller.execute("focus-changed", text, point, gate.token())
        dummy_hwnd = getattr(getattr(fixture, "dummy_target", None), "hwnd", None)
        dummy_focused = dummy_hwnd is not None and backend.windows.snapshot(dummy_hwnd).foreground
        result("focus_moves_to_other_owned_window_submit_blocked", lost.status == "rejected" and fixture.state()["submits"] == baseline_submits and dummy_focused,
               action=asdict(lost), submit_count=fixture.state()["submits"], dummy_hwnd=dummy_hwnd, dummy_focused=dummy_focused)
        fixture.close()
        previous_calls = len(backend.calls)
        closed = controller.execute("target-closed", text, point, gate.token())
        result("target_normal_close_stops_submit", closed.status == "rejected" and fixture.state()["submits"] == baseline_submits and len(backend.calls) == previous_calls,
               action=asdict(closed), closed=fixture.state()["closed"], submit_count=fixture.state()["submits"])
        report["status"] = "PASS" if all(row["status"] == "PASS" for row in rows) else "FAIL"
    except Exception as exc:
        report["status"] = "WAITING_FOCUS" if not rows and "cannot focus" in str(exc) else "FAIL"
        if "unfocused" in str(exc):
            report["status"] = "INTERRUPTED_FOCUS"
        if report["status"] == "WAITING_FOCUS":
            report["input_stage"] = "NOT_TESTED"
        report["error"] = type(exc).__name__ + ": " + str(exc)
        report["traceback"] = traceback.format_exc()
    finally:
        cleanup = []
        for fixture in fixtures:
            try:
                fixture.close()
                cleanup.append(dict(pid=fixture.target.pid, normal_close=True))
            except Exception as exc:
                cleanup.append(dict(normal_close=False, error=str(exc)))
        report["cleanup"] = cleanup
        report["evidence_directory"] = str(run_directory)
        (run_directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "PASS" else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-python", type=Path, required=True)
    parser.add_argument("--fixture-script", type=Path, default=Path(__file__).with_name("wechat_visual_test_window.py"))
    parser.add_argument("--method", choices=("SendMessage", "PostMessage"), default="SendMessage")
    parser.add_argument("--focus-only", action="store_true", help="Recheck only owned-dummy focus cancellation; never submit")
    args = parser.parse_args()
    with ExclusiveLease():
        return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
