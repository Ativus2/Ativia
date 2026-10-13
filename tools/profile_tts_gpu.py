"""Bounded comparison of fixed TTS text and read-only GPU samples; no game simulation."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gpu_monitor import NvidiaReader
from tts_audio import validate_wav_bytes, delete_audio

parser = argparse.ArgumentParser()
parser.add_argument('--label', required=True)
parser.add_argument('--route', choices=['local', 'gaming', 'auto'], default='local')
parser.add_argument('--repeat', type=int, default=1)
args = parser.parse_args()
out = ROOT / 'local_tts/logs/gpu-protection' / args.label
out.mkdir(parents=True, exist_ok=True)
reader = NvidiaReader()
initial = reader.read()
if args.route == 'local' and (initial['utilization'] >= 45 or initial['free_mb'] < 2048):
    raise SystemExit('GPU is not idle enough for a local comparison; no TTS started.')
rows, samples = [], []
stop = threading.Event()
epoch = time.monotonic()
def monitor():
    while not stop.is_set():
        try:
            samples.append(dict(t=time.monotonic()-epoch, **reader.read()))
        except Exception as exc:
            samples.append(dict(t=time.monotonic()-epoch, error=type(exc).__name__))
        stop.wait(.1)
thread = threading.Thread(target=monitor)
thread.start()
phrases = ['准备好了，我们继续。', '先看清楚屏幕上的信息，再选择合适的下一步。',
           '这一页介绍了三个重点。第一步先读题，确认问题的条件。第二步整理已知信息，注意前后关系。最后检查答案是否完整。']
opener = build_opener(ProxyHandler({}))
def health():
    with opener.open('http://127.0.0.1:9881/health', timeout=3) as response:
        return json.load(response)
before = health()
try:
    time.sleep(2)
    for i, text in enumerate(phrases * args.repeat):
        began = time.monotonic()
        if args.route == 'local':
            request = Request('http://127.0.0.1:9881/tts', json.dumps(dict(text=text, voice='dataset_voice', speed=1.2,
                              resource_policy=dict(tts_mode='local'))).encode(), {'Content-Type':'application/json'})
            with opener.open(request, timeout=120) as response:
                content = response.read()
            info = validate_wav_bytes(content)
            backend = 'local'
        else:
            from dotenv import load_dotenv
            load_dotenv(ROOT / '.env')
            from runtime_settings import RuntimeSettings
            from audio import synthesize_speech
            settings = replace(RuntimeSettings(), voice_backend='local', tts_mode=args.route,
                               speech_quirk_enabled=False, local_voice='dataset_voice')
            path = synthesize_speech(text, settings, reply_id=args.label + '-' + str(i))
            info, backend = path.info, path.backend
            delete_audio(path)
        rows.append(dict(index=i+1, characters=len(text), began=began-epoch, ended=time.monotonic()-epoch,
                         latency_seconds=time.monotonic()-began, duration=info.duration, backend=backend))
        print('GENERATED', i+1, backend, round(rows[-1]['latency_seconds'],3), flush=True)
        time.sleep(1)
    time.sleep(2)
finally:
    stop.set(); thread.join(); reader.close()
    result = dict(label=args.label, route=args.route, initial=initial, rows=rows, samples=samples, health_before=before, health_after=health())
    valid = [s for s in samples if 'error' not in s]
    result.update(peak_gpu_percent=max(s['utilization'] for s in valid), peak_vram_mb=max(s['used_mb'] for s in valid))
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('RESULT', args.label, 'peak_gpu', result['peak_gpu_percent'], 'peak_vram_mb', round(result['peak_vram_mb']), flush=True)
