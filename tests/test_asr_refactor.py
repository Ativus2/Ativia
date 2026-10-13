import asyncio
from dataclasses import replace
import json
from time import monotonic
import unittest
from unittest.mock import AsyncMock, Mock
from translation.asr.models import TranscriptEvent
from translation.stabilizer import TranscriptStabilizer, join_text
from translation.pipeline import ContextBuffer, TranslationProvider, TranslationScheduler
from translation.captions import Caption
from live_translation import TranslationSettings


def event(text,final=True,segment='1',revision=1,epoch=0,provider='deepgram',**kw):
    return TranscriptEvent(text,final,None,'en',monotonic(),provider,segment,revision,epoch,**kw)

class StabilizerTests(unittest.TestCase):
    def test_partial_is_never_translated_even_after_long_silence(self):
        s=TranscriptStabilizer()
        for i,text in enumerate(["I don't", "I don't think", "I don't think this is"]):
            self.assertFalse(any(r.state=='FINAL' for r in s.feed(event(text,False,revision=i+1),i)))
        self.assertEqual(s.tick(999),[])
    def test_negation_adjacent_finals_merge_across_languages(self):
        for left,right,expected in [("I don't think","this is actually a bad idea.","I don't think this is actually a bad idea."),
            ('저는 이것이','나쁜 생각이라고 보지 않아요.','저는 이것이 나쁜 생각이라고 보지 않아요.'),
            ('これは','悪い考えだとは思いません。','これは悪い考えだとは思いません。'),
            ('我不认为','这真是个坏主意。','我不认为这真是个坏主意。')]:
            with self.subTest(language=left):
                s=TranscriptStabilizer();s.feed(event(left),10);s.feed(event(right,segment='2'),10.3)
                self.assertEqual(s.tick(10.8),[])
                rows=s.tick(11);self.assertEqual([r.source for r in rows],[expected])
    def test_duplicate_final_is_not_duplicate_speech(self):
        s=TranscriptStabilizer();s.feed(event('Yes.'),0)
        self.assertEqual(s.feed(event('Yes.'),.1),[])
        first=s.tick(1);s.feed(event('Yes.',segment='2'),2);second=s.tick(3)
        self.assertEqual([r.source for r in first+second],['Yes.','Yes.'])
        self.assertNotEqual(first[0].id,second[0].id)
    def test_prefix_revisions_replace_same_occurrence(self):
        s=TranscriptStabilizer();s.feed(event('hello everyone'),0)
        s.feed(event('hello everyone welcome back',revision=2),.3)
        self.assertEqual(s.tick(1)[0].source,'hello everyone welcome back')
    def test_overlap_does_not_delete_real_repeated_words(self):
        self.assertEqual(join_text('very very','very good'), 'very very very good')
        self.assertEqual(join_text('hello everyone','everyone welcome back',True),'hello everyone welcome back')
    def test_pending_interim_keeps_final_prefix_but_not_translatable(self):
        s=TranscriptStabilizer();s.feed(event("I don't think"),0)
        s.feed(event('this is',False,segment='2'),.3)
        self.assertEqual(s.tick(5),[])
        s.feed(event('this is a bad idea.',segment='2',revision=2),5)
        self.assertEqual(s.tick(6)[0].source,"I don't think this is a bad idea.")
    def test_old_epochs_and_revisions_are_ignored(self):
        s=TranscriptStabilizer();s.feed(event('new',False,revision=2),0)
        self.assertEqual(s.feed(event('old',False,revision=1),.1),[])
        s.reset(1);self.assertEqual(s.feed(event('late')),[])
        s.feed(event('current',epoch=1),3);self.assertEqual(s.tick(4)[0].source,'current')
    def test_many_fast_finals_remain_one_sentence(self):
        s=TranscriptStabilizer()
        for i,word in enumerate(['I','do','not','think','this','is','bad.']):s.feed(event(word,segment=str(i)),i*.2)
        rows=s.tick(2);self.assertEqual(len(rows),1);self.assertEqual(rows[0].source,'I do not think this is bad.')
    def test_state_and_segments_bounded(self):
        s=TranscriptStabilizer()
        for i in range(1200):s.feed(event(f'{i}.',segment=str(i)),i*2);s.tick(i*2+1)
        self.assertLessEqual(len(s.records),96);self.assertLessEqual(len(s.segment_groups),96)

