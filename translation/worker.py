"""Qt boundary; recognition, audio, stabilizing and translation run off the UI thread."""
import asyncio
import threading
from PySide6.QtCore import QObject, Signal
from .pipeline import TranslationProvider
from .asr import make_provider
from .asr.base import ASRError
from .session import PipelineSession
from .windows_source import SourceError

class WindowsTranslationWorker(QObject):
    # Historical name retained; Windows, OpenAI and Deepgram share this worker.
    caption_changed=Signal(int,object)
    diagnostic=Signal(int,object)
    stream_reset=Signal(int,int)
    status=Signal(int,str)
    finished=Signal(int)
    def __init__(self,generation,settings,parent=None):
        super().__init__(parent)
        self.generation,self.settings=generation,settings
        self.thread=None; self.loop=None; self.task=None; self.source=None
        self.stop_event=threading.Event(); self.metrics={}
    def start(self):
        self.thread=threading.Thread(target=self._run,name='ativia-asr',daemon=True)
        self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.loop and self.task:
            try:self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:pass
    def command(self,command):
        if self.loop and self.source:
            try:self.loop.call_soon_threadsafe(self.source.command,command)
            except RuntimeError:pass
    def _status(self,message):
        if not self.stop_event.is_set():self.status.emit(self.generation,message)
    def _emit(self,signal,*args):
        if not self.stop_event.is_set():signal.emit(self.generation,*args)
    def _run(self):
        try:asyncio.run(self._listen())
        except asyncio.CancelledError:pass
        except Exception as exc:
            if not self.stop_event.is_set():
                code=getattr(exc,'status_code',None)
                message=(str(exc) if isinstance(exc,(SourceError,ASRError)) else
                    '翻译 API 认证失败，请检查独立 API Key。' if code in (401,403) else
                    '翻译 API 额度不足或限流，请稍后重试。' if code==429 else
                    '实时翻译连接失败，请检查音频设备、地址、模型和网络；可在设置中切换 Windows。')
                self._status(message)
        finally:self.finished.emit(self.generation)
    async def _listen(self):
        from openai import AsyncOpenAI
        self.loop=asyncio.get_running_loop(); self.task=asyncio.current_task()
        if self.stop_event.is_set():return
        self.source=make_provider(self.settings)
        # Reserve disjoint epochs across provider switches; reconnects increment locally.
        self.source.epoch=self.generation*1000
        async with AsyncOpenAI(api_key=self.settings.api_key,base_url=self.settings.base_url,
                organization='',project='',timeout=10,max_retries=0) as client:
            session=PipelineSession(self.source,self.settings,TranslationProvider(client,self.settings),
                lambda row:self._emit(self.caption_changed,row),self._status,
                lambda row:self._emit(self.diagnostic,row),lambda epoch:self._emit(self.stream_reset,epoch))
            try:await session.run()
            finally:
                self.metrics={**session.stabilizer.metrics,**session.scheduler.metrics}
                self.metrics['latency_ms']=list(self.metrics['latency_ms'])
                if session.provisional:self.metrics.update(session.provisional.metrics)
