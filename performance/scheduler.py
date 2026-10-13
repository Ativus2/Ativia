"""Admission/cancellation shared by isolated workers, never a shared I/O loop."""
from dataclasses import dataclass,field
from collections import deque
from threading import RLock,Event
from time import monotonic
from uuid import uuid4

PRIORITY={'voice':0,'text':1,'asr':2,'translation':3,'tts':4,'wechat':5,'vision':5,'proactive':6,'memory':7,'summary':8}

@dataclass
class Ticket:
    kind:str
    generation:int
    created:float
    deadline:float
    request_id:str=field(default_factory=lambda:uuid4().hex)
    parent_id:str=''
    state:str='queued'
    started:float|None=None
    reason:str=''
    cancelled:Event=field(default_factory=Event)
    cancel_callback:object=None
    @property
    def priority(self):return PRIORITY[self.kind]
    def valid(self):return not self.cancelled.is_set() and self.state in ('queued','active') and monotonic()<self.deadline

class TaskScheduler:
    def __init__(self):
        self.lock=RLock();self.generation=0;self.tasks={};self.history=deque(maxlen=128)
    def admit(self,kind,*,timeout=45,request_id=None,parent_id='',cancel=None,replace=False):
        if replace:
            self.cancel_kinds(('voice','text','vision','proactive','tts','wechat'),reason='new_user_input')
        with self.lock:
            self.generation+=1;now=monotonic()
            ticket=Ticket(kind,self.generation,now,now+timeout,parent_id=parent_id,cancel_callback=cancel)
            if request_id:ticket.request_id=request_id
            self.tasks[ticket.request_id]=ticket
        return ticket
    def start(self,ticket):
        if not ticket.valid():return False
        with self.lock:ticket.state='active';ticket.started=monotonic()
        return True
    def finish(self,ticket):
        if ticket is None:return
        with self.lock:
            if self.tasks.pop(ticket.request_id,None) is not None:
                ticket.state='done' if not ticket.cancelled.is_set() else 'cancelled'
                self.history.append(self._row(ticket))
    def cancel(self,ticket,reason='cancelled'):
        if ticket is None:return
        with self.lock:
            if ticket.request_id not in self.tasks or ticket.cancelled.is_set():return
            ticket.cancelled.set();ticket.reason=reason;ticket.state='cancelled';callback=ticket.cancel_callback
        if callback:
            try:callback()
            except Exception:pass
        self.finish(ticket)
    def cancel_kinds(self,kinds,reason='cancelled'):
        with self.lock:rows=[t for t in self.tasks.values() if t.kind in kinds]
        for t in rows:self.cancel(t,reason)
    def expire(self):
        with self.lock:rows=[t for t in self.tasks.values() if monotonic()>=t.deadline]
        for t in rows:self.cancel(t,'timeout')
    def _row(self,t):
        return dict(request_id=t.request_id,parent_id=t.parent_id,kind=t.kind,priority=t.priority,
            generation=t.generation,state=t.state,reason=t.reason,queue_ms=round(((t.started or monotonic())-t.created)*1000,2))
    def snapshot(self):
        with self.lock:
            return dict(active=[self._row(t) for t in sorted(self.tasks.values(),key=lambda t:t.priority) if t.state=='active'],
                queue=[self._row(t) for t in sorted(self.tasks.values(),key=lambda t:t.priority) if t.state=='queued'],
                cancelled=[r for r in self.history if r['state']=='cancelled'][-12:])