class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_zero_to_five_includes_original_before_translation(self):
        for count in range(6):
            context=ContextBuffer(count)
            for i in range(10):context.add(Caption(i,f'source{i}'))
            self.assertEqual(len(context.messages(8)),count)
            self.assertNotIn('source8',str(context.messages(8)));self.assertNotIn('source9',str(context.messages(8)))
    async def test_context_prompt_only_current_and_language_glossary(self):
        client=Mock();client.chat.completions.create=AsyncMock(return_value=Mock(choices=[Mock(message=Mock(content='译文'))]))
        settings=TranslationSettings(hotwords='Malenia, Rhodes Island')
        provider=TranslationProvider(client,settings);context=ContextBuffer();context.add(Caption(1,'She is Malenia.'))
        await provider.translate(Caption(2,'This weapon melts her.',language='en'),context)
        messages=client.chat.completions.create.call_args.kwargs['messages']
        self.assertIn('不能重复翻译前文',messages[0]['content']);self.assertIn('Malenia',messages[0]['content'])
        self.assertEqual(messages[-1]['content'],'This weapon melts her.')
    async def test_reset_rejects_api_that_ignores_cancellation(self):
        stabilizer=TranscriptStabilizer();out=[];entered=asyncio.Event();release=asyncio.Event()
        async def slow(c,context):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            return '过期译文'
        scheduler=TranslationScheduler(stabilizer,Mock(translate=slow),out.append,lambda s:None)
        task=asyncio.create_task(scheduler.run());now=monotonic()
        scheduler.accept(stabilizer.feed(event('Old.'),now-1));scheduler.accept(stabilizer.tick(now))
        await entered.wait();scheduler.reset();stabilizer.reset(1);release.set();await asyncio.sleep(.02)
        self.assertFalse(any(r.translation for r in out));scheduler.close();task.cancel();await asyncio.gather(task,return_exceptions=True)

class DeepgramTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_protocol_final_interim_hotwords_and_audio(self):
        from websockets.asyncio.server import serve
        from translation.asr.deepgram_asr import DeepgramASR
        from urllib.parse import parse_qs,urlsplit
        heard=asyncio.Event();captured={};events=[]
        async def server(ws):
            captured['query']=parse_qs(urlsplit(ws.request.path).query)
            captured['authorized']=ws.request.headers['Authorization']=='Token test-only'
            async for frame in ws:
                if isinstance(frame,bytes):
                    captured['bytes']=len(frame)
                    for final,text in [(False,"I don't"),(True,"I don't think this is bad.")]:
                        await ws.send(json.dumps(dict(type='Results',start=0,duration=.1,is_final=final,speech_final=final,
                            channel={'alternatives':[{'transcript':text,'confidence':.9,'words':[{'end':.1}]}]})))
                    heard.set()
                elif json.loads(frame)['type']=='Finalize':
                    await ws.send(json.dumps(dict(type='Results',from_finalize=True,channel={'alternatives':[{'transcript':''}]})))
        async with serve(server,'127.0.0.1',0) as server:
            p=DeepgramASR(language='ko',hotwords=['Rhodes Island','Malenia'],key_loader=lambda:'test-only')
            p.endpoint=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/listen'
            p.on_partial=events.append;p.on_final=events.append
            await p.start();await asyncio.wait_for(p.ready.wait(),2)
            await p.push_audio(b'\0'*3200,monotonic());await asyncio.wait_for(heard.wait(),2)
            await p.finalize();await asyncio.sleep(.03);await p.stop()
        self.assertEqual([r.is_final for r in events],[False,True])
        self.assertEqual(captured['query']['keyterm'],['Rhodes Island','Malenia']);self.assertTrue(captured['authorized'])
        self.assertEqual(captured['query']['language'],['ko']);self.assertEqual(captured['bytes'],3200)
        self.assertEqual(events[1].confidence,.9);self.assertIsNotNone(events[1].asr_latency_ms)
    async def test_network_failure_has_bounded_retries_safe_error_and_epochs(self):
        from translation.asr.deepgram_asr import DeepgramASR
        from translation.asr.base import ASRError
        connector=Mock(side_effect=OSError('secret-key-do-not-leak'));statuses=[];epochs=[]
        p=DeepgramASR(connector=connector,key_loader=lambda:'private-key',retry_delays=(.001,.001,.001))
        p.on_status=statuses.append;p.on_reset=epochs.append
        with self.assertRaises(ASRError) as raised:await p.run()
        self.assertEqual(connector.call_count,4);self.assertEqual(epochs,[1,2,3,4])
        self.assertNotIn('secret',str(raised.exception));self.assertNotIn('private',str(statuses))
    async def test_missing_key_does_not_connect(self):
        from translation.asr.deepgram_asr import DeepgramASR
        from translation.asr.base import ASRError
        connector=Mock();p=DeepgramASR(connector=connector,key_loader=lambda:'')
        with self.assertRaises(ASRError):await p.run()
        connector.assert_not_called()
    async def test_stop_cancels_reconnect_wait(self):
        from translation.asr.deepgram_asr import DeepgramASR
        p=DeepgramASR(connector=Mock(side_effect=OSError()),key_loader=lambda:'test',retry_delays=(30,))
        await p.start();await asyncio.sleep(.01);await asyncio.wait_for(p.stop(),.5)
        self.assertTrue(p.task.done())

