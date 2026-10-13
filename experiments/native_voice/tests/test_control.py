import asyncio
import json
import unittest
from aiohttp.test_utils import TestClient,TestServer
from experiments.native_voice.server import create_app

class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app=create_app(9884,limit=0,control_key='test-only-control-key')
        self.client=TestClient(TestServer(self.app));await self.client.start_server();self.sockets=[]
    async def asyncTearDown(self):
        for ws in self.sockets:await ws.close()
        await self.client.close()
    async def read(self,ws,kind):
        for _ in range(20):
            data=await asyncio.wait_for(ws.receive_json(),2)
            if data.get('type')==kind:return data
        self.fail('missing '+kind)
    async def connect(self,role):
        ws=await self.client.ws_connect('/bridge',headers={'Host':'127.0.0.1:9884','Origin':'http://127.0.0.1:9884'})
        self.sockets.append(ws);await ws.send_json(dict(type='hello',role=role,key='test-only-control-key'))
        return ws,await self.read(ws,'welcome')
    async def test_single_voice_lease_and_pause_closes_admission_before_ack(self):
        voice,welcome=await self.connect('voice');other,second=await self.connect('voice')
        self.assertTrue(welcome['owner']);self.assertFalse(second['owner']);self.assertIsNone(second['lease'])
        await other.send_json(dict(type='command',id='start',command='start'))
        start=await self.read(voice,'command');hub=self.app['control_hub']
        self.assertTrue(hub.can_mint(welcome['lease'],start['generation']))
        await voice.send_json(dict(type='state',generation=start['generation'],state='RUNNING',mic_active=True,cloud_connected=True))
        await self.read(voice,'state')
        await other.send_json(dict(type='command',id='pause',command='pause'))
        pause=await self.read(voice,'command')
        self.assertFalse(hub.can_mint(welcome['lease'],start['generation']))
        self.assertEqual(hub.state['state'],'PAUSING')
        await voice.send_json(dict(type='state',generation=start['generation'],state='RUNNING',mic_active=True))
        await voice.send_json(dict(type='ping'));await self.read(voice,'pong')
        self.assertEqual(hub.state['state'],'PAUSING')
        await voice.send_json(dict(type='state',generation=pause['generation'],state='PAUSED',mic_active=False,cloud_connected=False))
        await voice.send_json(dict(type='ping'));await self.read(voice,'pong')
        self.assertEqual(hub.state['state'],'PAUSED')
    async def test_capture_owner_cannot_claim_paused_with_live_resources(self):
        voice,welcome=await self.connect('voice')
        await voice.send_json(dict(type='state',generation=welcome['generation'],state='PAUSED',mic_active=True,cloud_connected=True))
        await voice.send_json(dict(type='ping'));await self.read(voice,'pong')
        self.assertEqual(self.app['control_hub'].state['state'],'FAILED')
    async def test_authorized_cost_limit_zero_no_api(self):
        voice,welcome=await self.connect('voice');await voice.send_json(dict(type='command',id='s',command='start'))
        start=await self.read(voice,'command')
        r=await self.client.post('/token',headers={'Host':'127.0.0.1:9884','Origin':'http://127.0.0.1:9884',
            'X-Voice-Lease':welcome['lease'],'X-Voice-Generation':str(start['generation'])})
        self.assertEqual(r.status,429);self.assertEqual((await r.json())['error'],'session_limit_reached')
    async def test_foreign_origin_and_wrong_key_cannot_control(self):
        with self.assertRaises(Exception):await self.client.ws_connect('/bridge',headers={'Host':'127.0.0.1:9884','Origin':'https://evil.example'})
        ws=await self.client.ws_connect('/bridge',headers={'Host':'127.0.0.1:9884'});self.sockets.append(ws)
        await ws.send_json(dict(type='hello',role='voice',key='wrong'));await ws.receive()
        self.assertIsNone(self.app['control_hub'].owner)
    async def test_disconnect_never_claims_pause_or_restarts_capture(self):
        voice,welcome=await self.connect('voice');await voice.send_json(dict(type='command',id='s',command='start'))
        start=await self.read(voice,'command')
        await voice.send_json(dict(type='state',generation=start['generation'],state='RUNNING',mic_active=True,cloud_connected=True))
        await voice.send_json(dict(type='ping'));await self.read(voice,'pong');await voice.close();await asyncio.sleep(.01)
        hub=self.app['control_hub'];self.assertEqual(hub.state['state'],'UNKNOWN');self.assertFalse(hub.allow_requests)
        self.assertIsNone(hub.state['mic_active'])
    async def test_mic_preference_in_paused_state_does_not_reopen_token_gate(self):
        voice,welcome=await self.connect('voice');hub=self.app['control_hub']
        await voice.send_json(dict(type='state',generation=welcome['generation'],state='PAUSED',mic_active=False,cloud_connected=False))
        await voice.send_json(dict(type='ping'));await self.read(voice,'pong')
        await voice.send_json(dict(type='command',id='mic',command='mic',enabled=True))
        command=await self.read(voice,'command')
        self.assertFalse(hub.can_mint(welcome['lease'],command['generation']))
