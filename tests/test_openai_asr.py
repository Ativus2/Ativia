"""Protocol and safety regression tests; cloud evidence is in logs/openai-*.jsonl."""
import asyncio
from dataclasses import replace
import json
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock, patch
from translation.asr.openai_asr import OpenAIASRProvider
from translation.asr.base import ASRError
from translation.asr.models import TranscriptEvent
from translation.session import PipelineSession
from live_translation import TranslationSettings


def packet(kind, item='A', **kw):
    return dict(type='conversation.item.input_audio_transcription.'+kind,item_id=item,**kw)


def ack(p,item='A',previous=None,now=1):
    p.receive(dict(type='input_audio_buffer.committed',item_id=item,previous_item_id=previous),now)


class OpenAIProtocolTests(unittest.TestCase):
    def test_language_context_and_dedicated_model(self):
        for lang in ('ko','en','ja','zh','auto'):
            p=OpenAIASRProvider(language=lang,hotwords=('Elden Ring','Malenia'),prompt='Malenia = 玛莲妮亚')
            inp=p.session_update()['session']['audio']['input']
            t=inp['transcription']
            self.assertEqual(t['model'],'gpt-live-transcribe')
            self.assertEqual(t['keywords'],['Elden Ring','Malenia'])
            self.assertEqual(t['prompt'],'Malenia = 玛莲妮亚')
            self.assertEqual(t.get('languages'),None if lang=='auto' else [lang])
            self.assertNotIn('language',t)
            self.assertIsNone(inp['turn_detection'])
            self.assertEqual(inp['format']['rate'],24000)

    def test_delta_accumulates_final_replaces_and_confidence_is_null(self):
        p=OpenAIASRProvider(language='ko');out=[]
        p.on_partial=p.on_final=out.append
        p.receive(packet('delta',delta='안녕'),1)
        p.receive(packet('delta',delta='하세요'),1.1)
        p.commits.append(1.2);ack(p,now=1.3)
        p.receive(packet('completed',transcript='안녕하세요.'),1.7)
        self.assertEqual([x.text for x in out],['안녕','안녕하세요','안녕하세요.'])
        self.assertEqual([x.is_final for x in out],[False,False,True])
        self.assertEqual(out[-1].asr_latency_ms,500)
        self.assertTrue(all(x.confidence is None for x in out))
        self.assertTrue(all(x.audio_start is None and x.audio_end is None for x in out))
        self.assertTrue(all(x.language=='ko' for x in out))

    def test_out_of_order_finals_keep_order_without_holding_next_partial(self):
        p=OpenAIASRProvider();out=[]
        p.on_partial=p.on_final=out.append
        p.receive(packet('delta',delta='First'))
        ack(p)
        p.receive(packet('delta','B',delta='Second'))
        ack(p,'B','A')
        p.receive(packet('completed','B',transcript='Second.'))
        self.assertEqual([x.text for x in out],['First','Second'])
        p.receive(packet('completed',transcript='First.'))
        self.assertEqual([x.text for x in out],['First','Second','First.','Second.'])

    def test_empty_earlier_turn_reorders_by_commit_chain(self):
        p=OpenAIASRProvider();out=[];p.on_final=out.append
        ack(p)
        p.receive(packet('delta','B',delta='Second'))
        ack(p,'B','A')
        p.receive(packet('completed','B',transcript='Second.'))
        p.receive(packet('completed',transcript=''))
        self.assertEqual([x.text for x in out],['Second.'])

    def test_duplicate_completed_and_late_delta_are_ignored(self):
        p=OpenAIASRProvider();out=[];p.on_final=p.on_partial=out.append
        ack(p);p.receive(packet('completed',transcript='Done.'))
        p.receive(packet('completed',transcript='Done.'))
        p.receive(packet('delta',delta='Old'))
        self.assertEqual(len(out),1)

    def test_server_errors_never_echo_keys_or_response(self):
        p=OpenAIASRProvider()
        for typ in ('error','conversation.item.input_audio_transcription.failed'):
            with self.assertRaises(ASRError) as cm:
                p.receive(dict(type=typ,error=dict(message='sk-private-secret',code='sk-private-secret')))
            self.assertNotIn('secret',str(cm.exception))

    def test_invalid_keywords_are_rejected_without_echo(self):
        with self.assertRaises(ASRError) as cm:
            OpenAIASRProvider(hotwords=('private<secret>',)).session_update()
        self.assertNotIn('private',str(cm.exception))

    def test_partials_do_not_become_stable_after_wait(self):
        p=OpenAIASRProvider();events=[];settings=TranslationSettings()
        session=PipelineSession(p,settings,None,diagnostic=events.append)
        p.receive(packet('delta',delta='I do not think'),10)
        session.accept(session.stabilizer.tick(monotonic()+100))
        self.assertFalse(any(x['stage']=='stable_final' for x in events))
        ack(p);p.receive(packet('completed',transcript='I do not think this is bad.'),11)
        session.accept(session.stabilizer.tick(monotonic()+1))
        self.assertEqual([x['text'] for x in events if x['stage']=='stable_final'],['I do not think this is bad.'])

    def test_next_partial_cannot_starve_completed_openai_item(self):
        p=OpenAIASRProvider();events=[]
        session=PipelineSession(p,TranslationSettings(),None,diagnostic=events.append)
        with patch('translation.session.monotonic',return_value=10):
            ack(p);p.receive(packet('completed',transcript='First without punctuation'))
        with patch('translation.session.monotonic',return_value=10.1):
            p.receive(packet('delta','B',delta='Next sentence'))
        session.tick(10.64)
        self.assertFalse(any(e['stage']=='stable_final' for e in events))
        session.tick(10.66)
        self.assertEqual([e['text'] for e in events if e['stage']=='stable_final'],['First without punctuation'])
        self.assertEqual(session.stabilizer.pending.row.source,'Next sentence')

    def test_adjacent_finals_still_merge_inside_650ms(self):
        p=OpenAIASRProvider();events=[]
        session=PipelineSession(p,TranslationSettings(),None,diagnostic=events.append)
        with patch('translation.session.monotonic',return_value=10):
            ack(p);p.receive(packet('completed',transcript='I do not'))
        with patch('translation.session.monotonic',return_value=10.1):
            p.receive(packet('delta','B',delta='think'))
        with patch('translation.session.monotonic',return_value=10.3):
            ack(p,'B','A');p.receive(packet('completed','B',transcript='think this is bad.'))
        session.tick(10.94)
        self.assertFalse(any(e['stage']=='stable_final' for e in events))
        session.tick(10.96)
        self.assertEqual([e['text'] for e in events if e['stage']=='stable_final'],['I do not think this is bad.'])

    def test_reset_clears_immediately_displayed_partial(self):
        p=OpenAIASRProvider();s=PipelineSession(p,TranslationSettings(),None)
        ack(p);p.receive(packet('completed',transcript='First'))
        p.receive(packet('delta','B',delta='Old'))
        self.assertEqual(s.stabilizer.pending.row.source,'Old')
        p.epoch+=1;s._reset(p.epoch);s.tick(monotonic()+1)
        self.assertFalse(s.stabilizer.records)

    def test_stale_epoch_rejected_before_diagnostics(self):
        p=OpenAIASRProvider();p.epoch=2000;out=[]
        s=PipelineSession(p,TranslationSettings(),None,diagnostic=out.append)
        s._raw(TranscriptEvent('Old',True,None,'en',1,'windows','x',epoch=1000))
        self.assertEqual(out,[]);self.assertEqual(s.stabilizer.epoch,2000)

    def test_deepgram_unavailability_never_touched_by_other_providers(self):
        from translation.asr import make_provider
        with patch('translation.asr.deepgram_asr.load_api_key',side_effect=AssertionError('must not run')):
            self.assertEqual(make_provider(TranslationSettings(backend='openai')).name,'openai')
            self.assertEqual(make_provider(TranslationSettings(backend='windows')).name,'windows')


class OpenAILifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_is_isolated_and_does_not_connect(self):
        connector=Mock();p=OpenAIASRProvider(connector=connector,key_loader=lambda:'')
        with self.assertRaises(ASRError): await p.run()
        connector.assert_not_called()

    async def test_http_401_is_not_retried_and_is_safe(self):
        exc=RuntimeError('sk-private-key');exc.response=SimpleNamespace(status_code=401)
        connector=Mock(side_effect=exc)
        p=OpenAIASRProvider(connector=connector,key_loader=lambda:'sk-private-key')
        with self.assertRaises(ASRError) as cm: await p.run()
        self.assertIn('401',str(cm.exception));self.assertNotIn('sk-private',str(cm.exception))
        self.assertEqual(connector.call_count,1)

    async def test_network_retries_bounded_and_clear_partial(self):
        p=OpenAIASRProvider(connector=Mock(side_effect=OSError('private-request')),
            key_loader=lambda:'private-key',retry_delays=(.001,.001))
        epochs=[];p.on_reset=epochs.append
        with self.assertRaises(ASRError) as cm: await p.run()
        self.assertEqual(epochs,[1,2,3]);self.assertFalse(p.turns)
        self.assertNotIn('private',str(cm.exception))

    async def test_stop_interrupts_backoff_and_clears_buffer(self):
        p=OpenAIASRProvider(connector=Mock(side_effect=OSError()),
            key_loader=lambda:'test',retry_delays=(30,))
        await p.start();await asyncio.sleep(.01)
        await asyncio.wait_for(p.stop(),.5)
        self.assertTrue(p.task.done());self.assertFalse(p.ready.is_set());self.assertFalse(p.turns)

    async def test_pcm_resampling_is_stateful_and_24khz(self):
        import base64
        p=OpenAIASRProvider(sample_rate=44100);p.ws=SimpleNamespace(send=AsyncMock())
        p.ready.set()
        sender=asyncio.create_task(p._sender())
        for _ in range(10): self.assertTrue(await p.push_audio(b'\0'*8820))
        await p.send_queue.join()
        if p.wire_tail:await p._process_wire(p.wire_tail,monotonic());p.wire_tail=b''
        await p._flush_until(p.controller.cursor)
        sender.cancel();await asyncio.gather(sender,return_exceptions=True)
        packets=[json.loads(c.args[0]) for c in p.ws.send.call_args_list]
        size=sum(len(base64.b64decode(d['audio'])) for d in packets if d['type']=='input_audio_buffer.append')
        self.assertLessEqual(abs(size-48000),2)

    async def test_commit_ack_race_and_finalize_waits_for_real_completed(self):
        p=OpenAIASRProvider();p.ready.set();p.ws=SimpleNamespace(send=AsyncMock())
        p.buffer_samples=2400
        async def send(data):
            if json.loads(data)['type']=='input_audio_buffer.commit':
                ack(p)
                p.receive(packet('completed',transcript='Done.'))
        p.ws.send.side_effect=send
        sender=asyncio.create_task(p._sender())
        try:await asyncio.wait_for(p.finalize(),.5)
        finally:sender.cancel();await asyncio.gather(sender,return_exceptions=True)
        self.assertFalse(p.commits)

    async def test_partial_never_calls_translation_until_final(self):
        p=OpenAIASRProvider()
        translate=AsyncMock(return_value='你好')
        session=PipelineSession(p,TranslationSettings(),SimpleNamespace(translate=translate))
        running=asyncio.create_task(session.scheduler.run())
        try:
            p.receive(packet('delta',delta='Hello'))
            session.accept(session.stabilizer.tick(monotonic()+2))
            await asyncio.sleep(.02)
            translate.assert_not_awaited()
            ack(p);p.receive(packet('completed',transcript='Hello.'))
            session.accept(session.stabilizer.tick(monotonic()+1))
            for _ in range(30):
                if translate.await_count:break
                await asyncio.sleep(.02)
            translate.assert_awaited_once()
        finally:
            session.scheduler.close();running.cancel()
            await asyncio.gather(running,return_exceptions=True)

if __name__=='__main__':unittest.main()
