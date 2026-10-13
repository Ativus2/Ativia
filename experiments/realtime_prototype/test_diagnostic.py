import asyncio,base64
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
import numpy as np
from experiments.realtime_prototype.realtime_stt import RealtimeTranscription
from experiments.realtime_prototype.core import Engine
from experiments.realtime_prototype.server import Session
from experiments.realtime_prototype.test_core import client,Response,PCM,turn

class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_cloud_does_not_even_check_gpu(self):
        api,_=client(Response([PCM]));engine=Engine(False,api)
        with patch('experiments.realtime_prototype.core.choose_route',side_effect=AssertionError('GPU route used')):
            await engine.synthesize('有用答案。',turn(),AsyncMock())

    async def test_admission_once_and_failure_sticks_without_replaying(self):
        api,_=client(Response([PCM]));engine=Engine(True,api);t=turn();send=AsyncMock()
        engine._local_audio=AsyncMock(side_effect=[(PCM,24000,1),ValueError('broken')])
        with patch('experiments.realtime_prototype.core.choose_route',return_value=NS(backend='local',reason='idle')) as route:
            for text in ['第一句。','第二句。','第三句。']:await engine.synthesize(text,t,send)
        self.assertEqual(route.call_count,1);self.assertEqual(engine._local_audio.await_count,2)
        self.assertEqual([c.kwargs['segment'] for c in send.await_args_list],[1,2,3])
        self.assertEqual([c.kwargs['backend'] for c in send.await_args_list],['local','openai','openai'])

    async def test_placeholder_pcm_not_first_meaningful(self):
        api,_=client(Response([PCM]));send=AsyncMock();engine=Engine(False,api);t=turn()
        await engine.synthesize('嗯。',t,send);self.assertFalse(send.await_args.kwargs['meaningful'])
        await engine.synthesize('缓存暂存常用数据。',t,send);self.assertTrue(send.await_args.kwargs['meaningful'])

    async def test_final_must_match_commit_item_and_not_delta(self):
        stt=RealtimeTranscription(NS(),{})
        stt.completed={'old':dict(transcript='过期'),'new':dict(transcript='正确',usage={'type':'duration','seconds':1})}
        stt.resolve();self.assertFalse(stt.final.done())
        stt.item_id='new';stt.resolve();self.assertEqual(await stt.final,'正确')
        await stt.close()

    async def test_sender_failure_does_not_hang_queue_drain(self):
        stt=RealtimeTranscription(NS(),{});stt.ready.set()
        stt.ws=NS(send_json=AsyncMock(side_effect=ConnectionResetError('test')),close=AsyncMock())
        stt.append(np.zeros(512));stt.append(np.zeros(512));stt.sender=asyncio.create_task(stt.send_audio())
        with self.assertRaises(ConnectionResetError):await asyncio.wait_for(stt.finish(),.5)
        await stt.close()

    async def test_duplicate_commit_and_post_commit_audio_rejected(self):
        stt=RealtimeTranscription(NS(),{});stt.committed=True
        with self.assertRaises(RuntimeError):await stt.finish()
        with self.assertRaises(RuntimeError):stt.append(np.zeros(512))
        await stt.close()

    async def test_only_final_starts_one_llm_and_cancel_owns_provider(self):
        with TemporaryDirectory() as temp,patch('experiments.realtime_prototype.server.HERE',Path(temp)):
            final=asyncio.Event()
            class FakeSTT:
                def __init__(self,*args):self.closed=False
                async def open(self):pass
                def append(self,frame):pass
                async def finish(self):await final.wait();return '最终文字'
                async def close(self):self.closed=True
            api=NS(client=NS(),respond=AsyncMock(return_value=('最终文字','回答')))
            session=Session(NS(send_json=AsyncMock()),api)
            with patch('experiments.realtime_prototype.server.RealtimeTranscription',FakeSTT):
                await session.input_start(dict(input_id='one'));provider=session.capture['provider']
                session.input_append(dict(input_id='one',audio=base64.b64encode(np.zeros(512,dtype='<f4').tobytes()).decode()))
                api.respond.assert_not_awaited()
                await session.begin(dict(stt='realtime',input_id='one',audio=''))
                await asyncio.sleep(0);api.respond.assert_not_awaited()
                final.set();await session.current.task;api.respond.assert_awaited_once()
                self.assertEqual(api.respond.await_args.kwargs['spoken'],'最终文字');self.assertTrue(provider.closed)
                with self.assertRaises(ValueError):await session.begin(dict(stt='realtime',input_id='one'))
                await session.input_start(dict(input_id='two'));provider=session.capture['provider']
                await session.interrupt();self.assertTrue(provider.closed)

if __name__=='__main__':unittest.main()
