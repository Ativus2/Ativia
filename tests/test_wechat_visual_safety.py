"""Offline safety contracts; no Windows input, model, or vendor SDK is invoked."""
from dataclasses import replace
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from integrations.wechat.visual.safety import (
    ActionLedger, BudgetExhausted, EpochGate, ExclusiveLease, LeaseBusy,
    OwnedWindowRegistry, PersistentModelBudget, RequestAlreadyAdmitted,
    SafeActionController, SafetyError, TestWindowTarget, WindowState,
)


def consume_in_worker(arguments):
    path, number = arguments
    budget = PersistentModelBudget(path)
    try:
        budget.consume("reply" if number % 2 else "vision", str(number))
        return "admitted"
    except BudgetExhausted:
        return "exhausted"


def attempt_lease_in_worker(path):
    try:
        with ExclusiveLease(path):
            return "acquired"
    except LeaseBusy:
        return "busy"


class FakeProcess:
    pid = 8123
    stopped = False
    def poll(self):
        return 0 if self.stopped else None


class FakeBackend:
    def __init__(self, target):
        self.state = WindowState(target.hwnd, target.pid, target.process_start,
                                 target.nonce, "python.exe", True, True, "",
                                 (800, 600), (20, 300, 700, 550))
        self.calls = []
        self.after_click = lambda: None
        self.after_input = lambda: None
        self.after_submit = lambda: None
        self.submit_error = False
        self.observed = True

    def inspect(self, _target):
        return self.state

    def click(self, _target, x, y):
        self.calls.append(("click", x, y))
        self.after_click()

    def input_text(self, _target, text):
        self.calls.append(("input_text", text))
        self.state = replace(self.state, draft=text)
        self.after_input()

    def submit(self, _target):
        self.calls.append(("submit",))
        self.after_submit()
        if self.submit_error:
            raise OSError("result is unknown")
        self.state = replace(self.state, draft="")

    def observe_submission(self, _target, _action_id):
        return self.observed


class FakePauseGate:
    def __init__(self):
        self.paused = True
        self.callbacks = []
    def allows(self):
        return not self.paused
    def subscribe(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)
    def transition(self, paused):
        self.paused = paused
        for callback in tuple(self.callbacks):
            callback("PAUSED" if paused else "RUNNING", 1)


class ModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "budget.sqlite3"
    def tearDown(self):
        self.tmp.cleanup()

    def test_all_request_kinds_share_six_durable_admissions(self):
        first = PersistentModelBudget(self.path)
        for kind in ("vision", "reply", "retry"):
            first.consume(kind)
        restarted = PersistentModelBudget(self.path)
        self.assertEqual(first.session_id, restarted.session_id)
        self.assertEqual(restarted.remaining, 3)
        for _ in range(3):
            restarted.consume("reply")
        with self.assertRaises(BudgetExhausted):
            PersistentModelBudget(self.path).consume("reply")
        self.assertEqual(first.remaining, 0)

    def test_request_identity_cannot_implicitly_retry(self):
        budget = PersistentModelBudget(self.path)
        budget.consume("reply", "same-request")
        with self.assertRaises(RequestAlreadyAdmitted):
            budget.consume("reply", "same-request")
        self.assertEqual(budget.used, 1)

    def test_limits_cannot_increase_or_exceed_six(self):
        for limit in (0, 7, True):
            with self.assertRaises(ValueError):
                PersistentModelBudget(self.path, limit=limit)
        PersistentModelBudget(self.path, limit=3)
        with self.assertRaises(SafetyError):
            PersistentModelBudget(self.path, limit=6)

    def test_concurrent_processes_cannot_overspend(self):
        budget = PersistentModelBudget(self.path)
        context = multiprocessing.get_context("spawn")
        with context.Pool(4) as pool:
            results = pool.map(consume_in_worker, [(str(self.path), i) for i in range(12)])
        self.assertEqual(results.count("admitted"), 6)
        self.assertEqual(results.count("exhausted"), 6)
        self.assertEqual(budget.remaining, 0)

    def test_corrupt_budget_fails_closed(self):
        self.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(Exception):
            PersistentModelBudget(self.path)
        self.assertEqual(self.path.read_bytes(), b"not a sqlite database")


