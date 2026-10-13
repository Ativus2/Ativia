"""Live admission/concurrency checks with fixed public text, never private recordings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tts_audio import validate_wav_bytes

parser = argparse.ArgumentParser()
parser.add_argument('stage', choices=['cold', 'warmup', 'concurrency', 'fragments'])
args = parser.parse_args()
opener = build_opener(ProxyHandler({}))

def health():
    with opener.open('http://127.0.0.1:9881/health', timeout=5) as response:
        return json.load(response)

def request(mode='local', games=None, text='准备好了，我们继续。'):
    policy = dict(tts_mode=mode)
    if games is not None:
        policy['game_processes'] = games
    data = dict(text=text, voice='dataset_voice', speed=1.2, resource_policy=policy)
    started = time.monotonic()
    try:
        req = Request('http://127.0.0.1:9881/tts', json.dumps(data).encode(), {'Content-Type':'application/json'})
        with opener.open(req, timeout=120) as response:
            audio = response.read()
        info = validate_wav_bytes(audio)
        return dict(status=200, seconds=time.monotonic()-started, duration=info.duration, bytes=len(audio))
    except HTTPError as exc:
        return dict(status=exc.code, seconds=time.monotonic()-started, error=exc.read().decode())

before = health()
rows = []
try:
    if args.stage == 'cold':
        rows = [request('gaming'), request('local', ['python.exe'])]
        assert all(row['status'] == 409 for row in rows), rows
        assert before['stats']['model_load_count'] == 0, before
        assert health()['stats']['model_load_count'] == 0
    elif args.stage == 'warmup':
        rows = [request()]
        assert rows[0]['status'] == 200, rows
        assert health()['stats']['model_load_count'] == 1
    elif args.stage == 'fragments':
        rows = [request(text='这一页介绍了三个重点。第一步先读题，确认问题的条件。第二步整理已知信息，注意前后关系。最后检查答案是否完整。' * 4)]
        assert rows[0]['status'] == 200, rows
        assert health()['stats']['last']['fragments'] >= 2
        assert health()['stats']['max_active'] == 1
    else:
        barrier = threading.Barrier(4)
        def simultaneous(_):
            barrier.wait()
            return request()
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(simultaneous, range(4)))
        assert sum(row['status'] == 200 for row in rows) == 1, rows
        assert all(row['status'] in (200,409,503) for row in rows), rows
        assert health()['stats']['max_active'] == 1
        assert health()['stats']['model_load_count'] == 1
finally:
    result = dict(stage=args.stage, before=before, rows=rows, after=health())
    path = ROOT / 'local_tts/logs/gpu-protection' / (args.stage + '-service.json')
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
