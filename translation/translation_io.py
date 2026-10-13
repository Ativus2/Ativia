"""One translation I/O loop, isolated from continuous ASR send/receive."""
import asyncio
from collections import deque
import threading
from .pipeline import ContextBuffer

class TranslationIO:
    def __init__(self,provider):
        self.provider=provider;self.loop=None;self.thread=None;self.ready=threading.Event()
        self.closed=False;self.failure=False
    async def start(self):
        def run():
            self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop)
            try:
                # Lazy SDK resource imports happen before capture, on this I/O thread.
                self.provider.client.chat.completions
            except Exception:self.failure=True
            self.loop.call_soon(self.ready.set)
            self.loop.run_forever()
            self.loop.run_until_complete(self.loop.shutdown_asyncgens());self.loop.close()
        self.thread=threading.Thread(target=run,name='ativia-translation-io',daemon=True);self.thread.start()
        await asyncio.to_thread(self.ready.wait,15)
        if not self.ready.is_set() or self.failure:raise RuntimeError('translation initialization failed')
    async def translate(self,c,context):
        if self.closed:raise asyncio.CancelledError()
        snapshot=ContextBuffer(context.count);snapshot.rows=deque(context.rows,maxlen=96)
        task=asyncio.run_coroutine_threadsafe(self.provider.translate(c,snapshot),self.loop)
        return await asyncio.wrap_future(task)
    async def close(self):
        if self.closed:return
        self.closed=True
        if not self.loop or not self.thread or not self.loop.is_running():return
        async def cleanup():
            current=asyncio.current_task()
            tasks=[t for t in asyncio.all_tasks() if t is not current]
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            await self.provider.client.close()
        future=asyncio.run_coroutine_threadsafe(cleanup(),self.loop)
        try:await asyncio.wait_for(asyncio.wrap_future(future),5)
        except Exception:pass  # cleanup failure must not prevent the audio device from closing
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            await asyncio.to_thread(self.thread.join,2)
