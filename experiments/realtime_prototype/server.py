"""Loopback-only minimal voice session. Static assets never leave localhost."""
import argparse
import asyncio
import base64
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from aiohttp import web
import numpy as np
from runtime_settings import SettingsStore,RuntimeSettings
from gpu_monitor import get_monitor
from tools.realtime_benchmark.common import Metrics
from experiments.realtime_prototype.core import Engine,Turn
from experiments.realtime_prototype.realtime_stt import RealtimeTranscription

HERE=Path(__file__).parent


class Session:
    def __init__(self,ws,engine):
        self.ws=ws; self.engine=engine; self.current=None; self.history=[]; self.offset=0
        self.capture=None

    @staticmethod
    def decode_audio(encoded):
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)>16000*4*30 or len(raw)%4:raise ValueError('invalid_audio_input_length')
        samples=np.frombuffer(raw,dtype='<f4').copy()
        if samples.size and (not np.isfinite(samples).all() or np.max(np.abs(samples))>1.01):raise ValueError('invalid_audio_input_values')
        return samples

    async def close_capture(self):
        capture,self.capture=self.capture,None
        if capture:
            capture['task'].cancel()
            try:await capture['task']
            except (asyncio.CancelledError,Exception):pass
            await capture['provider'].close()

    async def input_start(self,data):
        await self.interrupt()
        ident=str(data['input_id'])[:64]
        record=dict(kind='stt_realtime',input_id=ident,started_at=monotonic())
        async def delta(text):
            if self.capture and self.capture['id']==ident:
                await self.ws.send_json(dict(type='transcript_partial',input_id=ident,text=text))
        provider=RealtimeTranscription(self.engine.client,record,delta)
        task=asyncio.create_task(provider.open())
        # Ownership remains with this session; consume failures even before commit.
        task.add_done_callback(lambda t:None if t.cancelled() else t.exception())
        self.capture=dict(id=ident,record=record,provider=provider,task=task)

    def input_append(self,data):
        if not self.capture or self.capture['id']!=data['input_id']:raise ValueError('unknown_input_id')
        self.capture['provider'].append(self.decode_audio(data['audio']))

    async def send(self,turn,kind,**fields):
        if self.current is turn and not turn.cancelled.is_set():
            await self.ws.send_json(dict(type=kind,turn_id=turn.id,**fields))

    async def interrupt(self):
        await self.close_capture()
        turn=self.current
        if turn and turn.task and not turn.task.done():
            turn.cancelled.set(); turn.task.cancel()
            try: await turn.task
            except asyncio.CancelledError: pass

    async def begin(self,data):
        capture=None
        if data.get('stt')=='realtime':
            if not self.capture or self.capture['id']!=data.get('input_id'):raise ValueError('missing_realtime_capture')
            capture,self.capture=self.capture,None
        await self.interrupt()
        settings=SettingsStore().load()
        if data.get('mode') in ('auto','gaming','openai','local'):
            settings=replace(settings,tts_mode=data['mode'])
        if getattr(self.engine,'measured',None):
            settings=replace(settings,tts_mode='openai',voice_name='nova',voice_tone='soft',voice_speed=1.1,
                             speech_quirk_enabled=False)
        metrics=Metrics('B',int(data.get('index',0)))
        metrics.mark('speech_end',at=float(data.get('speech_end',monotonic()-self.offset))+self.offset)
        metrics.mark('vad_final')
        turn=Turn(uuid4().hex,metrics,settings); self.current=turn
        metrics.row['reply_id']=turn.id
        metrics.row.update(stt_adapter='realtime' if capture else 'file',input_timing=data.get('input_timing'),
                           audio_environment=data.get('audio_environment','external_speaker_human_unverified'))
        samples=self.decode_audio(data.get('audio',''))
        await self.send(turn,'turn_started',volume=.12 if data.get('parity') else settings.voice_volume)
        async def run():
            try:
                spoken=data.get('text')
                if capture:
                    record=capture['record'];metrics.row.setdefault('api_calls',[]).append(record)
                    metrics.mark('stt_started',at=record['started_at'])
                    try:
                        await capture['task'];spoken=await capture['provider'].finish()
                        record['status']='complete'
                        metrics.mark('stt_final');metrics.row['transcript']=spoken
                        await self.send(turn,'transcript',text=spoken)
                    except asyncio.CancelledError:
                        record['status']='cancelled';raise
                    except Exception as exc:
                        record.update(status='failed',error_type=type(exc).__name__);raise
                spoken,reply=await self.engine.respond(turn,samples,lambda kind,**kw:self.send(turn,kind,**kw),
                    parity=bool(data.get('parity',False)),history=self.history,spoken=spoken)
                self.history.extend([dict(role='user',content=spoken),dict(role='assistant',content=reply)])
                self.history=self.history[-6:]
            except asyncio.CancelledError:
                metrics.row['status']='failed' if metrics.row.get('error') else 'cancelled'
                metrics.mark('cancelled',reason=metrics.row.get('error') or 'user_or_disconnect')
                raise
            except Exception as exc:
                metrics.row.update(status='failed',error=type(exc).__name__+':'+str(exc))
                await self.send(turn,'error',message=metrics.row['error'])
            finally:
                if capture:await capture['provider'].close()
                row=metrics.finalize()
                row['first_meaningful_audible_ms']=((metrics.times['meaningful_audible']-metrics.times['speech_end'])*1000
                    if 'meaningful_audible' in metrics.times else None)
                row['tts_backend']='+'.join(dict.fromkeys(row.get('tts_backends',[])))
                logs=HERE/'logs'; logs.mkdir(exist_ok=True)
                with (logs/'turns.jsonl').open('a',encoding='utf-8') as log: log.write(json.dumps(row,ensure_ascii=False)+'\n')
                await self.send(turn,'result',result=row)
        turn.task=asyncio.create_task(run())


