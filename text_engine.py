"""One lazy async text/vision connection worker. No audio imports or output branch."""
import asyncio
from collections import deque
import os
import threading
from time import monotonic
from urllib.parse import urlsplit
from PySide6.QtCore import QObject,Signal


class TextEngine(QObject):
    delta=Signal(str,str)
    completed=Signal(str,str,object)
    failed=Signal(str,str)
    settled=Signal(str)
    closed=Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self._loop=None;self._thread=None;self._client=None;self._future=None;self._lock=None
        self._closing=False;self.metrics=deque(maxlen=64);self._inflight=0

    def submit(self,reply_id,request,valid):
        if self._closing:return
        self.cancel()
        if self._loop is None:
            self._loop=asyncio.new_event_loop()
            self._thread=threading.Thread(target=self._run,name='ativia-text-io',daemon=True)
            self._thread.start()
        self._future=asyncio.run_coroutine_threadsafe(self._generate(reply_id,request,valid),self._loop)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._lock=asyncio.Lock()
        self._loop.run_forever()
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.close();self.closed.emit()

    async def _generate(self,reply_id,request,valid):
        self._inflight+=1
        began=monotonic();chunks=[];pending=[];last_flush=began
        metrics=dict(reply_id=reply_id,first_text_ms=None,elapsed_ms=None,status='cancelled',usage=None)
        try:
            async with asyncio.timeout(45), self._lock:
                if not valid():return
                # Import network/model code only after explicit keyboard or image input.
                from openai import AsyncOpenAI
                from vision import build_text_request,build_manual_image_request
                if self._client is None:self._client=AsyncOpenAI(timeout=30.0,max_retries=0)
                metrics['host']=urlsplit(str(self._client.base_url)).hostname
                params=(build_manual_image_request(**request) if request.get('image_jpeg') is not None
                        else build_text_request(**request))
                metrics['model']=params['model'];metrics['output_budget']=params['max_completion_tokens']
                if not valid():return
                stream=await self._client.chat.completions.create(**params,stream=True,stream_options={'include_usage':True})
                async with stream:
                    async for chunk in stream:
                        if not valid():return
                        usage=getattr(chunk,'usage',None)
                        if usage is not None:metrics['usage']=usage.model_dump()
                        content=chunk.choices[0].delta.content if chunk.choices else None
                        if not content:continue
                        chunks.append(content);pending.append(content)
                        now=monotonic()
                        first=metrics['first_text_ms'] is None
                        if first:
                            metrics['first_text_ms']=(now-began)*1000
                            from performance.metrics import metrics as telemetry
                            telemetry.mark(reply_id,'agent_first_token',ms=metrics['first_text_ms'])
                            metrics['actual_model']=getattr(chunk,'model',None)
                        if first or now-last_flush>=.04:
                            self.delta.emit(reply_id,''.join(pending));pending.clear();last_flush=now
                    if pending and valid():self.delta.emit(reply_id,''.join(pending))
                if not valid():return
                text=''.join(chunks).strip()
                if not text or text=='[SILENT]':raise ValueError('这次没有生成有效内容，请再试一次。')
                metrics['status']='completed';metrics['elapsed_ms']=(monotonic()-began)*1000
                self.completed.emit(reply_id,text,dict(metrics))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if valid():
                from vision import _friendly_error
                metrics['status']='failed'
                self.failed.emit(reply_id,str(exc) if isinstance(exc,ValueError) else _friendly_error(exc))
        finally:
            self._inflight-=1
            metrics['elapsed_ms']=(monotonic()-began)*1000
            from performance.metrics import metrics as telemetry
            telemetry.mark(reply_id,'agent_done',ms=metrics['elapsed_ms'])
            self.metrics.append(metrics);request.clear();self.settled.emit(reply_id)

    def cancel(self):
        if self._future is not None and not self._future.done():self._future.cancel()

    def close(self):
        if self._closing:return
        self._closing=True;self.cancel()
        if self._loop is None:self.closed.emit();return
        async def cleanup():
            current=asyncio.current_task()
            tasks=[t for t in asyncio.all_tasks() if t is not current]
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            if self._client is not None:await self._client.close();self._client=None
            self._loop.stop()
        asyncio.run_coroutine_threadsafe(cleanup(),self._loop)

    def snapshot(self):
        return dict(worker_threads=int(bool(self._thread and self._thread.is_alive())),
                    pending_requests=max(self._inflight,int(bool(self._future and not self._future.done()))),
                    recent_requests=list(self.metrics))
