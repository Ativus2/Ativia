"""Small authenticated localhost command/state bridge, one capture owner only."""
import asyncio
import secrets
from time import monotonic
from aiohttp import web, WSMsgType

STATES={'IDLE','CONNECTING','RESUMING','RUNNING','PAUSING','PAUSED','FAILED','ENDED'}
COMMANDS={'start','pause','resume','interrupt','end','mic','focus','quit'}


class ControlHub:
    def __init__(self, key=None):
        self.key=key or secrets.token_urlsafe(32)
        self.instance=secrets.token_hex(4)
        self.clients=set();self.owner=None;self.lease=None
        self.generation=0;self.allow_requests=False;self.pending={}
        self.state=dict(state='IDLE',mic_active=False,mic_enabled=True,thinking=False,
                        speaking=False,caption='',error='',cloud_connected=False,page_connected=False)

    def snapshot(self):
        return dict(type='state',instance=self.instance,generation=self.generation,
                    mode='native-voice',**self.state)

    async def broadcast(self):
        data=self.snapshot()
        for ws in tuple(self.clients):
            if not ws.closed:
                try:await ws.send_json(data)
                except (ConnectionError,RuntimeError):pass

    def can_mint(self, lease, generation):
        return self.owner is not None and not self.owner.closed and self.allow_requests and \
            secrets.compare_digest(str(lease),str(self.lease)) and generation==self.generation

    async def command(self, ws, data):
        command=data.get('command');ident=str(data.get('id',''))[:80]
        if command not in COMMANDS or not ident:
            await ws.send_json(dict(type='ack',id=ident,phase='rejected',error='invalid_command'));return
        if command=='quit':
            self.allow_requests=False
            for client in tuple(self.clients):
                if client is not self.owner and not client.closed:await client.send_json(dict(type='shutdown_requested'))
            await ws.send_json(dict(type='ack',id=ident,phase='accepted'));return
        if command=='focus':
            for client in tuple(self.clients):
                if client is not self.owner and not client.closed:await client.send_json(dict(type='open_panel_requested'))
        if command=='mic' and not isinstance(data.get('enabled'),bool):
            await ws.send_json(dict(type='ack',id=ident,phase='rejected',error='invalid_mic_value'));return
        if command in {'start','resume'} and self.state['state'] not in {'IDLE','ENDED','PAUSED'}:
            await ws.send_json(dict(type='ack',id=ident,phase='rejected',error='not_ready_to_start'));return
        if command=='interrupt' and self.state['state']!='RUNNING':
            await ws.send_json(dict(type='ack',id=ident,phase='rejected',error='not_running'));return
        if command in {'start','resume','pause','end','mic'}:
            self.generation+=1
            if command=='mic':self.state['mic_enabled']=data['enabled']
            self.allow_requests=(command in {'start','resume'} and self.state['mic_enabled']) or \
                (command=='mic' and data['enabled'] and self.state['state']=='RUNNING')
        if command in {'pause','end'}:
            self.state.update(state='PAUSING',caption='',thinking=False,speaking=False)
        if self.owner is None or self.owner.closed:
            self.allow_requests=False
            if command in {'pause','end'} and self.state.get('mic_active') is False:
                self.state.update(state='PAUSED' if command=='pause' else 'ENDED',cloud_connected=False)
                await ws.send_json(dict(type='ack',id=ident,phase='completed'))
            else:
                self.state['error']='请打开语音页；首次收音需在页面授权。' if command in {'start','resume','focus'} else '语音页失联，不能确认资源已停止。'
                if self.state.get('mic_active') is None:self.state['state']='UNKNOWN'
                await ws.send_json(dict(type='ack',id=ident,phase='rejected',error='voice_page_required'))
            await self.broadcast();return
        self.pending[ident]=(ws,self.owner,self.generation,monotonic())
        await ws.send_json(dict(type='ack',id=ident,phase='accepted'))
        await self.owner.send_json(dict(type='command',id=ident,command=command,
                enabled=data.get('enabled'),generation=self.generation))
        await self.broadcast()

    async def websocket(self, request):
        ws=web.WebSocketResponse(heartbeat=2.5,max_msg_size=16384)
        await ws.prepare(request)
        try:
            hello=await asyncio.wait_for(ws.receive_json(),3)
            if not secrets.compare_digest(str(hello.get('key','')),self.key):
                await ws.close(code=1008,message=b'access denied');return ws
            self.clients.add(ws)
            owner=hello.get('role')=='voice' and self.owner is None
            if owner:
                self.owner=ws;self.lease=secrets.token_urlsafe(24)
                self.allow_requests=False;self.generation+=1
                self.state.update(state='IDLE',page_connected=True,mic_active=False,
                                  cloud_connected=False,thinking=False,speaking=False,caption='',error='')
            await ws.send_json(dict(type='welcome',owner=owner,lease=self.lease if owner else None,
                                    instance=self.instance,generation=self.generation))
            await self.broadcast()
            async for message in ws:
                if message.type!=WSMsgType.TEXT:continue
                try:data=message.json()
                except (ValueError,TypeError):await ws.close(code=1008);break
                kind=data.get('type')
                if kind=='ping':await ws.send_json(dict(type='pong'))
                elif kind=='command':await self.command(ws,data)
                elif kind=='state' and ws is self.owner:
                    if data.get('generation')!=self.generation:continue
                    incoming=data.get('state')
                    if incoming not in STATES:continue
                    if incoming=='RUNNING' and not self.allow_requests and self.state['state']=='PAUSING':
                        # A queued update from before pause cannot reopen capture admission.
                        continue
                    for field in ('mic_active','mic_enabled','thinking','speaking','cloud_connected'):
                        if isinstance(data.get(field),bool):self.state[field]=data[field]
                    self.state.update(state=incoming,error=str(data.get('error',''))[:200])
                    if incoming in {'PAUSED','ENDED'}:
                        if self.state['mic_active'] or self.state['cloud_connected']:
                            self.state.update(state='FAILED',error='资源尚未停止，拒绝报告完全暂停。')
                        self.allow_requests=False
                    if incoming=='FAILED':self.allow_requests=False
                    self.state['caption']=str(data.get('caption',''))[:4000] if incoming=='RUNNING' else ''
                    await self.broadcast()
                elif kind=='ack' and ws is self.owner:
                    item=self.pending.pop(data.get('id'),None)
                    if item and item[1] is ws and not item[0].closed:
                        await item[0].send_json(dict(type='ack',id=data['id'],phase=data.get('phase','completed'),
                            error=str(data.get('error',''))[:200]))
                now=monotonic()
                for ident,(sender,_,_,started) in tuple(self.pending.items()):
                    if now-started>12:
                        self.pending.pop(ident,None)
                        if not sender.closed:await sender.send_json(dict(type='ack',id=ident,phase='timeout',error='操作未确认，请查看真实状态。'))
        except (asyncio.TimeoutError,ValueError,ConnectionError):pass
        finally:
            self.clients.discard(ws)
            if self.owner is ws:
                self.owner=None;self.lease=None;self.allow_requests=False;self.generation+=1
                stopped=self.state['state'] in {'PAUSED','ENDED','IDLE'} and self.state['mic_active'] is False
                self.state.update(page_connected=False,caption='',thinking=False,speaking=False,
                    state=self.state['state'] if stopped else 'UNKNOWN',
                    mic_active=False if stopped else None,
                    error='' if stopped else '语音页失联，资源状态未知；不会自动恢复收音。')
                await self.broadcast()
            for ident,item in tuple(self.pending.items()):
                if item[0] is ws or item[1] is ws:self.pending.pop(ident,None)
            await ws.close()
        return ws

    async def close(self, app):
        self.allow_requests=False
        for ws in tuple(self.clients):await ws.close(code=1001,message=b'local service stopping')
