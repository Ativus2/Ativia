"""Owned worker and Ativia pipeline with an explicitly enabled downstream sender."""
from __future__ import annotations

from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import time
import uuid

from .safety import ExclusiveLease, SafetyError
from .session import VisualSession
from .types import TARGET_GROUP, VisualEvent
from .desktop import probe_desktop

ROUND_DIRECTORY = "round-20260930"
BACKENDS = {"win32_printwindow", "maa_gdi", "foreground_crop"}
WORKER_REASONS = {
    "window_not_unique", "minimized_pause", "paused", "resumed", "manual_confirmation", "capture_gap",
    "window_geometry_or_confirmation_changed", "target_header_changed", "manual_interaction_bottom_uncertain",
    "header_missing_or_ambiguous", "header_crop_boundary", "adjacent_title_fragment_ambiguous",
    "member_suffix_ambiguous", "exact_title", "capture_failed_or_minimized", "binding_invalid",
    "scroll_bottom_or_layout_uncertain", "non_increasing_frame", "empty_chat_burst", "insufficient_ordered_overlap",
    "CaptureUnavailable", "ValueError", "RuntimeError", "Error", "AccessDenied", "NoSuchProcess",
    "ZombieProcess", "TimeoutExpired", "worker_blocked", "main_state_unverified", "main_state_not_running",
    "desktop_unavailable", "desktop_state_unknown", "session_unavailable",
}


def metadata_reason(value):
    return value if isinstance(value, str) and value in WORKER_REASONS else "unrecognized_reason"


class StopCleanupError(RuntimeError):
    """Report cleanup failure without preserving possibly private error bodies."""
    def __init__(self, failures):
        self.failures = tuple(dict(row) for row in failures)
        super().__init__("visual stop cleanup failed: " + ", ".join(
            row["stage"] + ":" + row["error_type"] for row in self.failures))


def worker_environment(project_root, python_path, environ=None):
    """Explicit inheritance whitelist: no API keys, proxy credentials or DLL overrides."""
    environ = os.environ if environ is None else environ
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
               "APPDATA", "LOCALAPPDATA", "USERNAME", "USERDOMAIN", "HOMEDRIVE",
               "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    result = {key: value for key, value in environ.items() if key.upper() in allowed}
    windows = environ.get("SystemRoot", environ.get("SYSTEMROOT", r"C:\Windows"))
    result["PATH"] = os.pathsep.join((str(Path(python_path).parent), str(Path(windows) / "System32"), windows))
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                  PYTHONPATH=str(Path(project_root).resolve()))
    return result


def validate_runtime_config(config):
    if not isinstance(config, dict):
        raise ValueError("runtime.local.json must be an object")
    python = Path(config.get("python", ""))
    resource = Path(config.get("resource_root", ""))
    backend = config.get("backend", "win32_printwindow")
    if not python.is_absolute() or not python.is_file() or not resource.is_absolute() or not resource.is_dir():
        raise ValueError("runtime worker python or resource_root is unavailable")
    if backend not in BACKENDS:
        raise ValueError("unreviewed capture backend")
    calibration = config.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("calibration required")
    for key in ("header", "message", "input"):
        value = calibration.get(key)
        if (not isinstance(value, (list, tuple)) or len(value) != 4 or
                any(type(number) is not int for number in value) or min(value) < 0 or min(value[2:]) <= 0):
            raise ValueError("invalid calibration rectangle")
    return {"python": str(python), "resource_root": str(resource), "backend": backend,
            "calibration": calibration}


