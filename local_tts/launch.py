"""Idempotent foreground launcher with full server logs."""
from pathlib import Path
import subprocess
import sys
import json
from urllib.request import build_opener, ProxyHandler
ROOT=Path(__file__).resolve().parent
try:
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:9881/health",timeout=2) as response:
        result=json.load(response)
    if str(result.get("model", "")).startswith("GPT-SoVITS ") and result.get("status")=="ready":
        print("Ativia local TTS is already running on 127.0.0.1:9881.")
        sys.exit(0)
except (OSError,ValueError):pass
print("Starting local TTS. Keep this window open; Ctrl+C stops the service.",flush=True)
print("Logs: local_tts/logs/server-console.log",flush=True)
with (ROOT/"logs/server-console.log").open("a",encoding="utf-8") as log:
    try:
        result=subprocess.run([sys.executable,"-u",str(ROOT/"server.py")],cwd=ROOT.parent,stdout=log,stderr=subprocess.STDOUT)
    except KeyboardInterrupt:
        sys.exit(0)
sys.exit(result.returncode)
