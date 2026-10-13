import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from experiments.realtime_prototype.server import Session


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=TemporaryDirectory()
        self.folder=patch('experiments.realtime_prototype.server.HERE',Path(self.temp.name));self.folder.start()
        self.started=asyncio.Event()
        async def response(*args,**kwargs): self.started.set();await asyncio.Event().wait()
        self.ws=NS(send_json=AsyncMock());self.session=Session(self.ws,NS(respond=response))

    async def asyncTearDown(self):
        await self.session.interrupt();self.folder.stop();self.temp.cleanup()

    async def begin(self):
        await self.session.begin(dict(text='测试',audio=''));await self.started.wait();return self.session.current

    async def test_old_turn_messages_cannot_reach_new_turn(self):
        old=await self.begin();await self.session.begin(dict(text='下一句',audio=''))
        self.ws.send_json.reset_mock()
        await self.session.send(old,'pcm',pcm='stale')
        self.ws.send_json.assert_not_awaited();self.assertTrue(old.task.done())

    async def test_disconnect_cancels_owned_turn(self):
        current=await self.begin();await self.session.interrupt()
        self.assertTrue(current.cancelled.is_set());self.assertTrue(current.task.done())
        self.assertEqual(current.metrics.row['status'],'cancelled')

    async def test_playback_error_cancellation_remains_failure(self):
        current=await self.begin();current.metrics.mark('text_ready')
        current.metrics.row['error']='browser_playback_failed';current.task.cancel()
        with self.assertRaises(asyncio.CancelledError):await current.task
        self.assertEqual(current.metrics.row['status'],'failed')
        self.assertTrue(current.metrics.row['silent_reply'])


if __name__=='__main__':unittest.main()
