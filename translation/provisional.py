"""UI-only speculation through the existing TranslationProvider and formal context."""
import asyncio
from collections import deque
from dataclasses import replace
from time import monotonic
from .pipeline import ContextBuffer

class ProvisionalTranslator:
    def __init__(self, assembler, provider, context, emit, diagnostic, settings):
        self.assembler,self.provider,self.context=assembler,provider,context
        self.emit,self.diagnostic=emit,diagnostic
        self.enabled=getattr(settings,'provisional_translation_enabled','yes')=='yes'
        presets={'low':400,'balanced':500,'accurate':700}
        self.debounce=(int(getattr(settings,'provisional_debounce_ms','0')) or presets.get(settings.recognition_mode,500))/1000
        self.interval=int(getattr(settings,'provisional_interval_ms','1500'))/1000
        self.min_chars=int(getattr(settings,'provisional_min_chars','8'))
        self.min_new=int(getattr(settings,'provisional_min_new_chars','8'))
        self.limit=2
        self.current=None;self.history=deque(maxlen=100);self.display=None
        self.active=None;self.retired=set();self.serial=0;self.epoch_token=0;self.closed=False
        self.last_request=-1e9;self.requests_for_id=0;self.last_source=''
        self.metrics={'provisional_requests':0,'provisional_shown':0,'provisional_cancelled':0,'provisional_stale':0}

    def _cancel(self):
        if self.active and not self.active.done():
            self.active.cancel();self.retired.add(self.active)
            self.active.add_done_callback(self.retired.discard)
            self.metrics['provisional_cancelled']+=1
        self.active=None;self.serial+=1

    def observe(self,c):
        if c.state!='PARTIAL':
            if self.current and c.id==self.current.id:
                self.invalidate(c.id)
            return
        now=monotonic()
        if self.current is None or (c.epoch,c.id)!=(self.current.epoch,self.current.id):
            self._cancel();self.history.clear();self.display=None
            self.requests_for_id=0;self.last_source=''
        elif self.active and self.last_source and not c.source.startswith(self.last_source):
            self._cancel()
        if self.display and not c.source.startswith(self.display[0]):self.display=None
        self.current=c
        self.history.append((now,c.source,c.version))

    def invalidate(self,cid=None):
        if cid is not None and self.current and self.current.id!=cid:return
        self._cancel();self.current=None;self.history.clear();self.display=None

    def decorate(self,c):
        if (c.state=='PARTIAL' and self.current and c.id==self.current.id and
                c.epoch==self.current.epoch and self.display and c.source.startswith(self.display[0])):
            return replace(c,provisional_translation=self.display[1],provisional_source=self.display[0],
                           provisional_version=self.display[2])
        return c

    def tick(self,now=None):
        now=monotonic() if now is None else now;c=self.current
        if not self.enabled or self.closed or c is None or self.active is not None or len(self.retired)>1:return
        if self.requests_for_id>=self.limit or now-self.last_request<self.interval:return
        if not self.assembler.current(c):return
        # A prefix can stabilize while words continue arriving. Waiting for every
        # delta to stop would starve speculation during fluent speech.
        candidates=[entry for entry in self.history if now-entry[0]>=self.debounce]
        if not candidates:return
        source=candidates[-1][1]
        if not c.source.startswith(source):return
        if source!=c.source and ' ' in source:
            source=source.rsplit(' ',1)[0]  # don't submit a possibly growing last word
        if len(source.strip())<self.min_chars:return
        if self.last_source and (len(source)-len(self.last_source)<self.min_new):return
        self.serial+=1;serial=self.serial;token=self.epoch_token
        self.requests_for_id+=1;self.last_request=now;self.last_source=source
        self.metrics['provisional_requests']+=1
        snapshot=replace(c,source=source,partial_text=source)
        context=ContextBuffer(self.context.count);context.rows=deque(self.context.rows,maxlen=96)
        self.diagnostic(dict(stage='provisional_request',caption_id=c.id,revision=c.version,epoch=c.epoch,
                             provider=c.provider,source=source,timestamp=now,segment_ids=list(c.segment_ids)))
        self.active=asyncio.create_task(self._translate(snapshot,context,serial,token,now))

    async def _translate(self,c,context,serial,token,started):
        try:
            value=await self.provider.translate(c,context)
            current=self.current
            if (self.closed or token!=self.epoch_token or serial!=self.serial or current is None or
                    current.epoch!=c.epoch or current.id!=c.id or current.version<c.version or
                    not current.source.startswith(c.source) or not self.assembler.current(current) or
                    current.state!='PARTIAL'):
                self.metrics['provisional_stale']+=1;return
            self.display=(c.source,value,serial)
            self.metrics['provisional_shown']+=1
            self.emit(self.decorate(current),round((monotonic()-started)*1000,1))
        except asyncio.CancelledError:pass
        except Exception:
            self.diagnostic(dict(stage='provisional_error',provider=c.provider,epoch=c.epoch,
                caption_id=c.id,revision=c.version,message='临时翻译未完成，等待正式译文。',timestamp=monotonic()))
        finally:
            if serial==self.serial:self.active=None

    def reset(self):
        self.epoch_token+=1;self.invalidate();self.last_request=-1e9

    async def close(self):
        self.closed=True;self.reset()
        tasks=list(self.retired)
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
