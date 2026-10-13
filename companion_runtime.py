"""Per-instance companion policy; existing API/voice/subtitle settings are untouched."""
from dataclasses import asdict
import json,os
from pathlib import Path
from threading import RLock
from personality.engine import BehaviorContext,BehaviorPlanner,RelationshipState,MODES

def data_directory():
    return Path(os.environ.get('ATIVIA_DATA_DIR') or str(Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Ativia'))

class CompanionRuntime:
    def __init__(self,path=None):
        self.path=Path(path) if path else data_directory()/'companion.json'
        self.lock=RLock();self.mode='NORMAL';self.manual_mode=True
        self.relationship=RelationshipState();self.planner=BehaviorPlanner()
        self.last_plan=None;self.application='';self.closed=False
        try:
            row=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(row,dict):raise ValueError('invalid_companion_settings')
            if row.get('mode') in MODES:self.mode=row['mode']
            self.manual_mode=bool(row.get('manual_mode',True))
            for key,value in row.get('relationship',{}).items():
                if key=='conversation_frequency' and type(value) is int:
                    setattr(self.relationship,key,max(0,min(1000000,value)))
                elif key in asdict(self.relationship) and type(value) in (int,float) and 0<=value<=1:
                    setattr(self.relationship,key,value)
        except (OSError,ValueError,TypeError,AttributeError):pass
        from memory.service import MemoryService
        self.memory=MemoryService(self.path.with_name('memory.sqlite3'))
        try:
            options=row.get('memory',{})
            self.memory.enabled=options.get('enabled',True) is True
            self.memory.recall_enabled=options.get('recall_enabled',True) is True
            self.memory.budget=max(200,min(4000,int(options.get('budget',1200))))
        except (UnboundLocalError,AttributeError,ValueError,TypeError):pass
        self.last_recall=None

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        row=dict(schema_version=4,mode=self.mode,manual_mode=self.manual_mode,relationship=asdict(self.relationship),
                 memory=dict(enabled=self.memory.enabled,recall_enabled=self.memory.recall_enabled,budget=self.memory.budget))
        from uuid import uuid4
        temp=self.path.with_name(self.path.name+'.'+uuid4().hex+'.tmp')
        try:temp.write_text(json.dumps(row,ensure_ascii=False),encoding='utf-8');temp.replace(self.path)
        finally:temp.unlink(missing_ok=True)

    def set_mode(self,mode,*,manual=True,persist=True):
        if mode not in MODES:return
        if not manual and self.manual_mode:return
        with self.lock:self.mode=mode;self.manual_mode=manual
        if persist:
            try:self.save()
            except OSError:pass

    def context(self,text='',*,legacy_mode=None,event='none',paused=False,focused=False,confident=True,recent_reactions=0):
        mode='CLASS' if legacy_mode=='learning' else self.mode
        r=self.relationship
        plan=self.planner.plan(BehaviorContext(text,mode,self.application,event,paused,focused,confident,
            r.initiative_tolerance,r.familiarity,r.playfulness_tolerance,recent_reactions))
        self.last_plan=plan
        self.last_recall=self.memory.recall(text or self.application or event,mode,self.application) if text or event!='none' else None
        return dict(mode=mode,decision=asdict(plan),relationship=asdict(r),
                    memory=self.last_recall.text if self.last_recall else '')

    def refresh_environment(self):
        try:
            from screen_events import foreground_application
            meta=foreground_application()
            if not meta.get('owned') and meta.get('application'):self.application=meta['application']
        except Exception:pass

    def remember_input(self,text):
        self.memory.consider(text,application=self.application)

    def memory_options(self,enabled,recall,budget):
        self.memory.enabled=bool(enabled);self.memory.recall_enabled=bool(recall)
        self.memory.budget=max(200,min(4000,int(budget)))
        self.memory.submit(self.save)

    def record(self,text,reply=''):
        self.relationship.engaged(text)
        self.memory.submit(self.save)

    def close(self):
        if self.closed:return
        self.closed=True
        self.flush_future=self.memory.submit(self.save)
        self.memory.close()
