"""Loopback-only ephemeral credential broker. No audio or conversation proxy."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from aiohttp import web, ClientSession, ClientTimeout
from vision import ENTERTAINMENT_PROMPT
from experiments.native_voice.control import ControlHub

MODEL = 'gpt-realtime-2.1-mini'
VOICE = 'marin'
load_dotenv(ROOT / '.env')


def write_local(name, value):
    (HERE / 'logs').mkdir(exist_ok=True)
    with (HERE / 'logs' / name).open('a', encoding='utf-8') as f:
        f.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), **value}, ensure_ascii=False)+'\n')


def create_app(port=9884, limit=3, control_key=None):
    origin = f'http://127.0.0.1:{port}'
    lock = asyncio.Lock()
    attempts = 0
    hub=ControlHub(control_key or os.getenv('ATIVIA_CONTROL_KEY'))

    @web.middleware
    async def local_only(request, handler):
        if request.host != f'127.0.0.1:{port}' or request.remote not in ('127.0.0.1', '::1'):
            raise web.HTTPForbidden(text='localhost only')
        if request.method == 'POST' and request.headers.get('Origin') != origin:
            raise web.HTTPForbidden(text='same origin required')
        if request.path=='/bridge' and request.headers.get('Origin') not in (None,origin):
            raise web.HTTPForbidden(text='same origin required')
        response = await handler(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://api.openai.com; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
        return response

    app = web.Application(middlewares=[local_only], client_max_size=8192)
    app['control_hub']=hub
    app.on_shutdown.append(hub.close)

    async def config(request):
        return web.json_response(dict(model=MODEL, voice=VOICE, instructions=ENTERTAINMENT_PROMPT,
            persona_sha256=hashlib.sha256(ENTERTAINMENT_PROMPT.encode()).hexdigest(), limit=limit,
            control_key=hub.key,instance=hub.instance))

    async def token(request):
        nonlocal attempts
        lease=request.headers.get('X-Voice-Lease','')
        try:generation=int(request.headers.get('X-Voice-Generation','-1'))
        except ValueError:raise web.HTTPForbidden(text='invalid generation')
        if not hub.can_mint(lease,generation):
            return web.json_response({'error':'capture_admission_closed'},status=409)
        async with lock:
            if not hub.can_mint(lease,generation):
                return web.json_response({'error':'capture_admission_closed'},status=409)
            if attempts >= limit:
                return web.json_response({'error':'session_limit_reached'}, status=429)
            if not os.getenv('OPENAI_API_KEY'):
                return web.json_response({'error':'missing_server_api_key'}, status=503)
            attempts += 1  # Failed attempts also consume this finite smoke-test budget.
            record = dict(attempt=attempts, endpoint_host='api.openai.com', model=MODEL, voice=VOICE,
                          retries=0, kind='client_secret', status='started')
            try:
                async with ClientSession(timeout=ClientTimeout(total=20), trust_env=True) as client:
                    async with client.post('https://api.openai.com/v1/realtime/client_secrets',
                        headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']},
                        json={'expires_after':{'anchor':'created_at','seconds':60}, 'session':{
                            'type':'realtime', 'model':MODEL, 'instructions':ENTERTAINMENT_PROMPT,
                            'output_modalities':['audio'], 'max_output_tokens':1024,
                            'audio':{'input':{'transcription':None,
                                'turn_detection':{'type':'server_vad','threshold':0.5,
                                    'prefix_padding_ms':300,'silence_duration_ms':500,
                                    'create_response':True,'interrupt_response':True}},
                                'output':{'voice':VOICE}}, 'tracing':None}}) as upstream:
                        data = await upstream.json()
                        record.update(status=upstream.status, request_id=upstream.headers.get('x-request-id'))
                        if upstream.status != 200 or not isinstance(data.get('value'), str):
                            code = data.get('error',{}).get('code','invalid_client_secret_response')
                            record['error_code'] = code
                            return web.json_response({'error':code,'http_status':upstream.status}, status=502)
                        record['actual_model'] = data.get('session',{}).get('model')
                        if not hub.can_mint(lease,generation):
                            return web.json_response({'error':'USER_PAUSED'},status=409)
                        return web.json_response({'value':data['value'],'expires_at':data.get('expires_at')})
            except Exception as exc:
                record.update(status='failed', error_type=type(exc).__name__)
                return web.json_response({'error':type(exc).__name__}, status=502)
            finally:
                write_local('broker.jsonl', record)  # Never log tokens, headers, audio or transcripts.

    async def feedback(request):
        body = await request.json()
        note = str(body.get('note',''))[:2000]
        # Deliberately only user's submitted feedback, no implicit conversation/history capture.
        write_local('feedback.jsonl', {'note':note,'model':MODEL,'voice':VOICE})
        return web.json_response({'saved':True})

    async def index(request): return web.FileResponse(HERE/'index.html')
    async def bundle(request): return web.FileResponse(HERE/'dist'/'app.js')
    async def css(request): return web.FileResponse(HERE/'style.css')
    app.router.add_get('/', index)
    app.router.add_get('/app.js', bundle)
    app.router.add_get('/style.css', css)
    app.router.add_get('/config', config)
    app.router.add_get('/bridge', hub.websocket)
    app.router.add_post('/token', token)
    app.router.add_post('/feedback', feedback)
    return app


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=9884)
    parser.add_argument('--session-limit', type=int, default=3)
    args=parser.parse_args()
    if not (HERE/'dist'/'app.js').exists():
        raise SystemExit('Run build.bat first.')
    print(f'Native voice only: http://127.0.0.1:{args.port} ; manual sessions <= {args.session_limit}', flush=True)
    web.run_app(create_app(args.port,args.session_limit), host='127.0.0.1', port=args.port, access_log=None)
