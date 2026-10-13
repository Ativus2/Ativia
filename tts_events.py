"""Always-on speech lifecycle audit. Never log reply text, prompts or API keys."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
from time import monotonic
from uuid import uuid4

_lock = threading.Lock()
_logger = None
_listeners = set()
LOG_PATH = Path(os.getenv("ATIVIA_TTS_EVENT_LOG", str(Path(__file__).parent / "local_tts/logs/tts-events.jsonl")))


def new_reply_id():
    return uuid4().hex


def subscribe(callback):
    with _lock:_listeners.add(callback)
    def unsubscribe():
        with _lock:_listeners.discard(callback)
    return unsubscribe


_pending=None
_writer=None
def _safe_details(details):
    import re,math
    strings={'source','reason','stage','engine','model','actual_model','backend','target','error_type',
             'host','route','voice','voice_name','status','request','parent_id','response_id'}
    def numeric(value):
        if type(value) in (int,float) and math.isfinite(value):return value
        if isinstance(value,dict):return {k:numeric(v) for k,v in value.items() if type(v) in (int,float) and math.isfinite(v)}
        return None
    safe={}
    for key,value in details.items():
        if re.search(r'key|authorization|header|audio_data|prompt|text|token_string',key,re.I):continue
        if type(value) in (int,float,bool) or value is None:
            safe[key]=value if value is None or type(value) is bool else numeric(value)
        elif key=='usage' and isinstance(value,dict):safe[key]=numeric(value)
        elif key in strings and isinstance(value,str):
            safe[key]=value if re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}',value) and not re.search(r'sk-|Bearer|Authorization',value,re.I) else 'details_omitted'
    return safe

def _write_events():
    global _logger
    try:
        LOG_PATH.parent.mkdir(parents=True,exist_ok=True)
        _logger=logging.Logger('ativia.tts.audit',level=logging.INFO)
        handler=RotatingFileHandler(LOG_PATH,maxBytes=5*1024*1024,backupCount=4,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'));_logger.addHandler(handler)
    except OSError:_logger=None
    while True:
        row=_pending.get()
        try:
            if _logger:_logger.info(json.dumps(row,ensure_ascii=False))
        finally:_pending.task_done()

def event(reply_id,name,**details):
    global _pending,_writer
    import queue
    row=dict(at=datetime.now(timezone.utc).isoformat(),monotonic=monotonic(),
             pid=os.getpid(),reply_id=reply_id,event=name,**_safe_details(details))
    with _lock:
        if _pending is None:
            _pending=queue.Queue(maxsize=1024)
            _writer=threading.Thread(target=_write_events,name='ativia-audit-log',daemon=True);_writer.start()
        listeners=tuple(_listeners)
    try:_pending.put_nowait(row)
    except queue.Full:pass
    for callback in listeners:
        try:callback(row)
        except Exception:pass
    return row


class TTSCancelled(RuntimeError):
    pass


class TTSFailure(RuntimeError):
    pass


class GPUProtected(TTSFailure):
    """Resource admission changed after the client decision; cloud is mandatory."""
    pass
