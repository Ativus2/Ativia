"""Authenticated request to the one owned launcher; never searches/kills unrelated processes."""
import asyncio,json
from pathlib import Path
from aiohttp import ClientSession

async def main():
    path=Path(__file__).resolve().parent/'logs/runtime.json'
    if not path.exists():print('桌宠试验品未运行。');return
    runtime=json.loads(path.read_text(encoding='utf-8'));port=runtime['port']
    async with ClientSession() as client:
        async with client.ws_connect(f'http://127.0.0.1:{port}/bridge') as ws:
            await ws.send_json(dict(type='hello',role='control',key=runtime['key']))
            await ws.receive_json()
            await ws.send_json(dict(type='command',command='quit',id='stop-script'))
            for _ in range(100):
                if not path.exists():print('本次助手已退出，拥有的服务和会话已清理。');return
                await asyncio.sleep(.1)
            raise RuntimeError('退出未确认，请通过托盘查看状态。')
if __name__=='__main__':asyncio.run(main())
