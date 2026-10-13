"""Windows UIA adapter. Final is explicitly a local stability heuristic.
Windows owns audio capture and source language; confidence/timing are unavailable.
"""
from time import monotonic
from .base import ASRProvider
from .models import TranscriptEvent
from ..captions import CaptionAssembler
from ..windows_source import WindowsLiveCaptionsBackend

class WindowsASR(ASRProvider):
    name = 'windows'
    def __init__(self, language='ko', hotwords=(), endpoint_ms=650, close_owned=True,
                 source=None, baseline=True):
        super().__init__(language, hotwords)
        self.source = source or WindowsLiveCaptionsBackend(close_owned)
        self.assembler = CaptionAssembler(baseline=baseline, idle_seconds=endpoint_ms/1000,
                                          strict_revisions=True)

    def command(self, command): self.source.command(command)

    def process(self, event, now=None):
        now = monotonic() if now is None else now
        kind = event['kind']
        if kind == 'ready':
            self.ready.set()
            self.set_status('Windows 实时字幕 · 请在 Windows 选择源语言；final 为稳定推断，术语仅用于翻译')
            return
        rows = (self.assembler.feed(event['text'], now) if kind == 'snapshot'
                else self.assembler.tick(now) if kind == 'tick' else [])
        # Confirm earlier occurrences before displaying the next partial from the
        # same snapshot; otherwise unrelated complete sentences join one group.
        for row in sorted(rows, key=lambda row: row.state != 'FINAL'):
            if row.ignored or row.state not in ('LIVE', 'STABLE', 'FINAL'): continue
            self.emit(TranscriptEvent(row.source, row.state == 'FINAL', None,
                'und', now, self.name, str(row.id), row.version, self.epoch,
                speech_final=row.state == 'FINAL', cumulative=True))

    async def run(self):
        try:
            async for event in self.source.events(): self.process(event)
        finally:
            self.ready.clear()
            await self.source.close()
