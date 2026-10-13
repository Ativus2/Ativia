"""One bounded background executor for DB work; foreground recall uses only RAM."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore,Event,Lock
from .gate import MemoryGate,domain
import time
from .retrieval import retrieve

class MemoryService:
    def __init__(self,path):
        self.path=path;self.gate=MemoryGate();self.snapshot=();self.working=deque(maxlen=6);self.session=deque(maxlen=8)
        self.enabled=True;self.recall_enabled=True;self.budget=1200;self.error=None;self.closed=False;self.paused=False;self.privacy_epoch=0
        self.ready=Event();self.slots=BoundedSemaphore(64);self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ativia-memory')
        self.store=None;self.people_snapshot=();self.relationship_snapshot={};self.scheduler=None;self.submit(self._open)
    def _open(self):
        try:
            from .store import MemoryStore
            self.store=MemoryStore(self.path);self.refresh()
        finally:self.ready.set()
    def refresh(self):
        self.snapshot=tuple(self.store.rows());self.people_snapshot=tuple(self.store.people());self.relationship_snapshot=self.store.relationships();self.error=None
        return self.snapshot
    def submit(self,operation,*args):
        if self.closed or not self.slots.acquire(blocking=False):return None
        ticket=self.scheduler.admit('memory',timeout=30) if self.scheduler else None
        def work():
            if ticket:self.scheduler.start(ticket)
            try:return operation(*args)
            except Exception as exc:self.error=type(exc).__name__;return None
            finally:
                self.slots.release()
                if ticket:self.scheduler.finish(ticket)
        try:return self.executor.submit(work)
        except RuntimeError:self.slots.release();return None
    def consider(self,text,*,application=''):
        if self.closed or self.paused:return
        self.working.append(text[:300])
        candidate=self.gate.judge(text,application=application)
        if candidate.classification=='SESSION':self.session.append((time.time(),candidate.content));return
        if candidate.classification=='IGNORE' or not self.enabled:return
        def write():
            if self.paused or not self.enabled or self.store is None:return
            self.store.save(candidate);self.refresh()
        self.submit(write)
    def recall(self,query,mode,application='',*,scope=None,guard=None):
        if not self.recall_enabled or self.paused:return retrieve((),query,budget=0)
        session_rows=tuple(dict(id='session-'+str(at),content=text,layer='episodic',classification='SESSION',
            created_at=at,updated_at=at,expires_at=at+3600,pinned=0,mode=domain(text),application='',
            fact_key='',importance=.4,confidence=.95,source='explicit_user') for at,text in tuple(self.session))
        rows=self.snapshot
        if scope is not None and scope.channel=='group':
            # Group conversation runs on its own model worker. Read a scoped SQL
            # snapshot there, before LIMIT, without blocking the Qt foreground.
            rows=tuple(self.store.rows(scope=scope)) if self.store else ()
        result=retrieve(rows+session_rows,query,mode,application,self.budget,scope=scope)
        from performance.metrics import metrics
        metrics.mark('memory','memory_retrieval',ms=result.elapsed_ms)
        if result.ids and self.store:
            def used():
                if self.paused or (guard and not guard()):return
                self.store.used(tuple(i for i in result.ids if not i.startswith('session-')));self.refresh()
            self.submit(used)
        return result
    def person_relationship(self,person_id):
        from personality.engine import RelationshipState
        from dataclasses import fields
        row=self.relationship_snapshot.get(person_id,{})
        state=RelationshipState()
        if not isinstance(row,dict):return state
        for field in fields(RelationshipState):
            value=row.get(field.name)
            if field.name=='conversation_frequency' and type(value) is int:setattr(state,field.name,max(0,min(1000000,value)))
            elif field.name!='conversation_frequency' and type(value) in (int,float) and 0<=value<=1:setattr(state,field.name,value)
        return state

    def consolidate(self,candidates,people=(),relationships=(),guard=None):
        def permitted():return not self.paused and self.enabled and (guard is None or guard())
        def write():
            if not permitted() or self.store is None:return 0
            saved=0
            for identity in people:
                if not permitted():break
                self.store.remember_person(identity,guard=permitted)
            for candidate in candidates:
                if not permitted():break
                if self.store.save(candidate,guard=permitted):saved+=1
            for person_id,state in relationships:
                if not permitted():break
                self.store.save_relationship(person_id,state,guard=permitted)
            self.refresh();return saved
        return self.submit(write)

    def change(self,action,*args):
        if action in ('edit','delete','set_visibility'):self.privacy_epoch+=1
        def update():
            if self.store is None:return
            getattr(self.store,action)(*args);return self.refresh()
        return self.submit(update)
    def close(self):
        if self.closed:return
        self.closed=True;self.close_future=self.executor.submit(lambda:None)
        self.working.clear();self.session.clear()
        self.executor.shutdown(wait=False,cancel_futures=False)
