"""Invalid loopback HTTP 200 -> real OpenAI PCM -> real WASAPI playback.

Does not stop/reconfigure the user's voice service. Fake server receives text only.
"""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from PySide6.QtWidgets import QApplication
from runtime_settings import SettingsStore
from tools.realtime_benchmark.common import Metrics,REPORTS
from tools.realtime_benchmark.baseline import MeasuredPlayer
from experiments.realtime_prototype.core import Engine,Turn


class Handler(BaseHTTPRequestHandler):
    body=b''
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.send_response(200);self.send_header('Content-Type','audio/wav')
        self.send_header('Content-Length',str(len(self.body)));self.end_headers();self.wfile.write(self.body)
    def log_message(self,*_): pass


async def main(qt):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    engine=Engine(True);rows=[]
    zero=io.BytesIO()
    with wave.open(zero,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b'')
    try:
        for index,(case,body) in enumerate([('empty_http_200',b''),('corrupt_http_200',b'RIFF'+b'x'*4096),('zero_duration_wav',zero.getvalue())],1):
            Handler.body=body
            metrics=Metrics('B-fallback',index);metrics.mark('text_ready')
            t=Turn(f'fallback-probe-{index}',metrics,replace(SettingsStore().load(),voice_volume=.12))
            pcm=[]
            async def send(kind,**kw):
                if kind=='pcm': pcm.append(base64.b64decode(kw['pcm']))
            with patch('local_tts_client.ENDPOINT',f'http://127.0.0.1:{server.server_port}'),patch(
                    'experiments.realtime_prototype.core.choose_route',return_value=NS(backend='local',reason='fault_injection_no_gpu')):
                await engine.synthesize('测试回退成功，声音可以正常播放。',t,send)
            fd,path=tempfile.mkstemp(suffix='.wav');os.close(fd)
            player=MeasuredPlayer(metrics);done=asyncio.Event()
            player.set_volume(.12)
            player.finished.connect(lambda *_:done.set())
            def failed(*args): metrics.row['error']=str(args);done.set()
            player.playback_failed.connect(failed)
            try:
                with wave.open(path,'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b''.join(pcm))
                player.play_file(path,t.id)
                async with asyncio.timeout(30):
                    while not done.is_set():qt.processEvents();await asyncio.sleep(.005)
                metrics.row['status']='complete' if 'audible' in metrics.times and not metrics.row['error'] else 'failed'
                metrics.mark('playback_finished');metrics.row['case']=case;rows.append(metrics.finalize())
            finally:player.stop();Path(path).unlink(missing_ok=True)
    finally:await engine.close();server.shutdown();server.server_close();thread.join(2)
    (REPORTS/'fallback-probe.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([dict(case=r['case'],status=r['status'],backends=r.get('tts_backends'),silent=r['silent_reply']) for r in rows]))


if __name__=='__main__':asyncio.run(main(QApplication([])))
