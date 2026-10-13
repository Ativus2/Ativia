"""Continuous, explicitly armed send sessions; each reply can submit at most once.

No native API, OCR, model or clipboard lives here. The UI loop drives one stage
at a time. Observations come from the existing input-free worker. Only the main
process's narrowly scoped native backend can perform an admitted input action.
"""
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import unicodedata
import uuid

from .types import TARGET_GROUP


def normalized(text):
    if not isinstance(text, str):
        raise ValueError('invalid_text')
    return ''.join(unicodedata.normalize('NFKC', text).split())


def window_key(row):
    return (row['hwnd'], row['pid'], row['process_started'], row['class_name'], row['title'],
            tuple(row['client_rect']), tuple(row['client_screen']), row['dpi'])


class SendRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class SendContext:
    binding_id: str
    capture_session_id: str
    capture_epoch: int
    window: dict
    group: str = TARGET_GROUP

    def __post_init__(self):
        if self.group != TARGET_GROUP or not self.binding_id or not self.capture_session_id:
            raise ValueError('invalid_send_target')
        if type(self.capture_epoch) is not int or self.capture_epoch < 0:
            raise ValueError('invalid_capture_epoch')
        key = window_key(self.window)
        if not key[0] or not key[1] or not key[3] or self.window.get('minimized'):
            raise ValueError('invalid_send_window')
        if self.window.get('process_name', '').casefold() not in ('weixin.exe', 'wechat.exe'):
            raise ValueError('invalid_send_process')


@dataclass(frozen=True)
class SendReply:
    reply_id: str
    text: str = field(repr=False)
    source_event_id: str
    source_track_id: str
    source_binding_id: str
    conversation_id: str
    model_request_id: str
    created_at: float
    epoch: int
    source: str = 'model'

    def __post_init__(self):
        for key in ('reply_id', 'source_event_id', 'source_binding_id', 'conversation_id'):
            value = getattr(self, key)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError('invalid_reply_provenance')
        if (not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 1800 or
                any(ord(ch) < 32 and ch not in '\n\r\t' for ch in self.text)):
            raise ValueError('empty_invalid_or_oversized_reply')
        if self.source not in ('model', 'manual') or type(self.epoch) is not int:
            raise ValueError('invalid_reply_source')


@dataclass
class WechatSendSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    epoch: int = 0
    armed: bool = False
    paused: bool = False
    state: str = 'SEND_DISABLED'
    target_binding_id: str = ''
    target_hwnd: int = 0
    target_pid: int = 0
    target_process_start_time: float = 0
    target_title: str = ''
    target_chat_identity: str = TARGET_GROUP
    armed_at: float | None = None
    last_send_at: float | None = None
    send_attempts: int = 0
    send_confirmed: int = 0
    send_uncertain: int = 0
    send_failed: int = 0
    send_cancelled: int = 0


