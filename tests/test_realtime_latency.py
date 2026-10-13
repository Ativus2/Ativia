"""Endpoint, continuous PCM and provisional ordering regressions; real cloud evidence is separate."""
import asyncio,base64,json,struct,time,unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock,AsyncMock,patch
from translation.asr.commit_controller import CommitController,EndpointConfig
from translation.asr.openai_asr import OpenAIASRProvider
from translation.asr.models import TranscriptEvent
from translation.captions import Caption
from translation.stabilizer import TranscriptStabilizer
from translation.pipeline import ContextBuffer,TranslationProvider,TranslationScheduler
from translation.provisional import ProvisionalTranslator
from translation.translation_io import TranslationIO
from live_translation import TranslationSettings

def ev(text,final=False,segment='A',revision=1,**metadata):
    return TranscriptEvent(text,final,None,'en',time.monotonic(),'openai',segment,revision,0,metadata=metadata)

class EndpointTests(unittest.TestCase):
    def drive(self,c,level,ms):
        decisions=[]
        for _ in range(ms//20):
            c.observe(struct.pack('<h',level)*480, c.cursor/24000+.02)
            decision=c.decide(max(c.start,c.cursor-7680))
            if decision:decisions.append(decision);c.committed(decision[0])
        return decisions
    def test_silence_retains_next_prefix(self):
        c=CommitController();self.assertEqual(self.drive(c,3000,3000),[])
        d=self.drive(c,0,500);self.assertEqual(len(d),1);self.assertEqual(d[0][1],'silence')
        self.assertEqual(c.cursor-c.start,7680)
    def test_short_pause_only_after_soft_max(self):
        c=CommitController();self.drive(c,3000,3500)
        self.assertEqual(self.drive(c,0,200),[])
        self.assertEqual(self.drive(c,3000,1400),[])
        self.assertEqual(self.drive(c,0,300)[0][1],'soft_endpoint')
    def test_continuous_speech_hard_cap_without_periodic_cut(self):
        c=CommitController();self.assertEqual(self.drive(c,3000,8980),[])
        self.assertEqual(self.drive(c,3000,20)[0][1],'hard_timeout')
    def test_cannot_cut_already_sent_pcm(self):
        c=CommitController();self.drive(c,3000,5200)
        c.candidates.append((c.cursor-12000,c.cursor-7200))
        self.assertIsNone(c.decide(c.cursor))
    def test_presets_really_change_controller(self):
        self.assertEqual([(EndpointConfig.from_settings(TranslationSettings(recognition_mode=m)).silence_ms,
                          EndpointConfig.from_settings(TranslationSettings(recognition_mode=m)).hard_ms)
                         for m in ['low','balanced','accurate']],[(400,7500),(500,9000),(700,14000)])

class ContinuousAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pcm_once_across_commits_without_waiting_for_finals(self):
        p=OpenAIASRProvider(sample_rate=24000,endpoint_config=EndpointConfig())
        sent=[];p.ws=Mock(send=AsyncMock(side_effect=lambda data:sent.append(json.loads(data))))
        p.ready.set();task=asyncio.create_task(p._sender())
        data=(struct.pack('<h',2500)*24000*3+b'\0'*24000+
              struct.pack('<h',3100)*24000*10+b'\0'*24000)
        try:
            for i in range(0,len(data),960):
                await p.push_audio(data[i:i+960],time.monotonic())
                if i%(960*20)==0:await p.send_queue.join()
            await p.send_queue.join()
            marker=asyncio.get_running_loop().create_future()
            await p.send_queue.put(marker);await marker
            actual=b''.join(base64.b64decode(e['audio']) for e in sent if e['type']=='input_audio_buffer.append')
            self.assertEqual(actual,data)
            reasons=[r['commit_reason'] for r in p.records if r['commit_reason']]
            self.assertIn('silence',reasons);self.assertIn('hard_timeout',reasons)
            self.assertEqual(p.queue_drops,0);self.assertEqual(p.commit_acks,0)
            self.assertGreater(sum(x['type']=='input_audio_buffer.commit' for x in sent),2)
            first=next(i for i,x in enumerate(sent) if x['type']=='input_audio_buffer.commit')
            self.assertTrue(any(x['type']=='input_audio_buffer.append' for x in sent[first+1:]))
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    async def test_translation_io_cannot_block_asr_loop_and_exits(self):
        class Slow:
            client=Mock(chat=Mock(completions=Mock()),close=AsyncMock())
            async def translate(self,c,context):time.sleep(.15);return '译文'
        io=TranslationIO(Slow());await io.start()
        try:
            task=asyncio.create_task(io.translate(Caption(1,'test'),ContextBuffer()))
            began=time.monotonic();await asyncio.sleep(.03)
            self.assertLess(time.monotonic()-began,.1)
            self.assertEqual(await task,'译文')
        finally:await io.close()
        self.assertFalse(io.thread.is_alive())

class StableMetadataTests(unittest.TestCase):
    def test_endpoint_waits_are_nonzero_and_adaptive(self):
        for reason,quiet,delay in [('silence',500,.35),('soft_endpoint',180,.65),('hard_timeout',0,.9)]:
            s=TranscriptStabilizer(isolate_partials=True)
            s.feed(ev('A complete sentence.',True,commit_reason=reason,silence_before_commit_ms=quiet),10)
            self.assertEqual(s.tick(10+delay-.01),[])
            self.assertEqual(len(s.tick(10+delay+.01)),1)
    def test_next_partial_does_not_hold_previous_final(self):
        s=TranscriptStabilizer(isolate_partials=True)
        s.feed(ev('First complete.',True,segment_number=1),10)
        s.feed(ev('And next',segment='B',segment_number=2),10.2)
        self.assertEqual([r.source for r in s.tick(10.7)],['First complete.'])
        self.assertEqual(s.records[2].state,'PARTIAL')
    def test_out_of_order_partial_keeps_audio_order(self):
        s=TranscriptStabilizer(isolate_partials=True)
        s.feed(ev('Second',segment='B',segment_number=2),10)
        s.feed(ev('First.',True,segment_number=1),10.1)
        self.assertEqual(list(s.records),[1,2])
    def test_adjacent_final_merge_preserved(self):
        s=TranscriptStabilizer(isolate_partials=True)
        s.feed(ev("I don't think",True,segment_number=1),10)
        rows=s.feed(ev('this is bad.',True,segment='B',segment_number=2),10.3)
        self.assertEqual(rows[0].state,'MERGED')
        self.assertEqual(s.tick(11)[0].source,"I don't think this is bad.")

class ProvisionalTests(unittest.IsolatedAsyncioTestCase):
    def manager(self,provider=None):
        self.s=TranscriptStabilizer(isolate_partials=True);self.out=[];self.diag=[];self.context=ContextBuffer()
        self.provider=provider or Mock(translate=AsyncMock(return_value='临时中文'))
        self.p=ProvisionalTranslator(self.s,self.provider,self.context,lambda c,lat:self.out.append(c),self.diag.append,TranslationSettings())
        self.addAsyncCleanup(self.p.close)
    def partial(self,text,revision,now,segment='A'):
        row=self.s.feed(ev(text,segment=segment,revision=revision),now)[-1]
        with patch('translation.provisional.monotonic',return_value=now):self.p.observe(row)
        return row
    async def test_debounce_two_request_cap_and_no_formal_context(self):
        self.manager();self.partial('I think this system is',1,10)
        self.p.tick(10.49);self.assertEqual(self.p.metrics['provisional_requests'],0)
        self.p.tick(10.51);await asyncio.sleep(.01)
        self.partial('I think this system is much better than the old one',2,11)
        self.p.tick(11.6);self.assertEqual(self.p.metrics['provisional_requests'],1)
        self.p.tick(12.1);await asyncio.sleep(.01)
        self.partial('I think this system is much better than the old one and works continuously',3,13)
        self.p.tick(15);await asyncio.sleep(.01)
        self.assertEqual(self.p.metrics['provisional_requests'],2)
        self.assertEqual(list(self.context.rows),[])
        self.assertTrue(all(c.translation=='' for c in self.out))
        self.assertTrue(all(c.provisional_translation=='临时中文' for c in self.out))
        self.assertEqual(self.s.records[1].provisional_translation,'')
    async def test_append_only_growth_can_show_stable_prefix(self):
        self.manager();self.partial('This is a stable prefix',1,10)
        self.partial('This is a stable prefix and more words',2,10.3)
        self.p.tick(10.6);await asyncio.sleep(.01)
        self.assertEqual(len(self.out),1)
        self.assertEqual(self.out[0].source,'This is a stable prefix and more words')
    async def test_final_and_reset_reject_cancel_ignoring_old_result(self):
        for reset in (False,True):
            started=asyncio.Event();release=asyncio.Event()
            async def delayed(c,context):
                started.set()
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
                return '旧临时译文'
            self.manager(Mock(translate=delayed))
            self.partial('A sufficiently long sentence',1,10);self.p.tick(10.6);await started.wait()
            if reset:self.p.reset();self.s.reset(1)
            else:
                self.p.invalidate(1)
                self.s.feed(ev('A sufficiently long sentence.',True,revision=2),11)
                row=self.s.tick(12)[0];self.s.update(row,'TRANSLATED','正式译文')
            release.set();await asyncio.sleep(.01)
            self.assertEqual(self.out,[])
            if not reset:self.assertEqual(self.s.records[1].translation,'正式译文')
            await self.p.close()
    async def test_wrong_target_language_is_not_shown(self):
        client=Mock();client.chat.completions.create=AsyncMock(return_value=Mock(choices=[Mock(message=Mock(content='상황이 되었죠.'))]))
        p=TranslationProvider(client,TranslationSettings())
        with self.assertRaises(ValueError):await p.translate(Caption(1,'한국어 내용',state='PARTIAL'),ContextBuffer())
    async def test_formal_auth_failure_does_not_end_scheduler(self):
        s=TranscriptStabilizer();rows=[];error=RuntimeError('private');error.status_code=401
        scheduler=TranslationScheduler(s,Mock(translate=AsyncMock(side_effect=error)),rows.append,lambda s:None)
        task=asyncio.create_task(scheduler.run())
        try:
            now=time.monotonic();scheduler.accept(s.feed(ev('First.',True),now-1));scheduler.accept(s.tick(now))
            deadline=time.monotonic()+2
            while not scheduler.failed and not task.done() and time.monotonic()<deadline:await asyncio.sleep(.005)
            self.assertTrue(scheduler.failed);self.assertFalse(task.done())
            scheduler.accept(s.feed(ev('Second.',True,segment='B'),now+1));scheduler.accept(s.tick(now+2))
            self.assertTrue(any(c.source=='Second.' and c.state=='SKIPPED' for c in rows))
        finally:scheduler.close();task.cancel();await asyncio.gather(task,return_exceptions=True)


class MicrophoneFormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_stereo_device_is_downmixed_without_dropping_frames(self):
        from translation.asr.audio_capture import AudioCapture
        c=AudioCapture()
        with patch('sounddevice.query_devices',return_value=dict(default_samplerate=48000,max_input_channels=2)):
            self.assertEqual(await c.prepare(),48000)
        self.assertEqual(c.input_channels,2)
        stereo=struct.pack('<hh',2000,1000)*960
        class Stream:
            def __init__(self,**kwargs):self.kw=kwargs
            def __enter__(self):
                self.kw['callback'](stereo,960,None,SimpleNamespace(input_overflow=False))
                c.stopped.set()
            def __exit__(self,*a):pass
        with patch('sounddevice.RawInputStream',side_effect=Stream):c._record()
        pcm,_=c.queue.get_nowait()
        self.assertEqual(pcm,struct.pack('<h',1500)*960)
        self.assertEqual(c.dropped,0)
    async def test_single_channel_device_remains_supported(self):
        from translation.asr.audio_capture import AudioCapture
        c=AudioCapture()
        with patch('sounddevice.query_devices',return_value=dict(default_samplerate=44100,max_input_channels=1)):
            await c.prepare()
        self.assertEqual(c.input_channels,1)

class StopOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_stops_before_slow_translation_cleanup(self):
        import threading
        from translation.session import PipelineSession
        provider=OpenAIASRProvider()
        async def start():provider.task=asyncio.create_task(asyncio.sleep(.01))
        provider.start=start;provider.stop=AsyncMock()
        session=PipelineSession(provider,TranslationSettings(),None)
        capture=SimpleNamespace(stopped=threading.Event(),stop=AsyncMock(),captured_blocks=0,
            dropped=0,discontinuities=0,driver_notices=[],thread=None,block_ms=20,pre_roll_ms=400,
            sample_rate=48000,input_channels=2)
        session.capture=capture
        async def cleanup():
            self.assertTrue(capture.stopped.is_set())
            await asyncio.sleep(.03)
        session.translation_io=SimpleNamespace(start=AsyncMock(),close=cleanup,thread=Mock(is_alive=Mock(return_value=False)))
        async def feed(session):await asyncio.sleep(60)
        await session.run(feed=feed)
        capture.stop.assert_awaited_once();provider.stop.assert_awaited_once()
