"""Bounded, rotating, content-free production diagnostics; text only with explicit debug."""
from collections import deque
import hashlib,json,logging,queue,threading,time,re
from logging.handlers import RotatingFileHandler
from pathlib import Path

class WechatLog:
    def __init__(self,directory,debug=False,enabled=True):
        self.directory=Path(directory);self.debug=debug;self.rows=deque(maxlen=100)
        self.queue=queue.Queue(maxsize=512);self.closed=False;self.dropped=0
        self.thread=threading.Thread(target=self._write,name='ativia-wechat-log',daemon=True);self.thread.start() if enabled else None
    def emit(self,stage,**fields):
        row={'stage':stage,'timestamp':time.time()}
        for key,value in fields.items():
            if key in ('group','person','message_id'):row[key+'_hash']=hashlib.sha256(str(value).encode()).hexdigest()[:16]
            elif key in ('reason','error_type','status','request_id','category','identity_quality') and isinstance(value,str):
                row[key]=value if re.fullmatch(r'[A-Za-z0-9_:. -]{1,100}',value) else 'details_omitted'
            elif isinstance(value,(int,float,bool)) and key in ('count','ms','confidence','version','respond','sent','candidate','model_called','memory_written'):row[key]=value
        self.rows.append(row)
        try:self.queue.put_nowait(row)
        except queue.Full:self.dropped+=1
    def text(self,speaker,text):
        if not self.debug:return
        from memory.gate import sensitive
        if sensitive(text):text='[sensitive content omitted]'
        try:self.queue.put_nowait(dict(stage='debug_message',speaker=str(speaker)[:100],text=text[:500],timestamp=time.time()))
        except queue.Full:self.dropped+=1
    def _write(self):
        handler=None
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            handler=RotatingFileHandler(self.directory/'events.jsonl',maxBytes=1024*1024,backupCount=2,encoding='utf-8')
            while True:
                row=self.queue.get()
                if row is None:break
                handler.emit(logging.LogRecord('wechat',logging.INFO,'',0,json.dumps(row,ensure_ascii=False),(),None))
        except Exception:pass
        finally:
            if handler:handler.close()
    def close(self):
        if self.closed:return
        self.closed=True
        try:self.queue.put_nowait(None)
        except queue.Full:
            try:self.queue.get_nowait();self.queue.put_nowait(None)
            except queue.Empty:pass
