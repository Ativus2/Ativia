"""Bounded numeric telemetry. Text, image/audio payloads and headers are rejected."""
from collections import deque
from threading import Lock
from time import monotonic
import math,re
from pathlib import Path
import json,os,queue,threading
SAFE=re.compile(r'^[A-Za-z0-9_.:/-]{1,120}$')
class Metrics:
    def __init__(self):
        self.lock=Lock();self.events=deque(maxlen=512);self.latest={};self.prompt={};self.first_audio=deque(maxlen=256)
        self.queue=queue.Queue(maxsize=1024);self.writer=None;self.dropped=0
    def mark(self,request_id,stage,**values):
        if not SAFE.fullmatch(str(request_id)) or not SAFE.fullmatch(stage):return
        numeric={k:round(float(v),3) for k,v in values.items() if SAFE.fullmatch(k) and type(v) in (int,float) and math.isfinite(v)}
        row=dict(request_id=request_id,stage=stage,at=monotonic(),**numeric)
        with self.lock:
            if stage=='tts_first_audio':
                if request_id in self.first_audio:return
                self.first_audio.append(request_id)
            self.events.append(row);self.latest[stage]=row
        # Disk writing is lazy, bounded, and off the producer thread.
        if self.writer is None:
            with self.lock:
                if self.writer is None:
                    self.writer=threading.Thread(target=self._write,name='ativia-metrics-log',daemon=True);self.writer.start()
        try:self.queue.put_nowait(row)
        except queue.Full:self.dropped+=1
    def _write(self):
        import logging
        from logging.handlers import RotatingFileHandler
        path=Path(os.environ.get('ATIVIA_PERF_LOG',str(Path(__file__).resolve().parents[1]/'logs/performance.jsonl')))
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            handler=RotatingFileHandler(path,maxBytes=2*1024*1024,backupCount=2,encoding='utf-8')
            logger=logging.Logger('ativia-performance');logger.addHandler(handler);logger.setLevel(logging.INFO)
        except OSError:logger=None
        while True:
            row=self.queue.get()
            try:
                if logger:logger.info(json.dumps(row,separators=(',',':')))
            finally:self.queue.task_done()
    def snapshot(self):
        with self.lock:return dict(latest=dict(self.latest),prompt=dict(self.prompt),dropped=self.dropped)

metrics=Metrics()
def prompt_budget(system,recent='',memory='',screen='',user=''):
    # Conservative UTF-8 upper bounds, explicitly not exact tokenizer counts.
    bound=lambda text:len(str(text).encode('utf-8'))
    row=dict(system=bound(system),recent=bound(recent),memory=bound(memory),screen=bound(screen),
             total=bound(system)+bound(user))
    with metrics.lock:metrics.prompt=row
    return row