class LeaseTests(unittest.TestCase):
    def test_same_path_rejects_second_owner_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.lock"
            with ExclusiveLease(path):
                with self.assertRaises(LeaseBusy):
                    ExclusiveLease(path).acquire()
                context = multiprocessing.get_context("spawn")
                with context.Pool(1) as pool:
                    self.assertEqual(pool.apply(attempt_lease_in_worker, (str(path),)), "busy")
            self.assertTrue(path.exists())
            with ExclusiveLease(path):
                pass

    def test_owned_child_exit_releases_kernel_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "control.lock"
            script = ("import sys; from integrations.wechat.visual.safety import ExclusiveLease; "
                      "lease=ExclusiveLease(sys.argv[1]).acquire(); print('locked',flush=True); "
                      "sys.stdin.readline()")
            child = subprocess.Popen([sys.executable, "-B", "-c", script, str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(LeaseBusy):
                    ExclusiveLease(path).acquire()
                child.communicate("exit\n", timeout=10)
                self.assertEqual(child.returncode, 0)
                with ExclusiveLease(path):
                    pass
            finally:
                if child.poll() is None:
                    child.communicate("exit\n", timeout=10)


class EpochTests(unittest.TestCase):
    def test_blocking_admitted_operation_does_not_block_pause(self):
        gate = EpochGate()
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        def operation():
            entered.set()
            release.wait(3)
        worker = threading.Thread(target=lambda: gate.dispatch(gate.token(), operation))
        worker.start()
        self.assertTrue(entered.wait(1))
        pauser = threading.Thread(target=lambda: (gate.pause(), paused.set()))
        pauser.start()
        try:
            self.assertTrue(paused.wait(.5), "pause was delayed by a blocking SDK call")
            self.assertFalse(gate.allows())
        finally:
            release.set()
            worker.join(3)
            pauser.join(3)

    def test_pause_resume_and_invalidation_revoke_old_tokens(self):
        gate = EpochGate()
        token = gate.token()
        self.assertTrue(gate.allows(token))
        gate.pause()
        self.assertFalse(gate.allows())
        gate.resume()
        self.assertTrue(gate.allows())
        self.assertFalse(gate.allows(token))
        token = gate.token()
        gate.invalidate("capture lost")
        self.assertFalse(gate.allows(token))

    def test_external_pause_cannot_be_silently_resumed(self):
        external = FakePauseGate()
        gate = EpochGate(external)
        gate.resume()
        self.assertFalse(gate.allows())
        external.transition(False)
        token = gate.token()
        self.assertTrue(gate.allows(token))
        external.transition(True)
        self.assertFalse(gate.allows(token))
        gate.close()
        self.assertEqual(external.callbacks, [])

    def test_invalidation_notifies_preview_revocation_subscriber(self):
        gate = EpochGate()
        seen = []
        unsubscribe = gate.subscribe(lambda epoch, reason: seen.append((epoch, reason)))
        gate.pause()
        unsubscribe()
        gate.resume()
        self.assertEqual(seen, [(1, "paused")])


class SafeActionTests(unittest.TestCase):
    def setUp(self):
        self.registry = OwnedWindowRegistry()
        self.process = FakeProcess()
        self.registry.record_created_process(self.process, "start-001")
        self.target = self.registry.register_window(123, self.process.pid, "start-001", self.registry.issue_nonce())
        self.gate = EpochGate()
        self.backend = FakeBackend(self.target)
        self.controller = SafeActionController(self.backend, self.gate, self.registry, self.target)

    def execute(self, **kwargs):
        args = dict(action_id="one", text="中文 English\n第二行", point=(30, 350), epoch=self.gate.token())
        args.update(kwargs)
        return self.controller.execute(**args)

    def test_unicode_multiline_input_is_observed_before_single_submit(self):
        result = self.execute()
        self.assertEqual(result.status, "observed")
        self.assertTrue(result.input_verified)
        self.assertEqual([row[0] for row in self.backend.calls], ["click", "input_text", "submit"])
        self.assertEqual(self.backend.calls[1][1], "中文 English\n第二行")
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(len(self.backend.calls), 3)

    def test_input_only_does_not_submit(self):
        result = self.execute(submit=False)
        self.assertEqual(result.status, "prepared")
        self.assertEqual(len(self.backend.calls), 2)

    def test_wechat_kind_has_no_input_capability(self):
        self.controller.target = replace(self.target, kind="wechat")
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(self.backend.calls, [])

    def test_process_name_defense_denies_wechat_even_with_spoofed_test_record(self):
        for name in ("Weixin.exe", "WECHAT.EXE"):
            self.backend.state = replace(self.backend.state, process_name=name)
            self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(self.backend.calls, [])

    def test_nonce_pid_start_and_hwnd_are_reverified(self):
        original = self.backend.state
        for change in ({"pid": 5}, {"hwnd": 7}, {"nonce": "wrong"}, {"process_start": "reused-pid"}):
            self.backend.state = replace(original, **change)
            self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(self.backend.calls, [])

    def test_arbitrary_window_cannot_be_registered(self):
        with self.assertRaises(SafetyError):
            self.registry.register_window(555, 1, "other", self.registry.issue_nonce())
        with self.assertRaises(SafetyError):
            self.registry.register_window(555, self.process.pid, "start-001", "unissued-nonce")

    def test_nonempty_draft_is_never_overwritten(self):
        self.backend.state = replace(self.backend.state, draft="用户草稿")
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(self.backend.calls, [])

    def test_unfocused_or_closed_window_is_never_activated(self):
        original = self.backend.state
        for change in ({"focused": False}, {"exists": False}):
            self.backend.state = replace(original, **change)
            self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(self.backend.calls, [])

    def test_pause_after_click_prevents_text_and_submission(self):
        self.backend.after_click = self.gate.pause
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual([row[0] for row in self.backend.calls], ["click"])

    def test_pause_after_input_prevents_submission(self):
        self.backend.after_input = self.gate.pause
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual([row[0] for row in self.backend.calls], ["click", "input_text"])

    def test_focus_change_after_input_prevents_submission(self):
        self.backend.after_input = lambda: setattr(self.backend, "state", replace(self.backend.state, focused=False))
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(len(self.backend.calls), 2)

    def test_target_close_after_input_prevents_submission(self):
        self.backend.after_input = lambda: setattr(self.process, "stopped", True)
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(len(self.backend.calls), 2)

    def test_input_success_status_does_not_replace_actual_content_verification(self):
        self.backend.after_input = lambda: setattr(self.backend, "state", replace(self.backend.state, draft="truncated"))
        result = self.execute()
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.input_verified)
        self.assertEqual(len(self.backend.calls), 2)

    def test_unknown_submit_never_retries(self):
        self.backend.submit_error = True
        result = self.execute()
        self.assertEqual(result.status, "unknown")
        self.backend.state = replace(self.backend.state, draft="")
        self.assertEqual(self.execute().status, "rejected")
        self.assertEqual(len(self.backend.calls), 3)

    def test_no_observed_submission_is_unknown_not_success(self):
        self.backend.observed = None
        self.assertEqual(self.execute().status, "unknown")

    def test_persistent_ledger_rejects_attempt_after_controller_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.sqlite3"
            self.controller.ledger = ActionLedger(path)
            self.backend.observed = None
            self.assertEqual(self.execute().status, "unknown")
            self.controller = SafeActionController(self.backend, self.gate, self.registry, self.target, ActionLedger(path))
            self.assertEqual(self.execute().status, "rejected")
            self.assertEqual(len(self.backend.calls), 3)

    def test_stale_epoch_and_outside_coordinates_produce_zero_input(self):
        stale = self.gate.token()
        self.gate.invalidate("new capture session")
        self.assertEqual(self.execute(epoch=stale).status, "rejected")
        self.assertEqual(self.execute(point=(2, 2)).status, "rejected")
        self.assertEqual(self.execute(text=" ").status, "rejected")
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
