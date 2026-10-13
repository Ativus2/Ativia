"""Small lifecycle contract shared by native and cloud ASR.
Callbacks execute on the worker event loop, never on the Qt UI thread.
Configure language/hotwords before start; stop and recreate to switch providers.
"""
import asyncio
from abc import ABC, abstractmethod

class ASRError(RuntimeError):
    """User-safe message only; never wrap a raw provider exception/URL/header."""

class ASRProvider(ABC):
    name = 'unknown'
    accepts_audio = False
    def __init__(self, language='ko', hotwords=()):
        self.language = language
        self.hotwords = tuple(hotwords)
        self.on_partial = lambda event: None
        self.on_final = lambda event: None
        self.on_status = lambda message: None
        self.on_reset = lambda epoch: None
        self.task = None
        self.status = 'stopped'
        self.ready = asyncio.Event()
        self.epoch = 0

    def set_language(self, language):
        self._check_stopped(); self.language = language

    def set_hotwords(self, words):
        self._check_stopped(); self.hotwords = tuple(words)

    def _check_stopped(self):
        if self.task and not self.task.done():
            raise ASRError('请停止识别后修改语言或术语。')

    def get_status(self): return self.status

    def set_status(self, value):
        self.status = value; self.on_status(value)

    def emit(self, event):
        (self.on_final if event.is_final else self.on_partial)(event)

    async def start(self):
        self._check_stopped()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.ready.clear(); self.status = 'stopped'

    async def push_audio(self, pcm, captured_at=None):
        raise ASRError('Windows 实时字幕自行采集音频，不能直接接收 PCM 文件。')

    async def finalize(self): pass
    def command(self, command): pass

    @abstractmethod
    async def run(self): ...
