"""Local-only admission and actions for the visual transport.

This module never imports a controller SDK or sends Windows input. Production
capture backends intentionally do not implement ActionBackend. Input is only
admitted to a registered, freshly verified window created by this task.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Callable, Protocol


class SafetyError(RuntimeError):
    pass


class LeaseBusy(SafetyError):
    pass


class BudgetExhausted(SafetyError):
    pass


class RequestAlreadyAdmitted(SafetyError):
    pass


def default_control_lock() -> Path:
    """All visual entrypoints for this Windows user use the exact same lock."""
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return root / "Ativia" / "wechat-visual" / "control.lock"


class ExclusiveLease:
    """Nonblocking OS lock; process death releases it without deleting the file.

    Lock-file existence is not ownership. Never unlink this path: replacing an
    inode while another process owns it would permit two independent holders.
    """

    def __init__(self, path: Path | str | None = None, owner_tag: str = "visual"):
        self.path = Path(path) if path is not None else default_control_lock()
        self.owner_tag = owner_tag
        self._file = None

    def acquire(self):
        if self._file is not None:
            raise SafetyError("lease already acquired by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b", buffering=0)
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LeaseBusy("another visual instance owns the control lease") from exc
        self._file = handle
        return self

    def release(self):
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


@dataclass(frozen=True)
class BudgetTicket:
    session_id: str
    request_id: str
    kind: str
    used: int
    remaining: int


class PersistentModelBudget:
    """Durable pre-admission for all model requests, including vision/retries.

    No refunds: an error or process crash may follow a billable request. With
    session_id omitted, every restart reuses the persisted active session. A
    new explicit session must be a deliberate new user-authorized round, not a
    worker-generated UUID. SDK automatic retries must be disabled separately.
    """

    def __init__(self, path: Path | str, session_id: str | None = None, limit: int = 6):
        if type(limit) is not int or not 1 <= limit <= 6:
            raise ValueError("model request limit must be between 1 and 6")
        if session_id is not None and (not session_id or len(session_id) > 160):
            raise ValueError("invalid model budget session")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, limit_count INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS active (slot INTEGER PRIMARY KEY CHECK(slot=1), session_id TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS admissions (session_id TEXT NOT NULL, request_id TEXT NOT NULL, kind TEXT NOT NULL, admitted_at REAL NOT NULL, PRIMARY KEY(session_id,request_id))")
            db.execute("BEGIN IMMEDIATE")
            if session_id is None:
                active = db.execute("SELECT session_id FROM active WHERE slot=1").fetchone()
                session_id = active[0] if active else secrets.token_hex(16)
            stored = db.execute("SELECT limit_count FROM sessions WHERE id=?", (session_id,)).fetchone()
            if stored is not None and stored[0] != limit:
                raise SafetyError("cannot change an existing session model limit")
            db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?)", (session_id, limit))
            db.execute("INSERT OR IGNORE INTO active VALUES (1,?)", (session_id,))
        self.session_id = session_id
        self.limit = limit

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @property
    def used(self) -> int:
        with self._connection() as db:
            return db.execute("SELECT COUNT(*) FROM admissions WHERE session_id=?", (self.session_id,)).fetchone()[0]

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self, kind: str, request_id: str | None = None) -> BudgetTicket:
        if not isinstance(kind, str) or not kind or len(kind) > 64:
            raise ValueError("invalid request kind")
        request_id = request_id or secrets.token_hex(16)
        if not isinstance(request_id, str) or len(request_id) > 160:
            raise ValueError("invalid request id")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admissions WHERE session_id=? AND request_id=?", (self.session_id, request_id)).fetchone():
                raise RequestAlreadyAdmitted("request was already admitted; do not retry it")
            used = db.execute("SELECT COUNT(*) FROM admissions WHERE session_id=?", (self.session_id,)).fetchone()[0]
            if used >= self.limit:
                raise BudgetExhausted("this session has exhausted its model request budget")
            db.execute("INSERT INTO admissions VALUES (?,?,?,?)", (self.session_id, request_id, kind, time.time()))
        return BudgetTicket(self.session_id, request_id, kind, used + 1, self.limit - used - 1)


class EpochGate:
    """Thread-safe local gate, optionally linked to Ativia's existing PauseGate."""

    def __init__(self, external_gate=None, paused: bool = False):
        self._lock = threading.RLock()
        self._paused = bool(paused)
        self.epoch = 0
        self.reason = "paused" if paused else "ready"
        self._external = external_gate
        self._callbacks: list[Callable[[int, str], None]] = []
        self._unsubscribe = external_gate.subscribe(self._external_change) if external_gate is not None else None

    def _external_change(self, state, _epoch):
        self.invalidate("Ativia pause state changed: " + str(state))

    def token(self) -> int:
        with self._lock:
            return self.epoch

    def allows(self, epoch: int | None = None) -> bool:
        with self._lock:
            return (not self._paused and (epoch is None or epoch == self.epoch)
                    and (self._external is None or self._external.allows()))

    def subscribe(self, callback: Callable[[int, str], None]):
        with self._lock:
            self._callbacks.append(callback)
        def unsubscribe():
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return unsubscribe

    def _change(self, reason, paused=None):
        with self._lock:
            if paused is not None:
                self._paused = paused
            self.epoch += 1
            self.reason = reason
            callbacks, epoch = tuple(self._callbacks), self.epoch
        for callback in callbacks:
            try:
                callback(epoch, reason)
            except Exception:
                pass
        return epoch

    def pause(self):
        return self._change("paused", True)

    def resume(self):
        return self._change("resumed; baseline required", False)

    def invalidate(self, reason: str = "invalidated"):
        return self._change(reason)

    def dispatch(self, epoch: int, operation: Callable):
        """Serialize admission, not a blocking SDK call, with pause.

        An admitted operation is in flight and may finish after pause. Holding
        this lock while waiting for it would prevent pause from invalidating
        every other queued operation and preview promptly.
        """
        with self._lock:
            if not self.allows(epoch):
                raise SafetyError("pause or capture epoch invalidated")
        return operation()

    def close(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.pause()


@dataclass(frozen=True)
class TestWindowTarget:
    hwnd: int
    pid: int
    process_start: str
    nonce: str
    kind: str = "owned_test"


@dataclass(frozen=True)
class WindowState:
    hwnd: int
    pid: int
    process_start: str
    nonce: str
    process_name: str
    exists: bool
    focused: bool
    draft: str
    client_size: tuple[int, int]
    input_rect: tuple[int, int, int, int]


class OwnedWindowRegistry:
    """Ownership records from our own launcher, plus an unguessable window nonce.

    record_created_process accepts the Popen handle returned by our launcher.
    It must never be called for a PID discovered from arbitrary user windows.
    The backend independently observes PID/start/nonce on every action step.
    """

    def __init__(self):
        self._nonces = set()
        self._processes = {}
        self._targets = set()

    def issue_nonce(self) -> str:
        nonce = secrets.token_hex(24)
        self._nonces.add(nonce)
        return nonce

    def record_created_process(self, process, process_start: str):
        if process.poll() is not None or process.pid <= 0 or not process_start:
            raise SafetyError("test process is not live")
        self._processes[(int(process.pid), str(process_start))] = process

    def register_window(self, hwnd: int, pid: int, process_start: str, nonce: str) -> TestWindowTarget:
        if hwnd <= 0 or nonce not in self._nonces or (pid, process_start) not in self._processes:
            raise SafetyError("window was not created by this task")
        self._nonces.remove(nonce)
        target = TestWindowTarget(hwnd, pid, process_start, nonce)
        self._targets.add(target)
        return target

    def owns(self, target: TestWindowTarget) -> bool:
        process = self._processes.get((target.pid, target.process_start))
        return target in self._targets and process is not None and process.poll() is None


class ActionBackend(Protocol):
    def inspect(self, target: TestWindowTarget) -> WindowState: ...
    def click(self, target: TestWindowTarget, x: int, y: int): ...
    def input_text(self, target: TestWindowTarget, text: str): ...
    def submit(self, target: TestWindowTarget): ...
    def observe_submission(self, target: TestWindowTarget, action_id: str) -> bool | None: ...


class ActionLedger:
    """One attempt per action id; durable when a local path is supplied."""
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._states = {}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def reserve(self, action_id: str):
        if not isinstance(action_id, str) or not action_id or len(action_id) > 160:
            raise SafetyError("invalid action id")
        with self._lock:
            if self.path is None:
                if action_id in self._states:
                    raise SafetyError("action already attempted; never automatically retry")
                self._states[action_id] = "reserved"
            else:
                try:
                    with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                        db.execute("INSERT INTO actions VALUES (?, 'reserved')", (action_id,))
                except sqlite3.IntegrityError as exc:
                    raise SafetyError("action already attempted; never automatically retry") from exc

    def mark(self, action_id: str, state: str):
        with self._lock:
            if self.path is None:
                self._states[action_id] = state
            else:
                with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                    db.execute("UPDATE actions SET state=? WHERE id=?", (state, action_id))


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    status: str
    reason: str = ""
    input_verified: bool = False
    submitted: bool = False


class SafeActionController:
    """Input capability for owned test windows only. WeChat is unconditionally denied."""
    def __init__(self, backend: ActionBackend, gate: EpochGate, registry: OwnedWindowRegistry,
                 target: TestWindowTarget, ledger: ActionLedger | None = None):
        self.backend, self.gate, self.registry, self.target = backend, gate, registry, target
        self.ledger = ledger or ActionLedger()
        self._lock = threading.Lock()

    def _check(self, epoch: int, expected_draft: str | None = None) -> WindowState:
        if self.target.kind != "owned_test" or not self.registry.owns(self.target):
            raise SafetyError("input requires a task-owned test window; WeChat input is disabled")
        if not self.gate.allows(epoch):
            raise SafetyError("paused or stale epoch")
        state = self.backend.inspect(self.target)
        if state.process_name.casefold() in ("weixin.exe", "wechat.exe", "weixin", "wechat"):
            raise SafetyError("WeChat input is permanently disabled for this task")
        actual = (state.hwnd, state.pid, state.process_start, state.nonce)
        expected = (self.target.hwnd, self.target.pid, self.target.process_start, self.target.nonce)
        if not state.exists or actual != expected:
            raise SafetyError("test window closed or identity changed")
        if not state.focused:
            raise SafetyError("test window is not focused; never steal focus")
        if expected_draft is not None and state.draft != expected_draft:
            raise SafetyError("draft changed or input verification failed")
        if not self.gate.allows(epoch):
            raise SafetyError("paused while verifying target")
        return state

    def execute(self, action_id: str, text: str, point: tuple[int, int], epoch: int, submit: bool = True) -> ActionResult:
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            return ActionResult(action_id, "rejected", "empty or oversized text")
        if len(point) != 2 or any(type(value) is not int for value in point):
            return ActionResult(action_id, "rejected", "invalid client coordinates")
        if not self._lock.acquire(blocking=False):
            return ActionResult(action_id, "rejected", "another action is in progress")
        reserved = False
        submitting = False
        verified = False
        try:
            state = self._check(epoch, expected_draft="")
            x, y = point
            left, top, right, bottom = state.input_rect
            width, height = state.client_size
            if not (0 <= left <= x < right <= width and 0 <= top <= y < bottom <= height):
                raise SafetyError("point is outside the verified client input region")
            self.ledger.reserve(action_id)
            reserved = True
            if hasattr(self.backend, "set_action_gate"):
                self.backend.set_action_gate(self.gate, epoch)
            self.gate.dispatch(epoch, lambda: self.backend.click(self.target, x, y))
            self._check(epoch, expected_draft="")
            self.gate.dispatch(epoch, lambda: self.backend.input_text(self.target, text))
            self._check(epoch, expected_draft=text)
            verified = True
            if not submit:
                self.ledger.mark(action_id, "prepared")
                return ActionResult(action_id, "prepared", input_verified=True)
            self.ledger.mark(action_id, "submitting")
            self._check(epoch, expected_draft=text)
            # Mark unknown before dispatch: an exception cannot prove no input occurred.
            submitting = True
            self.gate.dispatch(epoch, lambda: self.backend.submit(self.target))
            self._check(epoch)
            observed = self.backend.observe_submission(self.target, action_id)
            status = "observed" if observed is True else "unknown"
            self.ledger.mark(action_id, status)
            return ActionResult(action_id, status, input_verified=True, submitted=True)
        except Exception as exc:
            status = "unknown" if submitting else "rejected"
            if reserved:
                self.ledger.mark(action_id, status)
            return ActionResult(action_id, status, str(exc), verified, submitting)
        finally:
            self._lock.release()
