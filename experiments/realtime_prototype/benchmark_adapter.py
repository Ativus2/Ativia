"""Persistent real browser client for the B side, using the same UI/PCM/VAD path."""
import asyncio
import atexit
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
NODE=Path('C:/Users/kzcru/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
_process=None; _error_log=None


def close():
    global _process,_error_log
    if _process:
        _process.stdin.close()
        try: _process.wait(8)
        except subprocess.TimeoutExpired: _process.terminate();_process.wait(3)
        _process=None
    if _error_log: _error_log.close();_error_log=None


atexit.register(close)


def command_sync(data):
    global _process,_error_log
    if _process is None:
        _error_log=(ROOT/'docs/realtime-benchmark/browser-errors.log').open('a',encoding='utf-8')
        _process=subprocess.Popen([str(NODE),str(Path(__file__).with_name('browser_driver.cjs'))],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=_error_log,text=True,encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW)
        message=json.loads(_process.stdout.readline())
        if not message.get('ready'): raise RuntimeError('browser_not_ready')
    _process.stdin.write(json.dumps(data)+'\n');_process.stdin.flush()
    result=json.loads(_process.stdout.readline())
    if not result['ok']: raise RuntimeError(result['error'])
    return result['result']


async def command(data):
    return await asyncio.wait_for(asyncio.to_thread(command_sync,data),200)


async def run(samples,input_end,settings,metrics,qt):
    row=await command(dict(type='replay',index=metrics.row['index'],mode=settings.tts_mode))
    metrics.row=row
    metrics.times={}
    for event in row['events']: metrics.times.setdefault(event['event'],event['at'])
