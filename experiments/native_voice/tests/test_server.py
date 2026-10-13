import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient,TestServer
from server import create_app,MODEL

class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=TestClient(TestServer(create_app(9884,limit=0)))
        await self.client.start_server()
    async def asyncTearDown(self):await self.client.close()
    async def test_config_contains_public_config_only(self):
        response=await self.client.get('/config',headers={'Host':'127.0.0.1:9884'})
        self.assertEqual(response.status,200);body=await response.json()
        self.assertEqual(body['model'],MODEL);self.assertNotIn('api_key',body)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
    async def test_cross_origin_and_host_rejected(self):
        r=await self.client.post('/token',headers={'Host':'127.0.0.1:9884','Origin':'https://example.com'})
        self.assertEqual(r.status,403)
        r=await self.client.get('/config',headers={'Host':'example.com'})
        self.assertEqual(r.status,403)
    async def test_limit_does_not_call_api(self):
        r=await self.client.post('/token',headers={'Host':'127.0.0.1:9884','Origin':'http://127.0.0.1:9884'})
        self.assertEqual(r.status,409) # No voice lease: rejected even before checking the cost budget.
    async def test_no_arbitrary_file_or_credential_serving(self):
        for path in ('/.env','/logs/broker.jsonl','/server.py'):
            r=await self.client.get(path,headers={'Host':'127.0.0.1:9884'})
            self.assertEqual(r.status,404)
