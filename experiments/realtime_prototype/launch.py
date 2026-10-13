"""Start only this loopback prototype and open its browser once ready."""
import json
import argparse
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--diagnostic',action='store_true');args=parser.parse_args()
PORT=9883 if args.diagnostic else 9882
URL=f'http://127.0.0.1:{PORT}'

def ready():
    try:
        with urllib.request.urlopen(URL+'/health',timeout=1) as response:
            state=json.load(response)
            if args.diagnostic and not state.get('diagnostic'):raise SystemExit('9883端口不是固定云端诊断服务，请检查占用。')
            return state.get('status')=='ready'
    except (OSError,ValueError):return False

if not ready():
    logs=HERE/'logs';logs.mkdir(exist_ok=True)
    with (logs/('diagnostic-launcher.log' if args.diagnostic else 'launcher.log')).open('ab') as log:
        flags=['--diagnostic','--port',str(PORT)] if args.diagnostic else ['--enable-local']
        process=subprocess.Popen([sys.executable,str(HERE/'server.py'),*flags],
            cwd=HERE.parents[1],stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(60):
        if ready():break
        if process.poll() is not None:raise SystemExit('启动失败，请查看 logs/launcher.log')
        time.sleep(.25)
    else:raise SystemExit('启动未就绪，请查看 logs/launcher.log')
webbrowser.open(URL)