class WindowsProviderTests(unittest.TestCase):
    def test_windows_uses_same_contract_null_confidence_and_configurable_final(self):
        from translation.asr.windows_asr import WindowsASR
        p=WindowsASR(endpoint_ms=1000,source=Mock(),baseline=False);events=[]
        p.on_partial=events.append;p.on_final=events.append
        p.process({'kind':'snapshot','text':'안녕하세요.'},10)
        p.process({'kind':'tick'},10.7);self.assertFalse(any(e.is_final for e in events))
        p.process({'kind':'tick'},11.1);self.assertTrue(events[-1].is_final)
        self.assertIsNone(events[-1].confidence);self.assertIsNone(events[-1].asr_latency_ms)
        self.assertEqual(events[-1].language,'und')


class RollingSnapshotRegression(unittest.TestCase):
    def test_tail_keeps_identity_when_more_than_one_old_sentence_scrolls_out(self):
        from translation.asr.windows_asr import WindowsASR
        p=WindowsASR(source=Mock(),baseline=False);s=TranscriptStabilizer();seen=[]
        p.on_partial=lambda e:seen.extend(s.feed(e,e.timestamp))
        p.on_final=lambda e:seen.extend(s.feed(e,e.timestamp))
        p.process({'kind':'snapshot','text':'First. Second. Third. So today we are going to'},1)
        original=p.assembler.window[-1].id
        p.process({'kind':'snapshot','text':'Third. So today we are going to discuss administrative details.'},1.2)
        self.assertEqual(p.assembler.window[-1].id,original)
        p.process({'kind':'tick'},3);seen+=s.tick(4)
        final=[c for c in seen if c.state=='FINAL' and 'So today' in c.source]
        self.assertEqual(len(final),1)
        self.assertEqual(final[0].source,'So today we are going to discuss administrative details.')

class FinalRevisionTests(unittest.TestCase):
    def test_punctuation_after_translation_does_not_retranslate(self):
        s=TranscriptStabilizer();s.feed(event('안녕하세요'),0);row=s.tick(1)[0]
        s.update(row,'TRANSLATED','你好')
        self.assertEqual(s.feed(event('안녕하세요.',revision=2),2),[])
        self.assertEqual(s.records[row.id].translation,'你好')
    def test_semantic_correction_still_invalidates_committed_translation(self):
        s=TranscriptStabilizer();s.feed(event('Submit by Thursday.'),0);row=s.tick(1)[0]
        s.update(row,'TRANSLATED','周四')
        s.feed(event('Submit by Friday.',revision=2),2)
        self.assertFalse(s.current(row));self.assertEqual(s.tick(3)[0].source,'Submit by Friday.')

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_websocket_disconnect_reconnect_and_stop(self):
        from websockets.asyncio.server import serve
        from translation.asr.deepgram_asr import DeepgramASR
        counter=0;epochs=[];events=[];received=asyncio.Event()
        async def server(ws):
            nonlocal counter
            counter+=1
            if counter==1:
                await ws.close(code=1011);return
            await ws.send(json.dumps(dict(type='Results',start=0,duration=1,is_final=True,
                channel={'alternatives':[{'transcript':'Recovered.','confidence':.8}]})))
            await ws.wait_closed()
        async with serve(server,'127.0.0.1',0) as server:
            p=DeepgramASR(key_loader=lambda:'test-only',retry_delays=(.01,))
            p.endpoint=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            p.on_reset=epochs.append
            def accept(event):events.append(event);received.set()
            p.on_final=accept;await p.start();await asyncio.wait_for(received.wait(),2);await p.stop()
        self.assertEqual(counter,2);self.assertEqual(epochs,[1]);self.assertEqual(events[0].epoch,1)
    async def test_auth_failure_is_not_retried_and_does_not_expose_response(self):
        from translation.asr.deepgram_asr import DeepgramASR
        from translation.asr.base import ASRError
        from types import SimpleNamespace
        exc=RuntimeError('private response and key');exc.response=SimpleNamespace(status_code=401)
        connector=Mock(side_effect=exc);p=DeepgramASR(connector=connector,key_loader=lambda:'private-key')
        with self.assertRaises(ASRError) as raised:await p.run()
        self.assertIn('401',str(raised.exception));self.assertNotIn('private',str(raised.exception));self.assertEqual(connector.call_count,1)

if __name__=='__main__':unittest.main()
