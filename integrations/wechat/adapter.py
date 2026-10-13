"""A small subprocess boundary isolates wxauto/native UI hangs from Qt and ASR."""
from typing import Protocol
from collections import deque
from pathlib import Path
import json,os,queue,subprocess,sys,threading,time

class ChatPlatformAdapter(Protocol):
    def start(self):...
    def receive_event(self):...
    def send_message(self,group,text,request_id,sequence):...
    def stop(self):...

class WechatAdapter:
    def __init__(self,config):
        self.config=config;self.process=None;self.reader=None;self.events=queue.Queue(maxsize=256)
        self.write_lock=threading.Lock();self.last_seen=time.monotonic();self.started=0
    def start(self):
        if self.process and self.process.poll() is None:return
        self.events=queue.Queue(maxsize=256);self.started=self.last_seen=time.monotonic()
        env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('API_KEY','AUTHORIZATION','ACCESS_TOKEN','SECRET'))}
        env['PYTHONIOENCODING']='utf-8'
        command=[sys.executable,'-X','utf8','-m','integrations.wechat.bridge']
        self.process=subprocess.Popen(command,cwd=str(Path(__file__).resolve().parents[2]),env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
            bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        process=self.process;event_queue=self.events
        self.reader=threading.Thread(target=self._read,args=(process,event_queue),name='ativia-wechat-ipc',daemon=True);self.reader.start()
        self._write(dict(action='start',config=self.config.document()))
    def _read(self,process,event_queue):
        try:
            for line in process.stdout:
                if len(line)>64000:continue
                try:row=json.loads(line)
                except ValueError:continue
                if not isinstance(row,dict):continue
                self.last_seen=time.monotonic()
                try:event_queue.put_nowait(row)
                except queue.Full:
                    # Losing an incoming context version must stop sending, not continue with stale context.
                    process.terminate();break
        except Exception:pass
        finally:
            try:event_queue.put_nowait({'stage':'disconnected'})
            except queue.Full:pass
    def _write(self,row):
        if self.config.dry_run and row.get('action')=='send':raise RuntimeError('DRY_RUN_SEND_IPC_BLOCKED')
        with self.write_lock:
            if not self.process or self.process.poll() is not None:raise RuntimeError('wechat_disconnected')
            self.process.stdin.write(json.dumps(row,ensure_ascii=False)+'\n');self.process.stdin.flush()
    def receive_event(self):
        rows=[]
        for _ in range(256):
            try:rows.append(self.events.get_nowait())
            except queue.Empty:break
        return rows
    def send_message(self,group,text,request_id,sequence):
        if self.config.dry_run:raise RuntimeError('DRY_RUN_SEND_IPC_BLOCKED')
        self._write(dict(action='send',group=group,text=text,request_id=request_id,sequence=sequence))
    def alive(self):return bool(self.process and self.process.poll() is None)
    def stop(self):
        process=self.process
        if process and process.poll() is None:
            try:process.terminate()
            except OSError:pass
    def close(self):
        self.stop()
        if self.process:
            try:self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
            for stream in (self.process.stdin,self.process.stdout):
                try:stream.close()
                except (OSError,AttributeError):pass
        if self.reader and self.reader is not threading.current_thread():self.reader.join(timeout=1)
