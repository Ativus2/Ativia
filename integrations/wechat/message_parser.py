"""Normalize only explicitly supported metadata; never call arbitrary message methods."""
import re,time,hashlib,math
from chat_events import ChatEvent
from .identity import IdentityResolver,group_id

def mentioned(text,names,at_only=False):
    for name in names:
        pattern=('@' if at_only else r'(?<![A-Za-z0-9_])@?')+re.escape(name)+r'(?![A-Za-z0-9_])'
        if re.search(pattern,text,re.I):return True
    return False

def normalize(raw,group,config,*,now=None):
    if group not in config.allowed_groups or not isinstance(raw,dict):return None
    if raw.get('historical') is True:return None
    if raw.get('attr')!='friend' or raw.get('type') not in ('text','quote'):return None
    if raw.get('chat_type','group')!='group':return None
    name=raw.get('sender');text=raw.get('content')
    if not isinstance(name,str) or not isinstance(text,str):return None
    name=name.strip();text=text.strip()
    if not name or not text or len(text)>4000:return None
    stable=raw.get('sender_id','')
    stable=stable if isinstance(stable,str) and len(stable)<=160 else ''
    person=IdentityResolver(config).resolve(group_id(group),name,stable)
    quote_name=raw.get('quote_nickname','');quote=raw.get('quote_content','')
    quote_name=quote_name if isinstance(quote_name,str) else '';quote=quote if isinstance(quote,str) else ''
    # Some builds append quote content to content; exclude it from the speaker's assertion.
    if raw.get('type')=='quote' and quote and text.endswith('\n'+quote):text=text[:-len(quote)-1].strip()
    if not text:return None
    identity=raw.get('id')
    if isinstance(identity,bool) or not isinstance(identity,(str,int)) or identity=='':return None  # No guessed IDs.
    stamp=raw.get('timestamp');basis=raw.get('timestamp_source','source')
    if not isinstance(stamp,(int,float)) or isinstance(stamp,bool) or not math.isfinite(stamp):stamp=time.time() if now is None else now;basis='received'
    mid=hashlib.sha256((group+'\0'+str(identity)).encode()).hexdigest()
    sequence=raw.get('sequence',0)
    if not isinstance(sequence,int) or isinstance(sequence,bool) or sequence<0:return None
    return ChatEvent('wechat','group',group_id(group),person.person_id,name,text,float(stamp),mid,
        mentioned(text,config.bot_names,True),raw.get('type')=='quote' and quote_name in config.bot_names,
        quote_name[:100],quote[:600],person.quality,sequence,basis,
        {'type':raw['type'],'attr':raw['attr']})
