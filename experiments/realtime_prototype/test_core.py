"""Fault and ownership tests; all providers mocked, no paid calls or microphone."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
import numpy as np
from runtime_settings import RuntimeSettings
from tools.realtime_benchmark.common import Metrics
from experiments.realtime_prototype.core import Engine, Turn


PCM=(np.sin(np.arange(4800)*.08)*8000).astype('<i2').tobytes()


class Response:
    def __init__(self,chunks,mime='application/octet-stream'):
        self.chunks=chunks; self.headers={'content-type':mime}; self.closed=False
    async def __aenter__(self): return self
    async def __aexit__(self,*_): self.closed=True
    async def iter_bytes(self,_):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            if isinstance(chunk,Exception): raise chunk
            yield chunk


class Stream:
    def __init__(self,texts): self.texts=texts; self.closed=False
    async def __aenter__(self): return self
    async def __aexit__(self,*_): self.closed=True
    async def __aiter__(self):
        for text in self.texts:
            await asyncio.sleep(0)
            yield NS(choices=[NS(delta=NS(content=text))])


def client(response=None,texts=('你好，','第一句。','第二句。')):
    stream=Stream(texts)
    return NS(audio=NS(speech=NS(with_streaming_response=NS(create=lambda **_:response))),
              chat=NS(completions=NS(create=AsyncMock(return_value=stream))),close=AsyncMock()),stream


def turn():
    return Turn('test-reply',Metrics('B',1),replace(RuntimeSettings(),
        speech_quirk_enabled=False,tts_mode='gaming',voice_tone='soft'))


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_dc_tiny_odd_and_wrong_container_are_errors(self):
        for data,mime in [([], 'application/octet-stream'),([b'\0'*9600],'application/octet-stream'),
                ([PCM[:100]],'application/octet-stream'),([PCM+b'x'],'application/octet-stream'),
                ([b'RIFF'+PCM],'application/octet-stream'),([PCM],'audio/wav'),([b'{}'],'application/json')]:
            with self.subTest(size=sum(map(len,data)),mime=mime):
                response=Response(data,mime); api,_=client(response); t=turn()
                with self.assertRaises(ValueError): await Engine(client=api).synthesize('回答。',t,AsyncMock())
                self.assertTrue(response.closed)

    async def test_stream_valid_pcm_before_response_finishes(self):
        response=Response([PCM[:6000],PCM[6000:]]); api,_=client(response); sent=[]
        async def send(kind,**kw): sent.append((kind,response.closed,kw))
        await Engine(client=api).synthesize('回答。',turn(),send)
        self.assertEqual(len(sent),2); self.assertFalse(sent[0][1]); self.assertTrue(response.closed)

    async def test_invalid_local_uses_cloud_before_audio_is_sent(self):
        api,_=client(Response([PCM])); engine=Engine(True,api); engine._local_audio=AsyncMock(side_effect=ValueError('invalid_wav'))
        t=turn(); send=AsyncMock()
        with patch('experiments.realtime_prototype.core.choose_route',return_value=NS(backend='local',reason='idle')):
            await engine.synthesize('回答。',t,send)
        self.assertEqual(t.metrics.row['tts_backends'],['local','openai'])
        self.assertEqual(send.await_args.kwargs['backend'],'openai')

    async def test_gpu_protection_never_calls_local(self):
        api,_=client(Response([PCM])); engine=Engine(True,api); engine._local_audio=AsyncMock()
        with patch('experiments.realtime_prototype.core.choose_route',return_value=NS(backend='openai',reason='gpu_high_sustained')):
            await engine.synthesize('回答。',turn(),AsyncMock())
        engine._local_audio.assert_not_called()

    async def test_cloud_disconnect_is_explicit_failure(self):
        api,_=client(Response([PCM,ConnectionResetError('dropped')]))
        with self.assertRaises(ConnectionResetError): await Engine(client=api).synthesize('回答。',turn(),AsyncMock())

    async def test_sentence_worker_is_serial_and_flushes_last_sentence(self):
        api,_=client(texts=('开始，',*[f'第{i}句。' for i in range(20)],'结束'))
        engine=Engine(client=api); active=0; peak=0; spoken=[]; t=turn()
        async def synth(text,turn,send):
            nonlocal active,peak
            active+=1; peak=max(peak,active); await asyncio.sleep(.001)
            spoken.append(text); turn.metrics.mark('tts_first_audio'); active-=1
        engine.synthesize=synth
        t.metrics.mark('audible'); t.played.set()
        await engine.respond(t,np.zeros(0),AsyncMock(),spoken='测试')
        self.assertEqual(peak,1); self.assertEqual(len(spoken),22); self.assertEqual(spoken[-1],'结束')
        self.assertEqual(t.metrics.row['status'],'complete')

    async def test_playback_failure_cannot_be_marked_complete(self):
        api,_=client(); engine=Engine(client=api); t=turn()
        async def synth(*_): t.metrics.mark('tts_first_audio')
        engine.synthesize=synth; t.played.set(); t.metrics.mark('audible'); t.metrics.row['error']='device_failed'
        with self.assertRaisesRegex(RuntimeError,'device_failed'):
            await engine.respond(t,np.zeros(0),AsyncMock(),spoken='测试')
        self.assertNotEqual(t.metrics.row['status'],'complete')

    async def test_no_audio_ack_cannot_be_marked_complete(self):
        api,_=client(); engine=Engine(client=api); t=turn()
        async def synth(*_): t.metrics.mark('tts_first_audio')
        engine.synthesize=synth; t.played.set()
        with self.assertRaisesRegex(ValueError,'playback_never_audible'):
            await engine.respond(t,np.zeros(0),AsyncMock(),spoken='测试')

    async def test_cancel_owns_children_and_closes_llm_stream(self):
        api,stream=client(texts=['开始，']+['一段。']*30); engine=Engine(client=api); t=turn()
        started=asyncio.Event(); ended=asyncio.Event()
        async def synth(*_):
            started.set()
            try: await asyncio.Event().wait()
            finally: ended.set()
        engine.synthesize=synth
        task=asyncio.create_task(engine.respond(t,np.zeros(0),AsyncMock(),spoken='测试'))
        await asyncio.wait_for(started.wait(),2); t.cancelled.set(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(ended.is_set()); self.assertTrue(stream.closed)

    async def test_first_sentence_does_not_wait_for_llm_eof(self):
        synth_started=asyncio.Event()
        class BlockingStream(Stream):
            async def __aiter__(self):
                yield NS(choices=[NS(delta=NS(content='你好，'))])
                await asyncio.wait_for(synth_started.wait(),2)
                yield NS(choices=[NS(delta=NS(content='继续。'))])
        api,_=client(); api.chat.completions.create=AsyncMock(return_value=BlockingStream([]))
        engine=Engine(client=api); t=turn(); t.played.set(); t.metrics.mark('audible')
        async def synth(*_): synth_started.set(); t.metrics.mark('tts_first_audio')
        engine.synthesize=synth
        await engine.respond(t,np.zeros(0),AsyncMock(),spoken='测试')


if __name__=='__main__': unittest.main()
