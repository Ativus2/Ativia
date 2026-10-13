"""Validated JSON sidecar; never modifies API, model, voice or subtitle settings."""
from dataclasses import dataclass,asdict,field
from pathlib import Path
import json,os,math
from uuid import uuid4

@dataclass(frozen=True)
class WechatConfig:
    enabled:bool=False
    dry_run:bool=True
    mode:str='normal'
    allowed_groups:tuple=()
    bot_names:tuple=('Ativia',)
    owner_names:tuple=()
    owner_ids:tuple=()
    account_name:str=''
    proactive_enabled:bool=False
    proactive_cooldown:float=120
    proactive_probability:float=.12
    max_consecutive_messages:int=2
    memory_enabled:bool=False
    debug:bool=False
    debounce_seconds:float=1.0
    poll_seconds:float=1.0
    context_messages:int=40
    context_seconds:int=900
    max_calls_per_minute:int=6
    candidate_delay_seconds:float=20
    reply_timeout_seconds:float=40

    @classmethod
    def parse(cls,raw):
        if not isinstance(raw,dict):raise ValueError('invalid_wechat_config')
        w=raw.get('wechat',raw)
        if not isinstance(w,dict):raise ValueError('invalid_wechat_config')
        proactive=w.get('proactive_reply',{});memory=w.get('memory',{})
        if not isinstance(proactive,dict) or not isinstance(memory,dict):raise ValueError('invalid_wechat_config')
        def flag(value):
            if not isinstance(value,bool):raise ValueError('expected_boolean')
            return value
        def names(value,limit=8):
            if not isinstance(value,(list,tuple)) or len(value)>limit:raise ValueError('invalid_name_list')
            if any(not isinstance(x,str) or not x.strip() or len(x)>100 or any(c in x for c in '\r\n\0') for x in value):raise ValueError('invalid_name')
            return tuple(dict.fromkeys(x.strip() for x in value))
        def number(name,default,low,high,source=w):
            value=source.get(name,default)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError('invalid_'+name)
            return value
        c=cls(enabled=flag(w.get('enabled',False)),dry_run=flag(w.get('dry_run',True)),mode=w.get('mode','normal'),
            allowed_groups=names(w.get('allowed_groups',[]),5),bot_names=names(w.get('bot_names',['Ativia'])),
            owner_names=names(w.get('owner_names',[])),owner_ids=names(w.get('owner_ids',[])),
            account_name=w.get('account_name',''),proactive_enabled=flag(proactive.get('enabled',False)),
            proactive_cooldown=number('min_cooldown_seconds',120,120,86400,proactive),
            proactive_probability=number('probability',.12,0,.3,proactive),
            max_consecutive_messages=int(number('max_consecutive_messages',2,1,2,proactive)),
            memory_enabled=flag(memory.get('enabled',False)),debug=flag(w.get('debug',False)),
            debounce_seconds=number('debounce_seconds',1,.8,3),poll_seconds=number('poll_seconds',1,1,10),
            context_messages=int(number('context_messages',40,20,50)),context_seconds=int(number('context_seconds',900,60,1200)),
            max_calls_per_minute=int(number('max_calls_per_minute',6,1,12)),
            candidate_delay_seconds=number('candidate_delay_seconds',20,5,300),reply_timeout_seconds=number('reply_timeout_seconds',40,5,60))
        if c.mode!='normal' or not isinstance(c.account_name,str) or len(c.account_name)>100:raise ValueError('invalid_wechat_mode_or_account')
        if not c.bot_names:raise ValueError('bot_name_required')
        if c.enabled and (not c.allowed_groups or not (c.owner_names or c.owner_ids)):raise ValueError('whitelist_and_owner_required')
        return c

    def document(self):
        row=asdict(self)
        row['proactive_reply']=dict(enabled=row.pop('proactive_enabled'),min_cooldown_seconds=row.pop('proactive_cooldown'),
            probability=row.pop('proactive_probability'),max_consecutive_messages=row.pop('max_consecutive_messages'))
        row['memory']=dict(enabled=row.pop('memory_enabled'))
        return {'wechat':row}

class WechatConfigStore:
    def __init__(self,path=None):
        from companion_runtime import data_directory
        self.path=Path(path) if path else data_directory()/'wechat.json'
    def load(self):
        if not self.path.exists():return WechatConfig()
        return WechatConfig.parse(json.loads(self.path.read_text(encoding='utf-8-sig')))
    def save(self,config):
        WechatConfig.parse(config.document())
        self.path.parent.mkdir(parents=True,exist_ok=True);temp=self.path.with_name(uuid4().hex+'.tmp')
        try:temp.write_text(json.dumps(config.document(),ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(self.path)
        finally:temp.unlink(missing_ok=True)
