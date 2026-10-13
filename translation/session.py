"""One continuous ASR pipeline with isolated UI-only provisional translations."""
import asyncio
from collections import OrderedDict
from dataclasses import asdict
from time import monotonic
from .pipeline import TranslationScheduler, TranslationProvider
from .provisional import ProvisionalTranslator
from .stabilizer import TranscriptStabilizer

class PipelineSession:
    def __init__(self,provider,settings,translation_provider,emit=lambda c:None,
                 status=lambda s:None,diagnostic=lambda d:None,reset=lambda e:None):
        self.provider,self.settings=provider,settings
        self.emit,self.status,self.diagnostic,self.reset_ui=emit,status,diagnostic,reset
        self.stabilizer=TranscriptStabilizer(int(settings.final_merge_window_ms),
                                            isolate_partials=provider.name=='openai')
        self.stabilizer.epoch=provider.epoch
        self.translation_io=None
        if isinstance(translation_provider,TranslationProvider):
            from .translation_io import TranslationIO
            self.translation_io=TranslationIO(translation_provider)
            translation_provider=self.translation_io
        self.scheduler=(TranslationScheduler(self.stabilizer,translation_provider,
            self._caption,status,int(settings.context_sentence_count)) if translation_provider else None)
        self.provisional=(ProvisionalTranslator(self.stabilizer,translation_provider,
            self.scheduler.context,self._provisional_caption,diagnostic,settings) if self.scheduler else None)
        self.capture=None;self.segment_stats=OrderedDict();self.segment_lookup={}
        self.closed=False
        self.audio_flow=dict(audio_blocks=0,audio_blocks_during_formal_translation=0,
            audio_blocks_during_provisional_translation=0)
        provider.on_partial=self._raw;provider.on_final=self._raw
        provider.on_status=self._status;provider.on_reset=self._reset
        provider.on_diagnostic=self._provider_diagnostic

    def _status(self,message):
        self.status(message)
        self.diagnostic(dict(stage='status',provider=self.provider.name,
            model=getattr(self.provider,'model','Windows Live Captions' if self.provider.name=='windows' else 'nova-3'),
            message=message,timestamp=monotonic()))

    def _provider_diagnostic(self,event):
        if self.closed or event.get('epoch',self.provider.epoch)!=self.provider.epoch:return
        if event.get('stage')=='segment_metrics':self._metrics(event)
        else:self.diagnostic(event)

    def _metrics(self,event):
        local=event.get('local_segment_id')
        if not local:return
        previous=self.segment_stats.setdefault(local,{})
        for key,value in event.items():
            if value is not None or key not in previous:previous[key]=value
        if previous.get('segment_id'):self.segment_lookup[previous['segment_id']]=local
        if previous.get('first_speech_time') is not None and previous.get('provisional_translation_time') is not None:
            previous['first_speech_to_first_provisional_translation_ms']=round(
                (previous['provisional_translation_time']-previous['first_speech_time'])*1000,1)
        end=previous.get('estimated_speech_end_time')
        for point,label in [('raw_final_time','raw_final'),('stable_final_time','stable_final'),
                ('provisional_translation_time','first_provisional_translation'),
                ('formal_translation_time','formal_translation')]:
            if end is not None and previous.get(point) is not None:
                previous['speech_end_to_'+label+'_ms']=round((previous[point]-end)*1000,1)
        while len(self.segment_stats)>256:
            removed,_=self.segment_stats.popitem(last=False)
            self.segment_lookup={k:v for k,v in self.segment_lookup.items() if v!=removed}
        self.diagnostic(dict(previous,stage='segment_metrics',timestamp=monotonic()))

    def _timed_caption(self,row,field,now,**extra):
        for segment_id in row.segment_ids:
            local=self.segment_lookup.get(segment_id)
            if local:
                prior=self.segment_stats[local]
                # First visible provisional is an onset metric, not the last update.
                if field=='provisional_translation_time' and prior.get(field) is not None:continue
                self._metrics(dict(local_segment_id=local,segment_id=segment_id,
                    caption_id=row.id,caption_version=row.version,**{field:now},**extra))

    def _raw(self,event):
        if self.closed or event.epoch!=self.provider.epoch:return
        if event.metadata:
            self._metrics(dict(event.metadata,segment_id=event.segment_id,
                **({'raw_final_time':event.timestamp} if event.is_final else {})))
        self.diagnostic(dict(stage='raw_final' if event.is_final else 'raw_partial',
            **asdict(event),model=getattr(self.provider,'model',None),
            latency_basis=getattr(self.provider,'latency_basis',None)))
        if event.is_final and self.provisional:
            cid=self.stabilizer.segment_groups.get((event.provider,event.epoch,event.segment_id))
            if cid:self.provisional.invalidate(cid)
        self.accept(self.stabilizer.feed(event,monotonic()))

    def accept(self,rows):
        for row in rows:
            if self.provisional:self.provisional.observe(row)
        if self.scheduler:self.scheduler.accept(rows)
        else:
            for row in rows:self._caption(row)

    def _caption(self,row):
        if self.closed:return
        self.emit(self.provisional.decorate(row) if self.provisional else row)
        now=monotonic()
        if row.state=='FINAL':self._timed_caption(row,'stable_final_time',now)
        elif row.state=='TRANSLATED':self._timed_caption(row,'formal_translation_time',now,
            translation_latency_ms=row.translation_latency_ms)
        if row.state in ('FINAL','TRANSLATED','ERROR','SKIPPED'):
            self.diagnostic(dict(stage='stable_final' if row.state=='FINAL' else row.state.lower(),
                provider=row.provider,model=getattr(self.provider,'model',None),
                latency_basis=getattr(self.provider,'latency_basis',None),epoch=row.epoch,
                caption_id=row.id,revision=row.version,language=row.language,text=row.source,
                stable_transcript=row.final_text or row.source,translation=row.translation,
                asr_latency_ms=row.asr_latency_ms,translation_latency_ms=row.translation_latency_ms,
                segment_ids=list(row.segment_ids),timestamp=now))

    def _provisional_caption(self,row,latency):
        if self.closed or row.epoch!=self.provider.epoch or not self.stabilizer.current(row):return
        self.emit(row);now=monotonic()
        self._timed_caption(row,'provisional_translation_time',now,provisional_latency_ms=latency)
        self.diagnostic(dict(stage='provisional',provider=row.provider,epoch=row.epoch,
            caption_id=row.id,revision=row.version,provisional_version=row.provisional_version,
            text=row.provisional_source,translation=row.provisional_translation,
            provisional_latency_ms=latency,segment_ids=list(row.segment_ids),timestamp=now))

    def _reset(self,epoch):
        if self.provisional:self.provisional.reset()
        if self.scheduler:self.scheduler.reset()
        self.stabilizer.reset(epoch);self.reset_ui(epoch)
        self.segment_stats.clear();self.segment_lookup.clear()
        self.diagnostic(dict(stage='reset',provider=self.provider.name,epoch=epoch,timestamp=monotonic()))

    def tick(self,now=None):
        self.accept(self.stabilizer.tick(now))
        if self.provisional:
            if self.scheduler.failed:self.provisional.enabled=False
            self.provisional.tick(now)

    async def _tick(self):
        while True:
            self.tick();await asyncio.sleep(.025)

    async def push_audio(self,pcm,captured_at=None):
        self.audio_flow['audio_blocks']+=1
        if self.scheduler and self.scheduler.active:
            self.audio_flow['audio_blocks_during_formal_translation']+=1
        if self.provisional and self.provisional.active:
            self.audio_flow['audio_blocks_during_provisional_translation']+=1
        return await self.provider.push_audio(pcm,captured_at)

    async def _audio(self):
        # Open the capture stream only once the initial ASR session is configured.
        # Subsequent commit/completed/translation events never touch its lifecycle.
        await self.provider.ready.wait()
        dropped=0;announced=False
        async for pcm,captured in self.capture.blocks():
            if not announced:
                announced=True
                self.diagnostic(dict(stage="capture_ready",provider=self.provider.name,sample_rate=self.capture.sample_rate,timestamp=monotonic()))
                self.status("正在连续收音 · "+("系统声音" if self.settings.audio_source=="system" else "麦克风"))
            if self.capture.dropped!=dropped:
                dropped=self.capture.dropped
                self.diagnostic(dict(stage='audio_discontinuity',provider=self.provider.name,
                    epoch=self.provider.epoch,reason='capture_queue_drop',capture_queue_drops=dropped,timestamp=monotonic()))
                self.status('音频队列发生丢块，正在恢复识别连接。')
                if self.provider.ws:self.provider.ws.transport.abort()
            await self.push_audio(pcm,captured)

    async def run(self,feed=None):
        tasks=[]
        try:
            if self.translation_io:
                try:await self.translation_io.start()
                except Exception:
                    self.scheduler.failed=True;self.provisional.enabled=False
                    self._status("翻译服务初始化失败，语音识别继续运行。")
            if self.provider.accepts_audio and feed is None:
                from .asr.audio_capture import AudioCapture
                self.capture=AudioCapture(self.settings.audio_source)
                self.provider.sample_rate=await self.capture.prepare()
            self.reset_ui(self.provider.epoch)
            await self.provider.start()
            tasks=[self.provider.task,asyncio.create_task(self._tick())]
            if self.scheduler:tasks.append(asyncio.create_task(self.scheduler.run()))
            if feed is not None:tasks.append(asyncio.create_task(feed(self)))
            elif self.capture:tasks.append(asyncio.create_task(self._audio()))
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:await task
        finally:
            self.closed=True
            # Stop the physical device immediately; translation cleanup can wait.
            if self.capture:self.capture.stopped.set()
            if self.scheduler:self.scheduler.close()
            if self.provisional:await self.provisional.close()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await self.provider.stop()
            if self.translation_io:await self.translation_io.close()
            self.diagnostic(dict(stage='audio_continuity',**self.audio_flow,
                translation_io_thread_stopped=not(self.translation_io and self.translation_io.thread.is_alive()),
                timestamp=monotonic()))
            if self.capture:
                await self.capture.stop()
                self.diagnostic(dict(stage='capture_summary',captured_blocks=self.capture.captured_blocks,
                    capture_queue_drops=self.capture.dropped,wasapi_discontinuities=self.capture.discontinuities,
                    driver_notices=list(self.capture.driver_notices),
                    capture_thread_stopped=not(self.capture.thread and self.capture.thread.is_alive()),
                    block_ms=self.capture.block_ms,pre_roll_ms=self.capture.pre_roll_ms,
                    native_sample_rate=self.capture.sample_rate,input_channels=self.capture.input_channels,timestamp=monotonic()))

    async def finish_file(self):
        await self.provider.finalize()
        self.accept(self.stabilizer.tick(force=True))
        if self.scheduler:
            deadline=monotonic()+15
            while (self.scheduler.pending or self.scheduler.active) and monotonic()<deadline:await asyncio.sleep(.025)
            if self.scheduler.pending or self.scheduler.active:
                self.status('测试结束等待超时；未完成译文不计成功。')