class VisualCoordinator:
    """Call poll from one UI/CLI event loop; pipe readers only enqueue packets.

    The local PauseGate mirrors the main Ativia gate conservatively. Unknown or
    stale main state pauses this session. This class never changes main Ativia.
    """
    def __init__(self, project_root, action, config, gate, settings, *,
                 session_factory=VisualSession, lease_factory=ExclusiveLease,
                 process_factory=subprocess.Popen, parent_start=None,
                 clock=time.monotonic, wall_clock=time.time, desktop_probe=probe_desktop):
        if action not in {"diagnose", "listen", "dry-run", "self-test", "run"}:
            raise ValueError("invalid visual action")
        self.project_root = Path(project_root).resolve()
        self.round_root = self.project_root / "runtime" / "wechat-visual" / ROUND_DIRECTORY
        self.action, self.config = action, validate_runtime_config(config)
        self.gate, self.settings = gate, settings
        self._session_factory, self._lease_factory = session_factory, lease_factory
        self._process_factory, self._parent_start = process_factory, parent_start
        self.clock, self.wall_clock = clock, wall_clock
        self._desktop_probe = desktop_probe
        self.desktop_available = False
        self.desktop_reason = 'desktop_state_unknown'
        self.session = None
        self.lease = None
        self.process = None
        self.capture_session_id = uuid.uuid4().hex
        self._queue = queue.Queue(maxsize=256)
        self._write_lock = threading.Lock()
        self._reader_fault = threading.Event()
        self._closed = False
        self.user_paused = False
        self.external_running = False
        self.external_observed = None
        self.last_heartbeat = None
        self.candidate = None
        self.candidate_observed = None
        self.bound = False
        self.baseline_complete = False
        self.listener_ready = False
        self.status = "WAITING_ATIVIA_STATE"
        self.reason = "main_state_unverified"
        self.frames = 0
        self.events = 0
        self.rejected_packets = 0
        self.frame_stats = {}
        self.status_rows = deque(maxlen=128)
        self.started_at = self.clock()
        self._last_poll = self.clock()
        self._previous_ready = False
        self.ready_seconds = 0.0
        self._last_persist = None
        self._preview_ids = set()
        self.preview_count = 0
        self.last_preview_metadata = None
        self.cleanup_errors = []
        self.stop_reason = None
        self._shutdown_business = None
        self.self_test_token = None
        self.self_test_started = None
        self.self_test_issued_at = None
        self.self_test_consumed = False
        self.self_test_expired = False
        self.self_test_confirmed = False
        self.self_test_binding = None
        self.self_test_preview_count = 0
        self.send_controller = None
        self.send_context = None
        self._generation_send_epochs = {}
        if gate.allows():
            gate.transition("PAUSING")
            gate.transition("PAUSED")

    @property
    def closed(self):
        return self._closed

    def reserve(self):
        """Reserve the user-wide control lease before exposing the stop server."""
        if self.lease is None:
            self.lease = self._lease_factory()
            self.lease.acquire()

    def set_external_status(self, row):
        self.external_observed = self.clock()
        self.external_running = isinstance(row, dict) and row.get("app_state") == "RUNNING"
        if not self.external_running:
            self.reason = "main_state_not_running"
        self._sync_pause()

    def _sync_pause(self):
        try:
            desktop=self._desktop_probe()
            self.desktop_available=desktop.available is True
            self.desktop_reason=desktop.reason
        except Exception:
            self.desktop_available=False
            self.desktop_reason='desktop_state_unknown'
        fresh = self.external_observed is not None and self.clock() - self.external_observed <= 3
        running = bool(fresh and self.external_running and self.desktop_available and not self.user_paused and not self._closed)
        if not self.desktop_available and not self._closed:
            self.reason=metadata_reason(self.desktop_reason)
            self.status='DESKTOP_UNAVAILABLE'
        if running == self.gate.allows():
            return
        if running:
            self.gate.transition("RESUMING")
            self.gate.transition("RUNNING")
            self.status = "WAITING_BINDING"
            self._command("resume")
        else:
            self.gate.transition("PAUSING")
            self.gate.transition("PAUSED")
            self._invalidate("PAUSED" if self.desktop_available else "DESKTOP_UNAVAILABLE")
            self._command("pause")

    def set_user_paused(self, paused):
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        self.user_paused = paused
        self._sync_pause()

    def _one_use_confirmation(self, confirmation):
        if self.action == "diagnose" or confirmation is None:
            return None
        if not isinstance(confirmation, dict) or confirmation.get("bottom_confirmed") is not True:
            raise ValueError("manual bottom confirmation required")
        stamp = confirmation.get("confirmed_at")
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or
                not 0 <= self.wall_clock() - stamp <= 300 or not isinstance(confirmation.get("window"), dict)):
            raise ValueError("manual confirmation is missing or stale")
        safe = {key: confirmation[key] for key in ("window", "bottom_confirmed", "confirmed_at")}
        digest = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
        self.round_root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.round_root / "confirmation-use.sqlite3", timeout=5)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS used (digest TEXT PRIMARY KEY, used_at REAL NOT NULL)")
                db.execute("INSERT INTO used VALUES (?,?)", (digest, self.wall_clock()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("manual confirmation already used; confirm the fresh local preview") from exc
        finally:
            db.close()
        return safe

    def start(self, confirmation=None):
        if self.process is not None or self._closed:
            raise SafetyError("coordinator already started or closed")
        self.reserve()
        try:
            if self.action == "self-test" and confirmation is not None:
                raise ValueError("self-test requires fresh confirmation in the local preview")
            extra = {}
            if self.action == "run":
                extra['continuous_model'] = True
            if self.action == "self-test":
                self.self_test_token = uuid.uuid4().hex[:12].upper()
                self.self_test_started = self.clock()
                self.self_test_issued_at = self.wall_clock()
                extra["self_test_mode"] = True
            self.session = self._session_factory(self.round_root, self.gate, self.settings,
                                                 model_enabled=self.action in {"dry-run", "self-test", "run"}, **extra)
            accepted_confirmation = self._one_use_confirmation(confirmation)
            if self._parent_start is None:
                import psutil
                parent_start = psutil.Process(os.getpid()).create_time()
            else:
                parent_start = self._parent_start
            command = [self.config["python"], "-B", "-m", "integrations.wechat.visual.worker",
                       "--parent-pid", str(os.getpid()), "--parent-start", str(parent_start),
                       "--resource-root", self.config["resource_root"], "--backend", self.config["backend"],
                       "--session", self.capture_session_id]
            if self.action == "self-test":
                command.extend(("--self-test-token", self.self_test_token,
                                "--self-test-issued-at", str(self.self_test_issued_at)))
            if self.action == "run":
                command.append('--send-inspection')
            self.process = self._process_factory(
                command, cwd=str(self.project_root), env=worker_environment(self.project_root, self.config["python"]),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="strict", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
            self.last_heartbeat = self.clock()
            threading.Thread(target=self._read_packets, name="ativia-visual-pipe", daemon=True).start()
            threading.Thread(target=self._discard_errors, name="ativia-visual-stderr", daemon=True).start()
            self._command("start", paused=not self.gate.allows(), confirmation=accepted_confirmation,
                          calibration=self.config["calibration"])
            self.status = "CAPTURE_STARTING" if self.gate.allows() else "PAUSED"
        except Exception:
            self.stop("START_FAILED")
            raise

    def _command(self, action, **fields):
        if action not in {"start", "pause", "resume", "confirm", "stop", "inspect_send", "end_send"}:
            raise SafetyError("worker input command not allowed")
        if self.process is None or self.process.poll() is not None:
            return False
        if action in ('pause','resume','confirm'):
            self._reset_barrier=uuid.uuid4().hex
            fields['reset_id']=self._reset_barrier
            self.candidate=self.candidate_observed=None
        try:
            payload = json.dumps({"action": action, **fields}, ensure_ascii=False)
            if len(payload) > 16384:
                raise ValueError("oversized worker command")
            with self._write_lock:
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
            return True
        except (OSError, BrokenPipeError, ValueError):
            self._reader_fault.set()
            return False

    def _read_packets(self):
        try:
            while True:
                line = self.process.stdout.readline(65537)
                if not line:
                    return
                if len(line) > 65536 or not line.endswith("\n"):
                    raise ValueError("worker protocol line exceeds limit")
                packet = json.loads(line)
                if not isinstance(packet, dict):
                    raise ValueError("worker packet must be object")
                self._queue.put_nowait(packet)
        except (OSError, ValueError, UnicodeError, queue.Full):
            self._reader_fault.set()

    def _discard_errors(self):
        # A native OCR diagnostic may contain text. Drain it without writing it
        # into a logfile, terminal, status report, or model conversation.
        try:
            while self.process.stderr.read(4096):
                pass
        except (OSError, UnicodeError):
            pass

    def _invalidate(self, reason):
        self.bound = self.baseline_complete = self.listener_ready = False
        self.self_test_binding = None
        self.self_test_confirmed = False
        self.send_context = None
        self._generation_send_epochs.clear()
        if self.send_controller is not None:
            send = self.send_controller
            paused = not self.gate.allows()
            if send.session.armed or send.active or send.queue or paused != send.session.paused:
                send.disable(reason, paused=paused)
        self.status = reason
        if self.session is not None:
            self.session.invalidate(reason)

    def attach_sender(self, backend):
        if self.action != 'run' or self.send_controller is not None:
            raise SafetyError('sender belongs only to continuous UI mode')
        from .send import WechatSendController
        self.send_controller = WechatSendController(self.round_root/'send', backend,
            lambda request: self._command('inspect_send', send_request=request),
            lambda transaction: self._command('end_send', send_request={'transaction_id':transaction}),
            valid=self._send_valid, clock=self.clock, wall=self.wall_clock,
            on_result=self._send_result)

    def _send_result(self, task, status, reason, wall):
        callback=getattr(self.session,'delivery_result',None)
        if callback is not None:callback(task,status,reason,wall)

    def _send_valid(self, context):
        try:desktop_ready=self._desktop_probe().available is True
        except Exception:desktop_ready=False
        return bool(desktop_ready and not self._closed and self.action == 'run' and self.gate.allows() and self.bound and
                    self.baseline_complete and self.listener_ready and self.send_context == context and
                    self.last_heartbeat is not None and self.clock()-self.last_heartbeat <= 10)

    def enable_send(self, *, confirmed):
        if not self.send_controller or not self.send_context:
            raise SafetyError('wait for bound listener ready')
        self.send_controller.arm(self.send_context, confirmed=confirmed, ready=self.listener_ready)

    def disable_send(self):
        if self.send_controller:
            self.send_controller.disable('user_disabled')

    def send_test_message(self, text):
        from .send import SendReply, SendRejected
        from ..identity import group_id
        if not self.send_controller or not self.send_context:
            raise SendRejected('send_disabled')
        identity='manual-'+uuid.uuid4().hex
        task=SendReply(identity,text,identity,identity,self.send_context.binding_id,group_id(TARGET_GROUP),
                       '',self.wall_clock(),self.send_controller.session.epoch,source='manual')
        if not self.send_controller.enqueue(task):
            raise SendRejected('send_disabled_stale_or_duplicate')
        return identity

    def _tag_generating_reply(self):
        job=getattr(getattr(self.session,'service',None),'job',None)
        if job is not None and job.ticket.request_id not in self._generation_send_epochs:
            self._generation_send_epochs[job.ticket.request_id] = (
                self.send_controller.session.epoch if self.send_controller and self.send_controller.session.armed else None)
        if len(self._generation_send_epochs)>256:
            self._generation_send_epochs.pop(next(iter(self._generation_send_epochs)))

    def _offer_preview_to_send(self, preview):
        if not self.send_controller or self.action != 'run':
            return
        generation=self._generation_send_epochs.pop(preview.get('trace_id'),None)
        if generation is None or preview.get('source_transport')!='visual' or preview.get('source_self_state')!='incoming':
            return
        from .send import SendReply
        try:
            task=SendReply(preview['reply_id'],preview['reply'],preview['source_event_id'],preview['source_track_id'],
                           preview['source_binding_id'],preview['conversation_id'],preview['model_request_id'],
                           preview['generated_at'],generation)
            queued=self.send_controller.enqueue(task)
            journal=getattr(self.session,'journal',None)
            if journal:journal.result(task,'QUEUED' if queued else 'NOT_QUEUED','' if queued else 'disabled_stale_or_duplicate')
        except (KeyError,ValueError):
            self.send_controller.log('SEND_CANCELLED','invalid_reply_provenance')

    def confirm_candidate(self, *, bottom_confirmed, self_sent_confirmed=False):
        if (self.action == "diagnose" or not self.gate.allows() or bottom_confirmed is not True or
                not self.candidate or self.candidate.get("header") != TARGET_GROUP or
                self.candidate_observed is None or self.clock() - self.candidate_observed > 5):
            raise SafetyError("fresh target preview, bottom confirmation and running state required")
        if self.action == "self-test" and (
                self_sent_confirmed is not True or not self.self_test_token or
                self.self_test_consumed or self.self_test_expired or
                self.clock() - self.self_test_started >= 300):
            raise SafetyError("current one-shot self message and unexpired test required")
        confirmation = {"window": self.candidate["window"], "bottom_confirmed": True,
                        "confirmed_at": self.wall_clock()}
        self._invalidate("CONFIRMING_BINDING")
        self.self_test_confirmed = self.action == "self-test"
        return self._command("confirm", confirmation=confirmation)

    @property
    def self_test_text(self):
        if not self.self_test_token:
            return ""
        from .self_capture import self_test_text
        return self_test_text(self.self_test_token)

    def _self_test_observation(self, packet):
        """A distinct one-use self observation, never an incoming VisualEvent."""
        row = packet.get("observation")
        if (self.action != "self-test" or not self.bound or not self.gate.allows() or
                self.self_test_consumed or self.self_test_expired or not self.self_test_confirmed or
                self.self_test_started is None or self.clock() - self.self_test_started >= 300):
            self.rejected_packets += 1
            return
        if not isinstance(row, dict):
            raise ValueError("invalid self-test observation")
        stamp = row.get("observed_at")
        confidence = row.get("confidence")
        box = row.get("box")
        roi = self.candidate.get("layout", {}).get("message") if self.candidate else None
        valid_box = (isinstance(box, (list, tuple)) and len(box) == 4 and
                     all(type(n) is int for n in box) and min(box) >= 0 and min(box[2:]) > 0 and
                     isinstance(roi, (list, tuple)) and len(roi) == 4 and all(type(n) is int for n in roi))
        if valid_box:
            x, y, w, h = box
            rx, ry, rw, rh = roi
            # A long right-aligned message may extend left of the middle.
            # Native OCR worker separately verifies the green right anchor.
            valid_box = x > rx and y > ry and rx + rw * .6 < x + w < rx + rw and y + h < ry + rh
        if (row.get("token") != self.self_test_token or row.get("group_name") != TARGET_GROUP or
                row.get("self_state") != "self" or row.get("side") != "right" or
                row.get("source") != "local_ocr" or row.get("evidence_kind") != "real_self_test" or
                (row.get("binding_id"), row.get("capture_session_id"), row.get("capture_epoch")) != self.self_test_binding or
                row.get("capture_session_id") != self.capture_session_id or
                type(stamp) not in (int, float) or not math.isfinite(stamp) or
                not 0 <= self.wall_clock() - stamp <= 5 or stamp < self.self_test_issued_at or
                type(confidence) not in (int, float) or not math.isfinite(confidence) or not .9 <= confidence <= 1 or
                row.get("complete") is not True or row.get("kind") != "text" or not valid_box or
                type(row.get("frame_sequence")) is not int or row["frame_sequence"] < 1 or
                type(row.get("stable_frames")) is not int or row["stable_frames"] < 2 or
                not isinstance(row.get("text"), str) or
                "".join(row["text"].split()) != "".join(self.self_test_text.split())):
            raise ValueError("self-test provenance or challenge mismatch")
        # Persist consumption before calling any business/model code. Neither an
        # error nor cancellation can re-arm this challenge.
        self.round_root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.round_root / "self-test-use.sqlite3", timeout=5)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS used (token_hash TEXT PRIMARY KEY, used_at REAL NOT NULL)")
                db.execute("INSERT INTO used VALUES (?,?)", (
                    hashlib.sha256(self.self_test_token.encode()).hexdigest(), self.wall_clock()))
        except sqlite3.IntegrityError as exc:
            raise SafetyError("self-test challenge was already consumed") from exc
        finally:
            db.close()
        self.self_test_consumed = True
        self.status = "SELF_TEST_GENERATING"
        if not self.session.receive_self_test(row, self.self_test_token):
            self.status = "SELF_TEST_REJECTED"

    def _packet(self, packet):
        stage = packet.get("stage")
        barrier=getattr(self,'_reset_barrier',None)
        if barrier:
            if stage=='unbound' and packet.get('reset_id')==barrier:
                self._reset_barrier=None
            elif stage not in ('heartbeat','fatal','stopped'):
                # FIFO pipe acknowledgement proves all older lifecycle/events
                # were discarded before the worker can establish a new baseline.
                self.rejected_packets+=1
                return
        if stage in {"candidate", "group_bound", "baseline_complete", "listener_ready", "event", "unbound", "blocked", "fatal", "stopped", "self_test_observation", "self_test_expired"}:
            row = {"stage": stage, "elapsed_seconds": round(self.clock() - self.started_at, 3)}
            if "reason" in packet:
                row["reason"] = metadata_reason(packet["reason"])
            self.status_rows.append(row)
        if stage == "heartbeat":
            self.last_heartbeat = self.clock()
        elif stage == "candidate":
            window, header, layout = packet.get("window"), packet.get("header"), packet.get("layout")
            if not isinstance(window, dict) or not isinstance(header, dict) or not isinstance(layout, dict):
                raise ValueError("invalid candidate")
            title = TARGET_GROUP if header.get("exact_match") is True and header.get("complete") is True else ""
            self.candidate = {"window": window, "header": title, "header_metadata": header, "layout": layout}
            self.candidate_observed = self.clock()
            self.reason = metadata_reason(header.get("reason"))
            if not self.bound:
                self.status = ("SELF_TEST_EXPIRED" if self.self_test_expired else
                               "SELF_TEST_CONSUMED" if self.self_test_consumed else
                               "WAITING_USER_BINDING" if self.gate.allows() else "PAUSED")
        elif stage == "group_bound":
            if not self.gate.allows() or self.action == "diagnose":
                return
            if packet.get("capture_session_id") != self.capture_session_id:
                raise ValueError("wrong capture session")
            if self.action == "self-test" and (not self.self_test_confirmed or self.self_test_consumed or self.self_test_expired):
                raise ValueError("self-test binding without explicit fresh confirmation")
            self.session.bind(packet.get("binding_id"), packet.get("capture_session_id"), packet.get("capture_epoch"))
            if self.action == "self-test":
                self.self_test_binding = (packet.get("binding_id"), packet.get("capture_session_id"), packet.get("capture_epoch"))
            self.bound = True
            self.baseline_complete = self.listener_ready = False
            self.status = "GROUP_BOUND"
            if self.action == 'run':
                from .send import SendContext
                self.send_context=SendContext(packet['binding_id'],packet['capture_session_id'],
                                              packet['capture_epoch'],packet['window'])
        elif stage == "baseline_complete":
            if self.action == "self-test":
                raise ValueError("self-test cannot report normal incoming baseline")
            if self.bound and self.gate.allows():
                self.baseline_complete = True
                self.status = "BASELINE_COMPLETE"
        elif stage == "listener_ready":
            if self.action == "self-test":
                raise ValueError("self-test cannot report incoming listener readiness")
            if not self.bound or not self.baseline_complete or not self.gate.allows():
                raise ValueError("listener ready before bound baseline")
            self.listener_ready = True
            self.status = "LISTENER_READY"
        elif stage == "event":
            if self.action in {"diagnose", "self-test"} or not self.listener_ready or not self.gate.allows():
                self.rejected_packets += 1
                return
            row = packet.get("event")
            if not isinstance(row, dict):
                raise ValueError("invalid visual event")
            event = VisualEvent(**row)
            if event.source == "synthetic" or event.capture_session_id != self.capture_session_id:
                raise ValueError("non-live event on real worker pipe")
            if self.session.receive(event):
                self.events += 1
        elif stage == "self_test_observation":
            self._self_test_observation(packet)
        elif stage == 'send_observation':
            if self.action != 'run' or not self.send_controller:
                raise ValueError('send observation outside continuous mode')
            self.send_controller.observe(packet.get('observation'))
        elif stage == 'send_observation_rejected':
            if self.send_controller:
                self.send_controller.disable('binding_invalid')
        elif stage in {"self_test_waiting", "self_test_captured"}:
            if self.action != "self-test":
                raise ValueError("self-test stage in normal listener")
            if self.bound and not self.self_test_consumed and not self.self_test_expired:
                self.status = "SELF_TEST_READING"
        elif stage == "self_test_expired":
            if self.action != "self-test":
                raise ValueError("self-test stage in normal listener")
            if not self.self_test_consumed:
                self.self_test_expired = True
                self._invalidate("SELF_TEST_EXPIRED")
                self._command("pause")
        elif stage == "frame":
            self.frames += 1
            stats = packet.get("stats", packet)
            if isinstance(stats, dict):
                self.frame_stats = {k: v for k, v in stats.items()
                                    if k in {"capture_ms", "ocr_ms", "changed", "message_count", "visible_tracks", "rejected", "rss_bytes", "cpu_percent"}
                                    and type(v) in (int, float, bool) and (type(v) is not float or math.isfinite(v))}
        elif stage == "blocked":
            self.reason = metadata_reason(packet.get("reason", "worker_blocked"))
            self._invalidate("BLOCKED")
        elif stage == "unbound":
            self.reason = metadata_reason(packet.get("reason"))
            confirmed = self.self_test_confirmed and packet.get("reason") == "manual_confirmation"
            self._invalidate("UNBOUND")
            self.self_test_confirmed = confirmed
        elif stage in {"fatal", "stopped"}:
            self.stop("WORKER_FATAL" if stage == "fatal" else "WORKER_STOPPED")
        else:
            raise ValueError("unknown worker stage")

    def poll(self):
        if self._closed:
            return
        capture_gap = self.process is not None and self.clock()-self._last_poll>5
        if capture_gap:
            self.reason='capture_gap'
            self._invalidate('CAPTURE_GAP')
        self._sync_pause()
        if capture_gap:
            # Reset the worker's baseline without undoing an existing user or
            # global pause. An unlocked desktop alone is not resume permission.
            self._command('resume' if self.gate.allows() else 'pause')
        if (self.action == "self-test" and self.self_test_started is not None and
                not self.self_test_consumed and not self.self_test_expired and
                self.clock() - self.self_test_started >= 300):
            self.self_test_expired = True
            self._invalidate("SELF_TEST_EXPIRED")
            self._command("pause")
        if self._reader_fault.is_set():
            self.stop("WORKER_PROTOCOL_FAILED")
            return
        if self.process is not None and self.process.poll() is not None:
            self.stop("WORKER_EXITED")
            return
        if self.last_heartbeat is not None and self.clock() - self.last_heartbeat > 10:
            self.stop("WORKER_HEARTBEAT_TIMEOUT")
            return
        for _ in range(64):
            try:
                packet = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._packet(packet)
            except (TypeError, ValueError, SafetyError):
                self.rejected_packets += 1
                self.stop("WORKER_PACKET_REJECTED")
                return
            if self._closed:
                return
        if self.session is not None:
            self._tag_generating_reply()
            self.session.tick()
            self._tag_generating_reply()
        current = self.clock()
        observable = bool(self.listener_ready and self.gate.allows() and self.last_heartbeat is not None and
                          current - self.last_heartbeat <= 10)
        if observable and self._previous_ready:
            self.ready_seconds += max(0, min(5, current - self._last_poll))
        self._last_poll, self._previous_ready = current, observable
        preview = self.preview
        if isinstance(preview, dict) and preview.get("trace_id") and preview["trace_id"] not in self._preview_ids:
            self._preview_ids.add(preview["trace_id"])
            if self.action == "self-test":
                self.self_test_preview_count += 1
                self.status = "SELF_TEST_PREVIEW_READY"
            else:
                self.preview_count += 1
                self._offer_preview_to_send(preview)
            self.last_preview_metadata = {
                "input_length": len(preview.get("input", "")), "reply_length": len(preview.get("reply", "")),
                "chunks": len(preview.get("chunks", [])), "personality_called": preview.get("personality_called") is True,
                "memory_recalled": preview.get("memory_recalled", 0), "send_blocked": "local_preview_sink",
                "evidence_kind": "real_self_test" if self.action == "self-test" else "real_incoming"}
            self.status_rows.append({"stage": "self_test_preview" if self.action == "self-test" else "local_preview", "elapsed_seconds": round(current - self.started_at, 3)})
        if self.send_controller is not None:
            self.send_controller.tick()
        self.persist()

    @property
    def preview(self):
        return self.session.preview if self.session is not None and not self._closed else None

    def _business_snapshot(self):
        if self._shutdown_business is not None:
            return self._shutdown_business
        return self.session.snapshot() if self.session is not None else {}

    def snapshot(self):
        business = self._business_snapshot()
        send = self.send_controller.snapshot() if self.send_controller else None
        service=getattr(self.session,'service',None)
        pipeline=('PAUSED' if not self.gate.allows() else
                  'WINDOW_UNAVAILABLE' if not self.listener_ready else
                  'DELIVERY_UNKNOWN' if send and not send['enabled'] and (send.get('last_result') or {}).get('status')=='SEND_UNCERTAIN' else
                  'GENERATING' if getattr(service,'job',None) else
                  'WAITING_SEND' if send and (send['active_stage'] or send['queue_size']) else
                  'MODEL_UNAVAILABLE' if business.get('status')=='model_unavailable' else 'WAITING_NEW_MESSAGE')
        return {"transport": "visual", "action": self.action, "target": TARGET_GROUP,
                "desktop_available": self.desktop_available, "desktop_reason": self.desktop_reason,
                "status": self.status, "pipeline_status":pipeline, "reason": metadata_reason(self.reason), "capture": self.config["backend"], "memory":business.get('memory','shadow'),
                "dry_run": not bool(send and send['enabled']), "wechat_input_enabled": bool(send and send['enabled']),
                "send": send, "bound": self.bound,
                "baseline_complete": self.baseline_complete, "listener_ready": self.listener_ready,
                "paused": not self.gate.allows(), "frames": self.frames, "events": self.events,
                "rejected_packets": self.rejected_packets, "model_admissions": business.get("model_admissions"),
                "model_remaining": business.get("model_remaining"), "preview_count": self.preview_count,
                "self_test_consumed": self.self_test_consumed,
                "self_test_expired": self.self_test_expired,
                "self_test_preview_count": self.self_test_preview_count,
                "self_test_status": business.get("self_test_status"),
                "self_test_observations": business.get("self_test_observations", 0),
                "self_test_model_calls": business.get("self_test_model_calls", 0),
                "effective_ready_seconds": round(self.ready_seconds, 3), "closed": self._closed,
                "stop_reason": self.stop_reason, "cleanup_errors": list(self.cleanup_errors)}

    def persist(self, force=False):
        """Only whitelisted metadata is durable; never save packets or previews."""
        current = self.clock()
        if not force and self._last_persist is not None and current - self._last_persist < 5:
            return
        # A waiting instance has not acquired the control lease yet. It must not
        # overwrite the status file of an active UI/CLI coordinator.
        if self.lease is None:
            return
        business = self._business_snapshot()
        counters = {key: business[key] for key in
                    ("received", "model_calls", "sent", "dry_runs", "ignored", "memory_written", "errors")
                    if key in business and type(business[key]) is int}
        log = getattr(self.session, "log", None)
        logs = list(getattr(log, "rows", ()))
        row = {**self.snapshot(), "schema_version": 1, "observed_at": self.wall_clock(),
               "elapsed_seconds": round(current - self.started_at, 3), "worker_pid": getattr(self.process, "pid", None),
               "frame_stats": self.frame_stats, "business_counts": counters,
               "last_preview": self.last_preview_metadata, "pipeline_stages": logs[-64:],
               "worker_stages": list(self.status_rows), "evidence_kind": "real_visual_runner_metadata",
               "cpu_scope": "worker_process; percent of one logical CPU; sampling method reported by worker",
               "model_counts_are": "durable admissions upper bound; failed/cancelled requests are not refunded",
               "wechat_input": ({"clicks": self.send_controller.snapshot()['clicks'],
                                  "keys": self.send_controller.snapshot()['keys'], "pastes":0,
                                  "sends":self.send_controller.snapshot()['submit_attempts']}
                                 if self.send_controller else {"clicks":0,"keys":0,"pastes":0,"sends":0}),
               "formal_memory_writes": 0, "desktop_private_memory_writes":0,
               "persistent_group_memory_writes":business.get('memory_written',0) if self.action=='run' else 0}
        self.round_root.mkdir(parents=True, exist_ok=True)
        path = self.round_root / "status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        self._last_persist = current

    def stop(self, reason="STOPPED"):
        if self._closed:
            return
        # Revoke observable readiness and preview before invoking any callback.
        # Each cleanup stage is independent so a failed native wait or a failed
        # shadow-memory close cannot skip the other resources or the lease.
        self._closed = True
        self.stop_reason = reason
        self.bound = self.baseline_complete = self.listener_ready = False
        self.candidate = self.candidate_observed = None
        self._previous_ready = False
        self.status = reason
        self._shutdown_business = {}

        def failed(stage, exc):
            self.cleanup_errors.append({"stage": stage, "error_type": type(exc).__name__})
            self.status = "STOP_FAILED"

        def attempt(stage, callback):
            try:
                return callback()
            except Exception as exc:
                failed(stage, exc)
                return None

        def wait_owned(stage):
            try:
                process.wait(timeout=1)
                return True
            except subprocess.TimeoutExpired:
                return False
            except Exception as exc:
                failed(stage, exc)
                return False

        process = self.process
        try:
            attempt("pause", self._sync_pause)
            if self.send_controller is not None:
                attempt('send_stop',lambda:self.send_controller.disable('user_stopped',stopped=True))
            attempt("invalidate", lambda: self._invalidate(reason))
            attempt("stop_command", lambda: self._command("stop"))
            if process is not None:
                try:
                    if not wait_owned("worker_wait"):
                        # Only our Popen handle, never a discovered WeChat/MAA PID.
                        attempt("worker_terminate", process.terminate)
                        if not wait_owned("worker_terminate_wait"):
                            attempt("worker_kill", process.kill)
                            if not wait_owned("worker_kill_wait"):
                                failed("worker_exit_unconfirmed", TimeoutError())
                finally:
                    for name in ("stdin", "stdout", "stderr"):
                        stream = getattr(process, name, None)
                        if stream is not None:
                            attempt("close_" + name, stream.close)
            if self.session is not None:
                business = attempt("session_snapshot", self.session.snapshot)
                if isinstance(business, dict):
                    self._shutdown_business = business
                attempt("session_close", lambda: self.session.close(timeout=3))
            # Dropped packets may include OCR bodies; keep none after shutdown.
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self.status = "STOP_FAILED" if self.cleanup_errors else reason
            attempt("persist_status", lambda: self.persist(force=True))
        finally:
            if self.lease is not None:
                attempt("lease_release", self.lease.release)
        if self.cleanup_errors:
            self.status = "STOP_FAILED"
            raise StopCleanupError(self.cleanup_errors) from None