class SendLedger:
    """Reserve event/reply and submit intent durably, before any external input."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.executescript('CREATE TABLE IF NOT EXISTS replies ('
                            'reply_id TEXT PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,'
                            'submitted INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);')
        finally:
            db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def reserve(self, task):
        db = self.connect()
        try:
            with db:
                db.execute('INSERT INTO replies(reply_id,event_key,status,created) VALUES (?,?,?,?)',
                           (task.reply_id, task.source_binding_id+':'+task.source_event_id, 'QUEUED', time.time()))
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            db.close()

    def submit(self, reply_id):
        db = self.connect()
        try:
            with db:
                changed = db.execute("UPDATE replies SET submitted=1,status=? WHERE reply_id=? AND submitted=0 AND status='QUEUED'",
                                     ('SUBMIT_INTENT', reply_id)).rowcount
                if changed != 1:
                    raise SendRejected('duplicate_submit')
        finally:
            db.close()

    def result(self, reply_id, status):
        db = self.connect()
        try:
            with db:
                db.execute('UPDATE replies SET status=? WHERE reply_id=?', (status, reply_id))
        finally:
            db.close()

    def recover(self):
        """Called only by the controller that holds the exclusive run lease.

        A durable intent can mean either side of Enter; never replay it. Queued
        work belongs to the old binding and cannot survive a process restart.
        """
        db = self.connect()
        try:
            with db:
                db.execute("UPDATE replies SET status='SEND_UNCERTAIN' WHERE status='SUBMIT_INTENT'")
                db.execute("UPDATE replies SET status='DROP_STALE_REPLY' WHERE status='QUEUED'")
                counts = dict(db.execute('SELECT status,COUNT(*) FROM replies GROUP BY status'))
                last = db.execute("SELECT reply_id FROM replies WHERE status='SEND_UNCERTAIN' ORDER BY created DESC LIMIT 1").fetchone()
            return counts, last[0] if last else None
        finally:
            db.close()


class WechatSendController:
    def __init__(self, root, backend, request_observation, end_observation, *, valid,
                 clock=time.monotonic, wall=time.time, on_result=None):
        self.root = Path(root)
        self.ledger = SendLedger(self.root/'send-ledger.sqlite3')
        self.backend = backend
        self.request_observation, self.end_observation = request_observation, end_observation
        self.valid = valid
        self.clock, self.wall = clock, wall
        self.session = WechatSendSession()
        self.context = None
        self.queue = deque()
        self.active = None
        self.phase = None
        self.awaiting = None
        self.observations = deque(maxlen=4)
        self.rows = deque(maxlen=256)
        self.last_result = None
        self.on_result = on_result
        self.last_submit = -math.inf
        self.write_attempts = 0
        self.stage_deadline = None
        self.transaction_started = None
        self.submitted = False
        self.baseline_count = 0
        self.verify_box = None
        self.verify_frames = 0
        self._offset = 0
        self._closed = False
        self.recovered_counts, unknown_id = self.ledger.recover()
        if unknown_id:
            self.last_result = dict(reply_id=unknown_id,status='SEND_UNCERTAIN',reason='recovered_delivery_unknown')
            self.log('DELIVERY_UNKNOWN_RECOVERED',count=self.recovered_counts['SEND_UNCERTAIN'])

    def log(self, stage, reason='', **metadata):
        task = self.active
        row = dict(timestamp=self.wall(), stage=stage, reason=reason, session_id=self.session.session_id,
                   epoch=self.session.epoch, binding_id=self.session.target_binding_id,
                   hwnd=self.session.target_hwnd, pid=self.session.target_pid,
                   reply_id=task.reply_id if task else None,
                   event_id=task.source_event_id if task else None,
                   **metadata)
        self.rows.append(row)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'send-lifecycle.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False)+'\n')

    def arm(self, context, *, confirmed, ready):
        if self._closed or confirmed is not True or ready is not True or not self.valid(context):
            raise SendRejected('fresh_bound_ready_confirmation_required')
        if self.active is not None:
            raise SendRejected('send_in_progress')
        self.context = context
        s = self.session
        s.epoch += 1
        s.armed, s.paused, s.state = True, False, 'SEND_ARMED'
        s.target_binding_id = context.binding_id
        s.target_hwnd, s.target_pid = context.window['hwnd'], context.window['pid']
        s.target_process_start_time = context.window['process_started']
        s.target_title, s.armed_at = context.window['title'], self.wall()
        self.log('SEND_SESSION_ARMED')

    def disable(self, reason='user_disabled', *, paused=False, stopped=False):
        if self._closed:
            return
        s = self.session
        s.epoch += 1
        s.armed, s.paused = False, paused
        s.state = 'SEND_STOPPED' if stopped else 'SEND_PAUSED' if paused else 'SEND_DISABLED'
        if self.active:
            self.finish('SEND_UNCERTAIN' if self.submitted else 'SEND_CANCELLED', reason)
        while self.queue:
            task = self.queue.popleft()
            try:self.ledger.result(task.reply_id, 'DROP_STALE_REPLY')
            except Exception:pass  # Already disarmed; recovery drops the old QUEUED row.
            self._notify_result(task,'SEND_CANCELLED',reason)
            s.send_cancelled += 1
        self.log('SEND_SESSION_DISABLED', reason)
        if stopped:
            self._closed = True

    def enqueue(self, task):
        s = self.session
        if (not s.armed or s.paused or self._closed or not self.context or not self.valid(self.context) or
                task.epoch != s.epoch or task.source_binding_id != self.context.binding_id):
            return False
        if not 0 <= self.wall()-task.created_at <= 30 or len(self.queue) >= 16:
            return False
        if not self.ledger.reserve(task):
            return False
        self.queue.append(task)
        self.log('SEND_TASK_CREATED', reply_id_queued=task.reply_id, event_id_queued=task.source_event_id,
                 source_track_id=task.source_track_id, conversation_id=task.conversation_id,
                 model_request_id=task.model_request_id, source=task.source,
                 length=len(task.text), text_hash=hashlib.sha256(task.text.encode()).hexdigest())
        return True

    def allows(self):
        return bool(self.active and self.context and self.session.armed and not self.session.paused and
                    not self._closed and self.active.epoch == self.session.epoch and self.valid(self.context) and
                    self.wall()-self.active.created_at <= 30)

    def _request(self, phase):
        if not self.allows():
            raise SendRejected('stale_before_observation')
        self.phase = phase
        self.awaiting = uuid.uuid4().hex
        self.stage_deadline = self.clock()+6
        self.request_observation(dict(request_id=self.awaiting, transaction_id=self.active.reply_id,
                                      binding_id=self.context.binding_id, capture_session_id=self.context.capture_session_id,
                                      capture_epoch=self.context.capture_epoch, phase=phase))

    def observe(self, row):
        if isinstance(row, dict) and row.get('request_id') == self.awaiting:
            self.observations.append(row)

    def _validate(self, row):
        if (not self.allows() or row.get('binding_id') != self.context.binding_id or
                row.get('capture_session_id') != self.context.capture_session_id or
                row.get('capture_epoch') != self.context.capture_epoch or row.get('group') != TARGET_GROUP or
                row.get('header_exact') is not True or window_key(row['window']) != window_key(self.context.window) or
                type(row.get('observed_at')) not in (int,float) or not 0 <= self.wall()-row['observed_at'] <= 3):
            raise SendRejected('target_or_binding_changed')
        self.backend.check(self.context, focused=self.phase != 'PREPARE')

    def tick(self):
        if self._closed:
            return
        if self.active is None:
            if not self.session.armed or not self.queue or self.clock()-self.last_submit < 2:
                return
            self.active = self.queue.popleft()
            self.submitted = False
            self.transaction_started = self.clock()
            self.verify_box, self.verify_frames, self._offset = None, 0, 0
            if not self.allows():
                self.finish('SEND_CANCELLED', 'DROP_STALE_REPLY')
                return
            self.session.send_attempts += 1
            try:
                self.backend.begin(self.context, self.allows)
                self._request('PREPARE')
            except Exception as exc:
                self.failure(exc)
            return
        if not self.allows():
            self.finish('SEND_UNCERTAIN' if self.submitted else 'SEND_CANCELLED', 'stale_or_paused')
            return
        try:
            if self.phase == 'WRITE':
                text = self.active.text.replace('\r\n','\n').replace('\r','\n').replace('\t',' ')
                chunk = text[self._offset:self._offset+32]
                if chunk:
                    self.backend.write(chunk)
                    self._offset += len(chunk)
                    return
                self.log('SEND_TEXT_WRITTEN', length=len(text))
                self._request('PRE_SUBMIT')
                return
            if self.clock() > self.stage_deadline:
                self.finish('SEND_UNCERTAIN' if self.submitted else 'SEND_FAILED', 'observation_timeout')
                return
            if not self.observations:
                return
            row = self.observations.popleft()
            if row.get('request_id') != self.awaiting:
                return
            self._validate(row)
            if self.phase == 'PREPARE':
                if row.get('input_empty') is not True:
                    self.finish('SEND_CANCELLED', 'INPUT_NOT_EMPTY')
                    return
                self.baseline_count = sum(normalized(item.get('text','')) == normalized(self.active.text)
                                          for item in row.get('own_bubbles', []))
                self.log('SEND_TARGET_VALIDATED')
                self.backend.focus(row['composer'])
                self._request('FOCUSED')
            elif self.phase == 'FOCUSED':
                if row.get('input_empty') is not True or row.get('input_focused') is not True:
                    self.finish('SEND_CANCELLED', 'input_focus_or_draft_changed')
                    return
                self.log('SEND_INPUT_FOCUSED')
                self.write_attempts += 1
                self.phase = 'WRITE'
                self.stage_deadline = self.clock()+10
            elif self.phase == 'PRE_SUBMIT':
                if (row.get('input_complete') is not True or row.get('input_focused') is not True or
                        normalized(row.get('input_text','')) != normalized(self.active.text)):
                    self.finish('SEND_CANCELLED', 'input_content_not_verified')
                    return
                self.log('SEND_PRE_SUBMIT_VALIDATED')
                # Final native identity/focus/user interaction and gate check is
                # performed again inside backend.submit immediately at SendInput.
                self.ledger.submit(self.active.reply_id)
                self.submitted = True
                self.log('SEND_SUBMIT_ATTEMPT')
                self.backend.submit()
                self.last_submit = self.clock()
                self.session.last_send_at = self.wall()
                self.verify_deadline = self.clock()+8
                self._request('VERIFY')
            elif self.phase == 'VERIFY':
                matches = [b for b in row.get('own_bubbles', []) if normalized(b.get('text','')) == normalized(self.active.text)
                           and b.get('side') == 'right' and b.get('complete') is True and b.get('confidence',0) >= .9]
                if len(matches) > self.baseline_count and row.get('input_empty') is True:
                    box = matches[-1].get('box')
                    self.verify_frames = self.verify_frames+1 if box == self.verify_box else 1
                    self.verify_box = box
                    if self.verify_frames >= 2:
                        self.log('SEND_VISUAL_CONFIRMATION', matching_bubbles=len(matches), stable_frames=self.verify_frames,
                                 confidence=matches[-1]['confidence'], normalized_length=len(normalized(self.active.text)))
                        self.finish('SEND_CONFIRMED')
                        return
                else:
                    self.verify_box, self.verify_frames = None, 0
                if self.clock() >= self.verify_deadline:
                    self.finish('SEND_UNCERTAIN', 'own_bubble_not_confirmed')
                else:
                    self._request('VERIFY')
        except Exception as exc:
            self.failure(exc)

    def failure(self, exc):
        reason = str(exc) if isinstance(exc, SendRejected) else type(exc).__name__
        self.finish('SEND_UNCERTAIN' if self.submitted else 'SEND_CANCELLED' if isinstance(exc, SendRejected) else 'SEND_FAILED', reason)
        if reason in ('target_or_binding_changed','window_identity_changed','native_input_partial','window_closed'):
            self.disable(reason)

    def _notify_result(self,task,status,reason):
        if self.on_result is not None:
            try:self.on_result(task,status,reason,self.wall())
            except Exception as exc:
                # Result persistence and this optional business sink are separate.
                try:self.log('SEND_RESULT_SINK_FAILED',type(exc).__name__,notice_reply_id=task.reply_id)
                except Exception:pass

    def finish(self, status, reason=''):
        task = self.active
        if task is None:
            return
        try:
            durable=True
            try:
                self.ledger.result(task.reply_id, status)
                self.log(status, reason, latency_ms=round((self.clock()-self.transaction_started)*1000))
            except Exception as exc:
                durable=False
                status='SEND_UNCERTAIN' if self.submitted else 'SEND_FAILED'
                reason='result_persistence_failed:'+type(exc).__name__
                # Retain an unknown outcome durably when possible; an earlier
                # SUBMIT_INTENT remains the recovery barrier if storage is down.
                try:self.ledger.result(task.reply_id,status)
                except Exception:pass
            key = {'SEND_CONFIRMED':'send_confirmed','SEND_UNCERTAIN':'send_uncertain',
                   'SEND_FAILED':'send_failed','SEND_CANCELLED':'send_cancelled'}[status]
            setattr(self.session, key, getattr(self.session,key)+1)
            self.last_result = dict(reply_id=task.reply_id, status=status, reason=reason)
            if status == 'SEND_UNCERTAIN' or not durable:
                # Keep receiving diagnostics, but do not compound an unresolved
                # external effect with more sends. No retry of this reply ID.
                self.session.armed = False
                self.session.epoch += 1
                if self.session.state not in ('SEND_STOPPED','SEND_PAUSED'):
                    self.session.state = 'SEND_DISABLED'
                while self.queue:
                    dropped = self.queue.popleft()
                    try:self.ledger.result(dropped.reply_id,'DROP_STALE_REPLY')
                    except Exception:pass
                    self._notify_result(dropped,'SEND_CANCELLED','previous_delivery_unknown')
                    self.session.send_cancelled += 1
            self._notify_result(task,status,reason)
        finally:
            cleanup_errors = []
            try:
                self.backend.end()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
            try:
                self.end_observation(task.reply_id)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
            finally:
                self.active, self.awaiting, self.phase = None, None, None
                self.observations.clear()
            if cleanup_errors:
                # A cleanup error cannot replay the completed reply or strand
                # the queue in an active transaction. Require a fresh enable.
                self.session.armed = False
                self.session.state = 'SEND_DISABLED'
                self.session.epoch += 1
                try:self.log('SEND_CLEANUP_FAILED', ','.join(cleanup_errors))
                except Exception:pass

    def snapshot(self):
        s = self.session
        return dict(enabled=s.armed, paused=s.paused, state=s.state, session_id=s.session_id, epoch=s.epoch,
                    binding_id=s.target_binding_id, attempts=s.send_attempts, confirmed=s.send_confirmed,
                    target_hwnd=s.target_hwnd, target_pid=s.target_pid,
                    target_process_start_time=s.target_process_start_time, target_title=s.target_title,
                    target_chat_identity=s.target_chat_identity, armed_at=s.armed_at, last_send_at=s.last_send_at,
                    uncertain=s.send_uncertain, failed=s.send_failed, cancelled=s.send_cancelled,
                    write_attempts=self.write_attempts, submit_attempts=getattr(self.backend,'submit_attempts',0),
                    clicks=getattr(self.backend,'clicks',0), keys=getattr(self.backend,'keys',0), pastes=0,
                    queue_size=len(self.queue), active_stage=self.phase, last_result=self.last_result,
                    recovered_counts=dict(self.recovered_counts))
