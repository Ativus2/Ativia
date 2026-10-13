"""Explicit metadata extraction. No message methods, history scrolling or guessed wxids."""
from datetime import datetime
import json,math,time

FIELDS=('id','attr','type','sender','content','sender_id','wxid','timestamp','time','quote_nickname','quote_content')


def identity(value):
    if isinstance(value,bool):return ''
    if isinstance(value,(str,int)):return str(value)
    if isinstance(value,(tuple,list)) and value and all(type(x) is int for x in value):
        return json.dumps(value,separators=(',',':'))
    return ''


def serialize_message(msg):
    values={};types={}
    for key in FIELDS:
        try:value=getattr(msg,key,None)
        except Exception:continue
        types[key]=type(value).__name__
        if isinstance(value,(str,int,float,bool,type(None),datetime)):
            values[key]=value
        elif key=='id':values[key]=identity(value)
    row={k:values.get(k) or '' for k in ('attr','type','sender','content','quote_nickname','quote_content')}
    row['id']=identity(values.get('id'))
    row['sender_id']=values.get('sender_id') or values.get('wxid') or ''
    stamp=values.get('timestamp')
    if isinstance(stamp,datetime):stamp=stamp.timestamp()
    if type(stamp) in (int,float) and math.isfinite(stamp) and 946684800<=stamp<=time.time()+86400:
        row.update(timestamp=stamp,timestamp_source='source')
    else:
        row.update(timestamp=time.time(),timestamp_source='received')
    row['field_types']=types
    row['source_timestamp_available']=row['timestamp_source']=='source'
    return row


class VisibleBaseline:
    def __init__(self,limit=4096):self.seen=set();self.ready=False;self.limit=limit
    def establish(self,messages):
        self.seen={identity(getattr(m,'id',None)) for m in messages};self.seen.discard('')
        self.ready=True
    def accept(self,row):
        mid=identity(row.get('id'))
        if not self.ready or not mid or mid in self.seen:return False
        if len(self.seen)>=self.limit:raise RuntimeError('MessageCacheLimit')
        self.seen.add(mid);return True