async def websocket(request):
    if request.headers.get('Origin') not in (f'http://127.0.0.1:{request.app["port"]}',f'http://localhost:{request.app["port"]}'):
        raise web.HTTPForbidden(text='Local browser origin required')
    ws=web.WebSocketResponse(max_msg_size=3*1024*1024,heartbeat=20)
    await ws.prepare(request)
    session=Session(ws,request.app['engine'])
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT: continue
            try:
                data=json.loads(msg.data); kind=data['type']
                if kind=='clock':
                    await ws.send_json(dict(type='clock',echo=data['at'],server=monotonic()))
                elif kind=='clock_offset': session.offset=float(data['offset'])
                elif kind=='input_start':await session.input_start(data)
                elif kind=='input_append':session.input_append(data)
                elif kind=='input_cancel':await session.close_capture()
                elif kind=='speech': await session.begin(data)
                elif kind=='interrupt': await session.interrupt()
                elif kind in ('audible','meaningful_audible','segment_audible','playback_done','playback_failed'):
                    turn=session.current
                    if not turn or data.get('turn_id')!=turn.id or turn.cancelled.is_set(): continue
                    if kind in ('audible','meaningful_audible','segment_audible'):
                        turn.metrics.mark(kind,at=float(data['at'])+session.offset,evidence=data.get('evidence'),segment=data.get('segment'))
                    elif kind=='playback_done': turn.metrics.mark('playback_finished',evidence=data.get('evidence')); turn.played.set()
                    else:
                        turn.metrics.row.update(status='failed',error='browser_playback_failed')
                        turn.played.set()
                        if turn.task and not turn.task.done(): turn.task.cancel()
            except Exception as exc:
                await ws.send_json(dict(type='error',message=type(exc).__name__+':'+str(exc)))
    finally: await session.interrupt()
    return ws


async def lifespan(app):
    if app['diagnostic']:
        import platform
        start=monotonic();await asyncio.to_thread(platform.platform)
        app['startup_platform_ms']=(monotonic()-start)*1000
    get_monitor(); app['engine']=Engine(allow_local=app['allow_local'],diagnostic=app['diagnostic'],
                                     reasoning='none' if app['diagnostic'] else None)
    yield
    await app['engine'].close(); get_monitor().stop()


def create_app(allow_local=False,diagnostic=False,port=9882):
    app=web.Application(client_max_size=3*1024*1024)
    app['allow_local']=allow_local and not diagnostic;app['diagnostic']=diagnostic;app['port']=port; app.cleanup_ctx.append(lifespan)
    app.router.add_get('/ws',websocket)
    async def index(_): return web.FileResponse(HERE/'web/index.html')
    async def health(_): return web.json_response(dict(status='ready',local_voice=app['allow_local'],model=os.getenv('OPENAI_MODEL'),
        diagnostic=diagnostic,reasoning='none' if diagnostic else None,startup_platform_ms=app.get('startup_platform_ms')))
    app.router.add_get('/',index); app.router.add_get('/health',health)
    app.router.add_static('/web',HERE/'web',show_index=False)
    app.router.add_static('/vad',HERE/'node_modules/@ricky0123/vad-web/dist',show_index=False)
    app.router.add_static('/ort',HERE/'node_modules/onnxruntime-web/dist',show_index=False)
    app.router.add_static('/fixtures',ROOT/'docs/realtime-benchmark/fixtures',show_index=False)
    async def feedback(request):
        if request.headers.get('Origin')!=f'http://{request.host}':raise web.HTTPForbidden()
        data=await request.json()
        if len(json.dumps(data))>10000:raise web.HTTPRequestEntityTooLarge(max_size=10000,actual_size=len(json.dumps(data)))
        logs=HERE/'logs';logs.mkdir(exist_ok=True)
        with (logs/'human-feedback.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(dict(at=monotonic(),feedback=data),ensure_ascii=False)+'\n')
        return web.json_response(dict(saved=True))
    app.router.add_post('/feedback',feedback)
    return app


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--enable-local',action='store_true')
    parser.add_argument('--diagnostic',action='store_true');parser.add_argument('--port',type=int,default=9882);args=parser.parse_args()
    web.run_app(create_app(args.enable_local,args.diagnostic,args.port),host='127.0.0.1',port=args.port,access_log=None)
