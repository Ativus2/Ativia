"""Group coordinator: bounded batches, independent model worker, one shared PauseGate."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict,dataclass,field
from pathlib import Path
import threading,time,re
from conversation_core import AtiviaConversation
from memory.memory_candidate import MemoryCandidateEvaluator,CandidateQueue
from personality.engine import RelationshipState
from chat_events import ChatEvent,OWNER_ID
from .adapter import WechatAdapter
from .context_buffer import GroupContextBuffer
from .response_policy import GroupResponsePolicy
from .message_parser import normalize
from .identity import PersonIdentity,group_id
from .logging import WechatLog

@dataclass
class GroupState:
    buffer:object
    policy:object
    pending_event:object=None
    pending_decision:object=None
    pending_at:float=0
    first_pending:float=0
    visual_pending:deque=field(default_factory=deque)

@dataclass
class ReplyJob:
    event:object
    version:int
    epoch:int
    privacy_epoch:int
    decision:object
    ticket:object
    future:object=None
    closer:object=None
    def abort(self):
        closer=self.closer
        if closer:
            self.closer=None
            def close():
                try:closer()
                except Exception:pass
            threading.Thread(target=close,name='ativia-wechat-stream-close',daemon=True).start()
    def bind(self,closer):
        self.closer=closer
        if closer and not self.ticket.valid():self.abort()

class WechatService:
    def __init__(self,runtime,gate,scheduler,settings,config,*,adapter_factory=WechatAdapter,log=None,core=None,require_stable_memory=False):
        self.runtime=runtime;self.gate=gate;self.scheduler=scheduler;self.config=config
        self.adapter_factory=adapter_factory;self.adapter=None;self.core=core or AtiviaConversation(runtime,settings)
        self.require_stable_memory=require_stable_memory
        self.log=log or WechatLog(Path(__file__).resolve().parents[2]/'logs/wechat',config.debug,enabled=config.enabled)
        self.groups={};self.evaluator=MemoryCandidateEvaluator(config.bot_names);self.candidates=CandidateQueue(config.candidate_delay_seconds)
        self.people={};self.relationships={};self.pending_relationships={};self.owner_dirty=False
        self.db_admitting=False;self.pending_db=[];self.pending_sends={};self.calls=deque();self.executor=None;self.job=None
        self.stop_event=threading.Event();self.wake=threading.Event();self.thread=None;self.closed=False;self.starting=False
        self.epoch=gate.epoch;self.connected=False;self.next_connect=0;self.failures=0;self.account_hash=''
        self.counts=dict(received=0,model_calls=0,sent=0,dry_runs=0,ignored=0,memory_written=0,errors=0)
        self.preview=None;self._preview_lock=threading.RLock();self.candidate_messages=deque(maxlen=100)
        self.visual_binding=None;self.visual_deliveries={}
        self.status='disabled' if not config.enabled else 'paused' if not gate.allows() else 'starting'
        self._unsubscribe=gate.subscribe(self.on_state)
    def start(self):
        if self.closed or not self.config.enabled or self.thread:return
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ativia-wechat-agent')
        self.thread=threading.Thread(target=self.run,name='ativia-wechat-policy',daemon=True);self.thread.start()
    def on_state(self,state,epoch):
        if state!='RUNNING':
            self.connected=False;self.status='paused'
            self.visual_binding=None
            self.visual_deliveries.clear()
            with self._preview_lock:self.preview=None
            if self.job:self.scheduler.cancel(self.job.ticket,'wechat_paused')
            if self.adapter:self.adapter.stop()
        self.wake.set()
    def valid(self,epoch=None):return not self.closed and self.config.enabled and self.gate.allows(epoch)
    def snapshot(self):
        return dict(enabled=self.config.enabled,dry_run=self.config.dry_run,status=self.status,connected=self.connected,
            groups=len(self.config.allowed_groups),**self.counts,
            pending_candidates=len(self.candidates.pending),pending_send=len(self.pending_sends),debug=self.config.debug)
    def group(self,name):
        if name not in self.groups:self.groups[name]=GroupState(GroupContextBuffer(self.config.context_messages,self.config.context_seconds),GroupResponsePolicy(self.config))
        return self.groups[name]
    def reset_epoch(self):
        self.epoch=self.gate.epoch;self.groups.clear();self.candidates.clear();self.people.clear();self.pending_relationships.clear();self.pending_sends.clear();self.owner_dirty=False
        self.visual_binding=None
    def run(self):
        try:
            while not self.stop_event.is_set():
                try:self.tick(time.monotonic(),time.time())
                except Exception as exc:
                    self.counts['errors']+=1;self.log.emit('error',error_type=type(exc).__name__)
                    self.disconnect('coordinator_error')
                self.wake.wait(.25);self.wake.clear()
        finally:
            if self.adapter:self.adapter.close()
            self.candidates.clear();self.pending_sends.clear();self.owner_dirty=False
            if self.executor:self.executor.shutdown(wait=False,cancel_futures=True)
            self.log.close()
    def disconnect(self,reason):
        self.connected=False;self.status=reason;self.failures=min(self.failures+1,6)
        self.preview=None
        self.next_connect=time.monotonic()+min(60,2**self.failures)
        if self.job:self.scheduler.cancel(self.job.ticket,'wechat_disconnected')
        if self.adapter:self.adapter.stop()
        self.groups.clear();self.candidates.clear();self.people.clear();self.pending_relationships.clear();self.pending_sends.clear();self.owner_dirty=False
        self.log.emit('connection',status=reason)
    def tick(self,now,wall):
        if self.epoch!=self.gate.epoch:self.reset_epoch()
        self.finish_db()
        if not self.valid():
            if self.adapter:self.adapter.stop()
            self.connected=False;self.status='paused';return
        if self.adapter is not None and not self.adapter.alive():
            errors=[p for p in self.adapter.receive_event() if p.get('stage')=='error']
            self.counts['errors']+=1
            self.disconnect(errors[-1].get('status','bridge_failed') if errors else 'disconnected')
            self.adapter.close();self.adapter=None;return
        if self.adapter is None:
            if now<self.next_connect:return
            self.starting=True
            try:
                if self.adapter:self.adapter.close()
                self.adapter=self.adapter_factory(self.config);self.adapter.start();self.status='connecting'
                if not self.valid(self.epoch):self.adapter.stop();return
            finally:self.starting=False
        if now-self.adapter.last_seen>35:
            self.disconnect('listener_timeout');return
        for packet in self.adapter.receive_event():
            stage=packet.get('stage')
            if stage=='ready':
                account=packet.get('account_hash','')
                if self.account_hash and account and account!=self.account_hash:self.disconnect('account_changed');self.next_connect=float('inf');continue
                if account:self.account_hash=account
                self.connected=True;self.status='listening';self.failures=0;self.log.emit('connected',count=packet.get('groups',0))
            elif stage=='message' and self.connected:self.receive(packet.get('group'),packet.get('message'),now,wall)
            elif stage=='send_result':self.sent_result(packet,wall)
            elif stage=='disconnected' and not self.connected and self.status not in ('connecting','starting'):continue
            elif stage in ('error','disconnected'):
                self.counts['errors']+=1;reason=packet.get('status','disconnected')
                self.log.emit('error',error_type=packet.get('error_type','ConnectionLost'),status=reason)
                self.disconnect(reason)
        self.finish_reply(now,wall)
        for identity,(at,job,text) in list(self.pending_sends.items()):
            if now-at>15:self.pending_sends.pop(identity);self.log.emit('send',status='unknown',request_id=identity)
        if self.connected:self.start_reply(now,wall)
        if self.config.memory_enabled and self.runtime.memory.enabled and self.valid(self.epoch):self.consolidate(now)
    def receive(self,name,raw,now,wall):
        if not self.valid(self.epoch) or name not in self.config.allowed_groups:return
        event=normalize(raw,name,self.config,now=wall)
        if event is None:self.counts['ignored']+=1;return
        return self.receive_event(event,now,wall)
    @staticmethod
    def _is_visual(event):
        raw=getattr(event,'raw_message',None)
        return (getattr(event,'transport',None)=='visual' or
                isinstance(raw,dict) and raw.get('transport')=='visual')
    def bind_visual(self,binding_id,capture_session_id,capture_epoch):
        """Accept the runner's manually confirmed binding, never infer it from text."""
        if (not self.config.dry_run or not self.valid() or
                any(not isinstance(value,str) or not value or len(value)>256
                    for value in (binding_id,capture_session_id)) or
                type(capture_epoch) is not int or capture_epoch<0):
            raise ValueError('invalid_visual_binding')
        self.invalidate_visual('visual_binding_changed')
        self.epoch=self.gate.epoch
        self.visual_binding=(binding_id,capture_session_id,capture_epoch)
        self.connected=True;self.status='visual_listening'
    def invalidate_visual(self,reason='visual_binding_invalidated'):
        self.visual_binding=None;self.connected=False;self.status=reason
        if self.job and self._is_visual(self.job.event):
            self.scheduler.cancel(self.job.ticket,reason)
        self.groups.clear();self.candidates.clear();self.people.clear()
        self.pending_relationships.clear();self.pending_sends.clear();self.owner_dirty=False
        self.visual_deliveries.clear()
        with self._preview_lock:self.preview=None
    def _visual_event_valid(self,event,name):
        from .visual.types import VisualEvent
        raw=event.raw_message
        if (not self.config.dry_run or not isinstance(raw,dict) or
                raw.get('self_state')!='incoming' or raw.get('group_identity_quality')!='manual_session_binding' or
                event.is_reply_to_me or event.reply_to_name or event.reply_to_text or
                self.visual_binding!=(raw.get('binding_id'),raw.get('capture_session_id'),raw.get('capture_epoch'))):
            return False
        try:
            observation=VisualEvent(
                observation_id=event.message_id,binding_id=raw.get('binding_id'),group_name=name,
                speaker_session_id=event.sender_id,sender_name=event.sender_name,text=event.text,
                observed_at=event.timestamp,capture_session_id=raw.get('capture_session_id'),
                capture_epoch=raw.get('capture_epoch'),sequence=event.source_sequence,
                confidence=raw.get('confidence'),source=raw.get('source'),
                identity_quality=event.identity_quality,transport=raw.get('transport'),
                id_source=raw.get('id_source'),timestamp_source=event.timestamp_source,
                mention_kind=raw.get('mention_kind'))
        except (TypeError,ValueError):return False
        return type(event.is_at_me) is bool and event.is_at_me==observation.is_at_me
    def receive_event(self,event,now,wall):
        """Validated ChatEvent boundary shared by wxauto and the visual runner."""
        if not self.valid(self.epoch):return False
        if not isinstance(event,ChatEvent) or event.platform!='wechat' or event.conversation_type!='group':
            self.counts['ignored']+=1;return False
        name=next((name for name in self.config.allowed_groups if group_id(name)==event.conversation_id),None)
        if name is None:self.counts['ignored']+=1;return False
        visual=self._is_visual(event)
        if visual and not self._visual_event_valid(event,name):
            self.counts['ignored']+=1;return False
        group=self.group(name)
        if not group.buffer.append(event,wall):self.counts['ignored']+=1;return False
        self.counts['received']+=1;self.log.emit('received',group=name,person=event.sender_id,message_id=event.message_id)
        self.log.text(event.sender_name,event.text)
        if not visual and self.job and self.job.event.conversation_id==event.conversation_id :
            previous=self.job.event;self.scheduler.cancel(self.job.ticket,'wechat_context_changed')
            if self.job.decision.strong and wall-previous.timestamp<30 and group.pending_event is None:
                group.pending_event=previous;group.pending_decision=self.job.decision;group.first_pending=now
        relationship=self.relationship(event.sender_id,event.identity_quality)
        aliases=tuple(r['content'].split('外号是 ',1)[1] for r in self.runtime.memory.snapshot
            if r.get('subject_id')=='ativia' and r.get('group_id')==event.conversation_id and r.get('visibility')=='group'
            and r.get('type')=='alias' and '外号是 ' in r['content'])[:2]
        relevant=any(r.get('group_id')==event.conversation_id and any(w in r['content'] for w in re.findall(r'[A-Za-z]{3,}|[\u4e00-\u9fff]{2}',event.text)) for r in self.runtime.memory.snapshot[:100])
        if visual:
            # Visual V1 accepts only the exact displayed @Ativia token validated
            # above. Aliases, quotes, followups and proactive policy never admit it.
            from .response_policy import ResponseDecision
            decision=ResponseDecision(event.is_at_me,'visual_explicit_at' if event.is_at_me else 'visual_no_explicit_at',event.is_at_me)
        else:
            decision=group.policy.decide(event,group.buffer,wall,familiarity=relationship.familiarity,relevant_memory=relevant,aliases=aliases)
        if self.runtime.mode in ('CLASS','FOCUS','SLEEP') and not decision.strong:
            decision=type(decision)(False,'quiet_mode',False)
        self.log.emit('policy',group=name,respond=decision.respond,reason=decision.reason,version=group.buffer.version)
        if visual:
            if decision.respond:
                if len(group.visual_pending)<16:
                    group.visual_pending.append((event,decision,now))
                else:
                    self.counts['ignored']+=1;self.log.emit('policy',respond=False,reason='visual_queue_full')
            else:self.counts['ignored']+=1
        elif decision.respond and (decision.strong or group.pending_event is None or not group.pending_decision.strong):
            if group.pending_event is None:group.first_pending=now
            group.pending_event=event;group.pending_decision=decision
        if group.pending_event and not visual:group.pending_at=now+self.config.debounce_seconds
        if self.config.memory_enabled and self.runtime.memory.enabled:
            candidate=self.evaluator.evaluate(event,group.buffer.snapshot(wall))
            if (candidate and self.require_stable_memory and event.identity_quality!='stable_id'
                    and candidate.subject_id not in (event.conversation_id,'ativia')):
                candidate=None
            if candidate:
                self.candidate_messages.append(event.message_id)
                self.candidates.add(candidate,now,self.epoch)
                if not self.require_stable_memory or event.identity_quality=='stable_id':
                    self.people[event.sender_id]=PersonIdentity(event.sender_id,event.sender_name,quality=event.identity_quality)
                self.log.emit('memory_candidate',person=event.sender_id,candidate=True,category=candidate.type,confidence=candidate.confidence)
        return True
    def relationship(self,identity,quality=None):
        if self.require_stable_memory and quality!='stable_id':return RelationshipState()
        if identity==OWNER_ID:return self.runtime.relationship
        if identity not in self.relationships:self.relationships[identity]=self.runtime.memory.person_relationship(identity)
        return self.relationships[identity]
    def start_reply(self,now,wall):
        if self.job is not None or self.pending_sends:return
        if not self.executor:return
        for name,group in self.groups.items():
            if group.pending_event is None and group.visual_pending:
                event,decision,queued_at=group.visual_pending.popleft()
                group.pending_event=event;group.pending_decision=decision
                group.first_pending=queued_at;group.pending_at=queued_at+self.config.debounce_seconds
            event=group.pending_event
            if event is None or (now<group.pending_at and now-group.first_pending<5):continue
            decision=group.pending_decision;group.pending_event=None;group.pending_decision=None
            if self._is_visual(event) and not 0<=wall-event.timestamp<=30:
                self.log.emit('policy',respond=False,reason='visual_event_expired');continue
            if not decision or not decision.respond:continue
            if not decision.strong and (self.runtime.mode in ('CLASS','FOCUS','SLEEP') or wall-group.policy.last_proactive<self.config.proactive_cooldown or group.policy.consecutive>=self.config.max_consecutive_messages):continue
            while self.calls and now-self.calls[0]>=60:self.calls.popleft()
            if len(self.calls)>=self.config.max_calls_per_minute or not group.policy.admit_call(wall):
                self.log.emit('policy',respond=False,reason='model_budget');continue
            if not self.valid(self.epoch):return
            ticket=self.scheduler.admit('wechat',timeout=self.config.reply_timeout_seconds)
            job=ReplyJob(event,group.buffer.version,self.epoch,self.runtime.memory.privacy_epoch,decision,ticket)
            ticket.cancel_callback=job.abort;self.scheduler.start(ticket);self.job=job
            if self._is_visual(event):self.status='visual_generating'
            self.calls.append(now);self.counts['model_calls']+=1;self.log.emit('model',model_called=True,request_id=ticket.request_id)
            def cancelled(j=job):
                return not self.valid(j.epoch) or not j.ticket.valid() or j.privacy_epoch!=self.runtime.memory.privacy_epoch
            events=group.buffer.snapshot(wall)
            # Uncertain people cannot authorize personal recall. Group-owned
            # plans and experiences still remain useful across visual sessions.
            scoped_visual=self.require_stable_memory and self._is_visual(event)
            recall_args=({'memory_subjects':tuple(e.sender_id for e in events if e.identity_quality=='stable_id')}
                         if scoped_visual else {})
            recall_allowed=self.config.memory_enabled and (scoped_visual or not self.require_stable_memory or
                (event.identity_quality=='stable_id' and all(e.identity_quality=='stable_id' for e in events)))
            job.future=self.executor.submit(self.core.handle,event,events,ticket=ticket,cancelled=cancelled,bind_cancel=job.bind,
                memory_enabled=recall_allowed,relationship=self.relationship(event.sender_id,event.identity_quality),**recall_args)
            break
    def finish_reply(self,now,wall):
        job=self.job
        if not job or not job.future or not job.future.done():return
        self.job=None
        try:text=job.future.result()
        except Exception as exc:
            self.log.emit('model_error',error_type=type(exc).__name__);self.scheduler.finish(job.ticket)
            if self._is_visual(job.event):self.status='model_unavailable'
            return
        self.log.emit('model_finished',ms=round((time.monotonic()-(job.ticket.started or job.ticket.created))*1000),request_id=job.ticket.request_id)
        name=next((n for n in self.config.allowed_groups if group_id(n)==job.event.conversation_id),None)
        group=self.groups.get(name)
        valid=(self.connected and self.valid(job.epoch) and job.ticket.valid() and group and
               (self._is_visual(job.event) or group.buffer.version==job.version)
               and self.runtime.memory.privacy_epoch==job.privacy_epoch)
        if self._is_visual(job.event):
            valid=valid and self._visual_event_valid(job.event,name) and 0<=wall-job.event.timestamp<=30
            if self.connected:self.status='visual_listening'
        if valid and text:
            if len(text)>16000:
                self.log.emit('model_discarded',reason='reply_exceeds_transport_budget',request_id=job.ticket.request_id)
                self.scheduler.finish(job.ticket);return
            # Last local version check precedes child-side sequence and exact-window checks.
            if self.config.dry_run:
                self.preview_reply(job,text)
                self.scheduler.finish(job.ticket);return
            self.pending_sends[job.ticket.request_id]=(now,job,text)
            try:self.adapter.send_message(name,text,job.ticket.request_id,group.buffer.source_sequence)
            except Exception:
                self.pending_sends.pop(job.ticket.request_id,None);self.log.emit('send',status='unknown',request_id=job.ticket.request_id)
        else:self.log.emit('model_discarded',reason='stale_or_cancelled',request_id=job.ticket.request_id)
        self.scheduler.finish(job.ticket)
    def preview_reply(self,job,text):
        # Local terminal sink: no send IPC, vendor call, clipboard or UI input.
        if not self.config.dry_run or not self.valid(job.epoch) or not job.ticket.valid():return
        from .sender import split_reply
        raw_trace=getattr(job.ticket,'wechat_trace',{})
        trace={key:raw_trace[key] for key in ('personality_called','memory_recalled','model')
            if isinstance(raw_trace,dict) and key in raw_trace}
        name=next((n for n in self.config.allowed_groups if group_id(n)==job.event.conversation_id),'')
        preview=dict(group=name,sender=job.event.sender_name,trigger=job.decision.reason,
            input=job.event.text,reply=text,chunks=split_reply(text),trace_id=job.ticket.request_id,
            memory_candidate_queued=job.event.message_id in self.candidate_messages,
            send_blocked='local_preview_sink',**trace)
        # Provenance for an optional downstream visual send sink. The existing
        # service remains a dry-run producer and never gains native input.
        raw=job.event.raw_message if isinstance(job.event.raw_message,dict) else {}
        preview.update(reply_id=job.ticket.request_id,source_event_id=job.event.message_id,
            source_track_id=job.event.message_id,source_binding_id=raw.get('binding_id',''),
            source_capture_epoch=raw.get('capture_epoch'),conversation_id=job.event.conversation_id,
            model_request_id=job.ticket.request_id,generated_at=time.time(),
            source_transport=raw.get('transport',''),source_self_state=raw.get('self_state',''))
        # PauseGate invokes subscribers after releasing its own lock, so this
        # lock has no inverted nesting with gate.allows(). A pause either
        # prevents publication or clears it before transition() returns.
        with self._preview_lock:
            if not self.config.dry_run or not self.valid(job.epoch) or not job.ticket.valid():return
            self.counts['dry_runs']+=1;self.preview=preview
            if self._is_visual(job.event):
                self.visual_deliveries[job.ticket.request_id]=(job,text)
                while len(self.visual_deliveries)>64:self.visual_deliveries.pop(next(iter(self.visual_deliveries)))
        self.log.emit('dry_run',group=name,person=job.event.sender_id,count=len(preview['chunks']),
            status='SEND_BLOCKED',request_id=job.ticket.request_id)
        self.log.text('Ativia dry-run',text)
    def visual_delivery(self,task,status,reason,wall):
        """Trusted downstream result, separate from the wxauto dry-run protocol.

        Only confirmed model output for this exact live binding enters spoken
        conversation context. Unknown/cancelled output stays only in send ledger.
        """
        pending=self.visual_deliveries.pop(task.reply_id,None)
        if pending is None:return False
        job,text=pending
        self.log.emit('visual_delivery',status=status,reason=reason,request_id=task.reply_id)
        if (status!='SEND_CONFIRMED' or task.source!='model' or text!=task.text or
                task.source_event_id!=job.event.message_id or task.conversation_id!=job.event.conversation_id or
                task.model_request_id!=job.ticket.request_id or not self.valid(job.epoch)):
            return False
        name=next((n for n in self.config.allowed_groups if group_id(n)==task.conversation_id),None)
        if (task.source_binding_id!=job.event.raw_message.get('binding_id') or
                not self._visual_event_valid(job.event,name)):
            return False
        group=self.groups.get(name)
        if group is None:return False
        self.counts['sent']+=1;group.buffer.sent(job.event,text,wall);group.policy.sent(job.decision,wall)
        if self.config.memory_enabled and self.runtime.memory.enabled and job.event.identity_quality=='stable_id':
            person=job.event.sender_id;relationship=self.relationship(person,'stable_id')
            with self.runtime.lock:relationship.engaged(job.event.text)
            self.people[person]=PersonIdentity(person,job.event.sender_name,quality='stable_id')
            self.pending_relationships[person]=asdict(relationship)
        return True
    def sent_result(self,packet,wall):
        identity=packet.get('request_id');pending=self.pending_sends.pop(identity,None)
        if pending is None:return
        _,job,text=pending;status=packet.get('status','unknown')
        if self.config.dry_run and status=='sent':
            self.log.emit('error',error_type='DryRunProtocolViolation');return
        self.log.emit('send',status=status,sent=status=='sent',request_id=identity)
        if status=='dry_run' and self.config.dry_run and self.valid(job.epoch):
            self.preview_reply(job,text)
            return
        if status!='sent' or not self.valid(job.epoch):return
        name=next((n for n in self.config.allowed_groups if group_id(n)==job.event.conversation_id),None);group=self.groups.get(name)
        if group is None:return
        self.counts['sent']+=1;group.buffer.sent(job.event,text,wall);group.policy.sent(job.decision,wall)
        person=job.event.sender_id;relationship=self.relationship(person,job.event.identity_quality)
        if self.config.memory_enabled and self.runtime.memory.enabled:
            with self.runtime.lock:relationship.engaged(job.event.text)
            self.people[person]=PersonIdentity(person,job.event.sender_name,quality=job.event.identity_quality)
            self.pending_relationships[person]=asdict(relationship)
            if person==OWNER_ID:self.owner_dirty=True
    def consolidate(self,now):
        self.db_admitting=True
        try:self._consolidate(now)
        finally:self.db_admitting=False
    def _consolidate(self,now):
        candidates=self.candidates.take_due(now,self.epoch)
        if not candidates and not self.pending_relationships:return
        epoch=self.epoch
        binding=self.visual_binding
        guard=lambda:self.valid(epoch) and self.config.memory_enabled and (binding is None or self.visual_binding==binding)
        future=self.runtime.memory.consolidate(candidates,tuple(self.people.values()),tuple(self.pending_relationships.items()),guard=guard)
        self.people.clear();self.pending_relationships.clear()
        if future:self.pending_db.append(future)
        if self.owner_dirty:
            self.owner_dirty=False
            future=self.runtime.memory.submit(lambda:self.runtime.save() if guard() else None)
            if future:self.pending_db.append(future)
    def finish_db(self):
        for future in tuple(self.pending_db):
            if not future.done():continue
            self.pending_db.remove(future)
            try:result=future.result()
            except Exception:result=None
            if isinstance(result,int):self.counts['memory_written']+=result;self.log.emit('memory_written',memory_written=result)
    def quiescent(self):
        return not self.starting and not self.db_admitting and not (self.adapter and self.adapter.alive()) and all(f.done() for f in tuple(self.pending_db))
    def close(self):
        if self.closed:return
        self.closed=True;self._unsubscribe();self.stop_event.set();self.wake.set()
        if self.job:self.scheduler.cancel(self.job.ticket,'wechat_closed')
        if self.adapter:self.adapter.stop()
        if not self.thread:self.log.close()
    def closed_finished(self):
        return (not self.thread or not self.thread.is_alive()) and self.quiescent() and (not self.job or not self.job.future or self.job.future.done())
