"""Visual input -> existing Ativia business logic -> a local-only preview.

Continuous mode owns a dedicated group Memory; test modes keep fresh shadows.
Test modes share a durable round
budget; continuous mode uses the existing service policy without that test cap.
Nothing here starts wxauto or exposes input.
The runner owns capture, manual binding, the user-wide lease and its IPC worker.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import math
from pathlib import Path
import tempfile
import threading
import time

from chat_events import ChatEvent
from companion_runtime import CompanionRuntime
from conversation_core import AtiviaConversation
from performance.scheduler import TaskScheduler
from ..config import WechatConfig
from ..service import ReplyJob, WechatService
from .safety import PersistentModelBudget, SafetyError
from .types import TARGET_GROUP, VisualEvent


class MetadataLog:
    """Bounded local status only. Message/reply bodies are never logged here."""
    def __init__(self):
        self.rows = deque(maxlen=256)

    def emit(self, stage, **fields):
        allowed = {"status", "reason", "respond", "version", "count", "error_type",
                   "model_called", "memory_written", "candidate", "category",
                   "confidence", "request_id", "ms"}
        self.rows.append({"stage": stage, **{k: v for k, v in fields.items() if k in allowed}})

    def text(self, *_):
        pass

    def close(self):
        pass


class BudgetedConversation:
    """Persistent pre-admission around the existing zero-retry reply endpoint.

    Admissions are conservative upper bounds on HTTP calls; cancellation or an
    error consumes the slot. Vision parsing must consume from this same budget.
    """
    def __init__(self, core, budget: PersistentModelBudget):
        self.core = core
        self.budget = budget

    def handle(self, event, events, **kwargs):
        if kwargs["cancelled"]() or not kwargs["ticket"].valid():
            return ""
        self.budget.consume("reply", "reply:" + kwargs["ticket"].request_id)
        if kwargs["cancelled"]() or not kwargs["ticket"].valid():
            return ""
        # AtiviaConversation invokes vision.respond_to_voice, whose client has
        # max_retries=0. Never wrap this call in automatic retry logic.
        return self.core.handle(event, events, **kwargs)


def _no_adapter(_config):
    raise SafetyError("visual session must never start a wxauto adapter")


class VisualSession:
    def __init__(self, root, gate, settings, *, scheduler=None, budget=None,
                 model_enabled=False, self_test_mode=False, continuous_model=False):
        if type(model_enabled) is not bool or type(self_test_mode) is not bool:
            raise ValueError("model_enabled and self_test_mode must be explicit boolean")
        if type(continuous_model) is not bool or continuous_model and self_test_mode:
            raise ValueError("continuous mode is separate from self-test")
        self.continuous_model = continuous_model
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal=None
        self._journal_preview_id=None
        if continuous_model:
            from .journal import VisualJournal
            self.journal=VisualJournal(self.root/'event-journal.sqlite3')
            self.journal.reconcile(self.root/'send/send-ledger.sqlite3')
        self.budget_path = self.root / "model-budget.sqlite3"
        if budget is not None and (not isinstance(budget, PersistentModelBudget) or
                                   budget.path.resolve() != self.budget_path):
            raise ValueError("visual reply and vision must share the fixed round budget path")
        if continuous_model and budget is not None:
            raise ValueError("continuous mode must not inherit test budget")
        self.budget = None if continuous_model else budget or PersistentModelBudget(self.budget_path)
        self.model_enabled = model_enabled
        self.self_test_mode = self_test_mode
        self._self_lock = threading.RLock()
        self._self_test_consumed = False
        self._self_test_started_at = time.monotonic()
        self._self_job = None
        self._self_preview = None
        self._self_model_counted = False
        self._self_counts = dict(self_test_observations=0, self_test_model_calls=0, self_test_previews=0)
        self._self_status = "WAITING_SELF_TEST" if self_test_mode else "DISABLED"
        self.gate = gate
        self.scheduler = scheduler or TaskScheduler()
        self.closed = False
        self.shadow_directory = None
        if continuous_model:
            self.memory_directory = self.root / 'group-memory'
            self.memory_directory.mkdir(exist_ok=True)
        else:
            shadow_root = self.root / "shadow"
            shadow_root.mkdir(exist_ok=True)
            self.shadow_directory = Path(tempfile.mkdtemp(prefix="run-", dir=shadow_root))
            self.memory_directory = self.shadow_directory
        # Same MemoryService/Store and Personality, with an explicit isolated
        # database. Never copy the desktop private database or old test shadows.
        self.runtime = CompanionRuntime(self.memory_directory / "companion.json")
        if not self.runtime.memory.ready.wait(5) or self.runtime.memory.store is None:
            self.runtime.close()
            raise RuntimeError("visual memory failed to initialize")
        self.runtime.memory.paused = not gate.allows()
        self.log = MetadataLog()
        core = AtiviaConversation(self.runtime, settings)
        self.core = core if continuous_model else BudgetedConversation(core, self.budget)
        config = WechatConfig(enabled=True, dry_run=True, allowed_groups=(TARGET_GROUP,),
                              bot_names=("Ativia",), owner_names=(), owner_ids=(),
                              proactive_enabled=False, memory_enabled=True, debug=False)
        # The standard config parser requires owner assignment for wxauto. A
        # visual session deliberately has no owner mapping and never parses or
        # persists this private in-memory configuration as a wxauto config.
        self.service = WechatService(self.runtime, gate, self.scheduler, settings, config,
                                     core=self.core, log=self.log, adapter_factory=_no_adapter,
                                     require_stable_memory=continuous_model)
        self.service.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ativia-visual-preview")
        self._unsubscribe = gate.subscribe(self._on_pause)

    def _on_pause(self, state, _epoch):
        self.runtime.memory.paused = state != "RUNNING"
        if state != "RUNNING":
            self._cancel_self_test("SELF_TEST_CANCELLED_PAUSED")

    def bind(self, binding_id, capture_session_id, capture_epoch):
        if self.closed:
            raise SafetyError("visual session is closed")
        if self.service.visual_binding != (binding_id, capture_session_id, capture_epoch):
            self._cancel_self_test("SELF_TEST_CANCELLED_BINDING_CHANGED")
        self.service.bind_visual(binding_id, capture_session_id, capture_epoch)

    def invalidate(self, reason="visual_binding_invalidated"):
        self._cancel_self_test("SELF_TEST_CANCELLED_BINDING_INVALID")
        self.service.invalidate_visual(reason)

    def _cancel_self_test(self, status):
        with self._self_lock:
            self._self_preview = None
            job = self._self_job
            if self._self_test_consumed:
                self._self_status = status
            if job is not None:
                self.scheduler.cancel(job.ticket, "visual_self_test_cancelled")
                if job.future is not None:
                    job.future.cancel()

    def _self_valid(self, job):
        raw = job.event.raw_message
        return (self.self_test_mode and self.model_enabled and not self.closed and
                self.service.config.dry_run is True and self.service.connected and
                self.service.valid(job.epoch) and job.ticket.valid() and
                self.runtime.memory.privacy_epoch == job.privacy_epoch and
                self.service.visual_binding == (raw["binding_id"], raw["capture_session_id"], raw["capture_epoch"]))

    def _self_observation_valid(self, observation, expected_token):
        # This is intentionally separate from VisualEvent/receive_event. The
        # observation keeps its self identity and never becomes group ingress.
        from .self_capture import normalize_self_text, self_test_text, validate_self_token
        if not isinstance(observation, dict):
            return False
        try:
            validate_self_token(expected_token)
        except (TypeError, ValueError):
            return False
        if observation.get("token") != expected_token:
            return False
        expected = {"group_name": TARGET_GROUP, "source": "local_ocr", "self_state": "self",
                    "side": "right", "evidence_kind": "real_self_test", "kind": "text", "complete": True}
        if any(observation.get(key) != value for key, value in expected.items()) or observation.get("complete") is not True:
            return False
        text = observation.get("text")
        if not isinstance(text, str) or not text or len(text) > 4000:
            return False
        if normalize_self_text(text) != normalize_self_text(self_test_text(expected_token)):
            return False
        confidence, stamp = observation.get("confidence"), observation.get("observed_at")
        if (type(confidence) not in (int, float) or not math.isfinite(confidence) or not .9 <= confidence <= 1 or
                type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 300):
            return False
        sequence, epoch, stable = observation.get("frame_sequence"), observation.get("capture_epoch"), observation.get("stable_frames")
        if (type(sequence) is not int or sequence < 1 or type(epoch) is not int or epoch < 0 or
                type(stable) is not int or stable < 2):
            return False
        box = observation.get("box")
        if isinstance(box, dict) and set(box) == {"x", "y", "width", "height"}:
            box = [box[key] for key in ("x", "y", "width", "height")]
        if (not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(v) is not int for v in box) or
                min(box) < 0 or min(box[2:]) < 1 or max(box) > 32768):
            return False
        binding = (observation.get("binding_id"), observation.get("capture_session_id"), epoch)
        return self.service.visual_binding is not None and binding == self.service.visual_binding

    def receive_self_test(self, observation, expected_token):
        """Consume one explicit self observation; model output is local preview only.

        True means accepted once, even if the later request fails or is cancelled.
        Ordinary ingress, its self-ignore rule, buffers, candidates and counters
        are never called or changed by this diagnostic path.
        """
        if (not self.self_test_mode or not self.model_enabled or self.closed or
                not self.gate.allows() or not self.service.valid(self.gate.epoch) or
                self.service.config.dry_run is not True or not self.service.connected or
                time.monotonic() - self._self_test_started_at > 300 or
                not self._self_observation_valid(observation, expected_token)):
            return False
        with self._self_lock:
            if self._self_test_consumed or self.closed or not self.gate.allows():
                return False
            self._self_test_consumed = True
            self._self_counts["self_test_observations"] += 1
            self._self_status = "SELF_TEST_ADMITTED"
            capture = observation["capture_session_id"]
            raw = {key: observation[key] for key in ("binding_id", "capture_session_id", "capture_epoch", "source",
                                                     "self_state", "side", "evidence_kind", "confidence")}
            raw.update(transport="visual_self_test", group_name=TARGET_GROUP, ordinary_incoming=False)
            event = ChatEvent(platform="wechat_self_test", conversation_type="group",
                              conversation_id="wechat:visual:self-test:group:" + capture,
                              sender_id="wechat:visual:self-test:self:" + capture, sender_name="Ativia 本人（单次自测）",
                              text=observation["text"], timestamp=observation["observed_at"],
                              message_id="self-test:" + capture + ":" + expected_token,
                              is_at_me=False, identity_quality="self_test_session",
                              source_sequence=observation["frame_sequence"], timestamp_source="self_test_observed",
                              raw_message=raw)
            ticket = self.scheduler.admit("wechat", timeout=self.service.config.reply_timeout_seconds,
                                          request_id="self-test-" + expected_token)
            job = ReplyJob(event, 0, self.gate.epoch, self.runtime.memory.privacy_epoch, None, ticket)
            ticket.cancel_callback = job.abort
            self._self_job = job
            if not self.scheduler.start(ticket) or not self._self_valid(job):
                self._cancel_self_test("SELF_TEST_CANCELLED_BEFORE_MODEL")
                return True
            try:
                job.future = self.service.executor.submit(self._run_self_test, job, expected_token)
            except Exception as exc:
                self.scheduler.cancel(ticket, "self_test_submit_failed")
                self._self_status = "SELF_TEST_FAILED"
                self.log.emit("self_test_error", error_type=type(exc).__name__)
            return True

    def _run_self_test(self, job, token):
        if not self._self_valid(job):
            return ""
        # Same fixed persistent six-call budget. A repeated token after restart
        # also cannot spend a second slot or retry an earlier request.
        self.budget.consume("self_test", "self-test:" + token)
        if not self._self_valid(job):
            return ""
        with self._self_lock:
            if not self._self_valid(job):
                return ""
            self._self_status = "SELF_TEST_MODEL_RUNNING"
        self.log.emit("self_test_core_admitted", count=1, request_id=job.ticket.request_id)
        # Existing Personality, scoped shadow recall and zero-retry model path.
        # Budget was consumed above, so bypass only the outer budget wrapper.
        return self.core.core.handle(job.event, (job.event,), ticket=job.ticket,
                                     cancelled=lambda: not self._self_valid(job), bind_cancel=job.bind,
                                     memory_enabled=True)

    def _count_self_provider(self, job):
        with self._self_lock:
            if job is not None and getattr(job.ticket, "wechat_model_started", False) is True and not self._self_model_counted:
                self._self_model_counted = True
                self._self_counts["self_test_model_calls"] += 1
                self.log.emit("self_test_model", model_called=True, request_id=job.ticket.request_id)

    def _finish_self_test(self):
        job = self._self_job
        if job is None or job.future is None:
            return
        self._count_self_provider(job)
        if not self._self_valid(job):
            self._cancel_self_test("SELF_TEST_CANCELLED")
        if not job.future.done():
            return
        self._self_job = None
        try:
            text = job.future.result()
            self._count_self_provider(job)
            with self._self_lock:
                if not self._self_valid(job):
                    self._self_status = "SELF_TEST_CANCELLED"
                    return
                if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                    self._self_status = "SELF_TEST_EMPTY_OR_INVALID_REPLY"
                    return
                from ..sender import split_reply
                trace = getattr(job.ticket, "wechat_trace", {})
                trace = {key: trace[key] for key in ("personality_called", "memory_recalled", "model")
                         if isinstance(trace, dict) and key in trace}
                self._self_preview = dict(group=TARGET_GROUP, sender=job.event.sender_name,
                                          trigger="self_test_one_shot", input=job.event.text, reply=text,
                                          chunks=split_reply(text), trace_id=job.ticket.request_id,
                                          memory_candidate_queued=False, send_blocked="local_preview_sink",
                                          evidence_kind="real_self_test", self_state="self",
                                          ordinary_incoming=False, acceptance_evidence=False, **trace)
                self._self_counts["self_test_previews"] += 1
                self._self_status = "SELF_TEST_PREVIEW_READY"
                self.log.emit("self_test_preview", status=self._self_status, count=1,
                              request_id=job.ticket.request_id)
        except Exception as exc:
            self._self_status = "SELF_TEST_CANCELLED" if not job.ticket.valid() else "SELF_TEST_FAILED"
            self.log.emit("self_test_error", error_type=type(exc).__name__)
        finally:
            self._count_self_provider(job)
            self.scheduler.finish(job.ticket)

    def receive(self, event, *, now=None, wall=None):
        if self.closed:
            return False
        if isinstance(event, VisualEvent):
            event = event.to_chat_event()
        if not self.service._is_visual(event):
            return False
        accepted=self.service.receive_event(event, time.monotonic() if now is None else now,
                                          time.time() if wall is None else wall)
        if accepted and self.journal:self.journal.event(event)
        return accepted

    def delivery_result(self,task,status,reason,wall):
        if self.journal:self.journal.result(task,status,reason)
        return self.service.visual_delivery(task,status,reason,wall)

    def tick(self, *, now=None, wall=None):
        """Only drive existing business stages. Never call service.tick/start."""
        if self.closed:
            return
        self._finish_self_test()
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        service = self.service
        if service.epoch != self.gate.epoch:
            service.reset_epoch()
        service.finish_db()
        if not service.valid():
            service.connected = False
            service.status = "paused"
            return
        service.finish_reply(now, wall)
        if self.journal and service.preview and service.preview['reply_id']!=self._journal_preview_id:
            self.journal.preview(service.preview)
            self._journal_preview_id=service.preview['reply_id']
        if self.model_enabled and service.connected and not self.self_test_mode:
            service.start_reply(now, wall)
        elif not self.model_enabled or self.self_test_mode:
            # Switching a listener to preview must not replay its old triggers.
            for group in service.groups.values():
                group.pending_event = None
                group.pending_decision = None
                group.visual_pending.clear()
        if service.config.memory_enabled and self.runtime.memory.enabled:
            service.consolidate(now)

    @property
    def preview(self):
        if self.self_test_mode:
            with self._self_lock:
                return dict(self._self_preview) if self._self_preview is not None and not self.closed else None
        with self.service._preview_lock:
            return dict(self.service.preview) if self.service.preview is not None else None

    def snapshot(self):
        self._count_self_provider(self._self_job)
        return {**self.service.snapshot(), "transport": "visual", "target": TARGET_GROUP,
                "binding": "GROUP_BOUND" if self.service.visual_binding else "UNBOUND",
                "memory": "persistent_group" if self.continuous_model else "shadow", "wechat_input_enabled": False,
                "model_enabled": self.model_enabled,
                "model_admissions": self.service.counts['model_calls'] if self.continuous_model else self.budget.used,
                "model_remaining": None if self.continuous_model else self.budget.remaining,
                "model_budget_mode": "existing_policy_rate_limit" if self.continuous_model else "fixed_test_budget",
                "self_test_mode": self.self_test_mode,
                **self._self_counts, "self_test_status": self._self_status,
                "self_test_model_calls_meaning": "provider entry invocations, not confirmed HTTP requests; no retries; persistent admissions are a separate upper bound"}

    def close(self, timeout=5):
        if self.closed:
            return
        self.closed = True
        self_job = self._self_job
        self._cancel_self_test("SELF_TEST_CANCELLED_CLOSED")
        self._unsubscribe()
        self.service.invalidate_visual("visual_session_closed")
        self.service.close()
        job = self.service.job
        if job is not None and job.future is not None:
            try:
                job.future.result(timeout=max(0, timeout))
            except (Exception, TimeoutError):
                pass
        if self_job is not None and self_job.future is not None:
            try:
                self_job.future.result(timeout=max(0, timeout))
            except (Exception, TimeoutError):
                pass
            self._count_self_provider(self_job)
        self.service.executor.shutdown(wait=False, cancel_futures=True)
        self.runtime.close()
        self.runtime.memory.close_future.result(timeout=max(0.1, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
