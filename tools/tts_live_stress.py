"""Explicit live TTS acceptance run. Only fixed test phrases are sent to providers."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import socket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument("--scenario", choices=("local", "fallback"), required=True)
parser.add_argument("--count", type=int, default=20)
parser.add_argument("--volume", type=float, default=.15)
parser.add_argument("--voice", default="dataset_voice_2", help="Local voice profile to exercise")
args = parser.parse_args()
out = ROOT / "local_tts/logs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-stress-" + args.scenario)
out.mkdir(parents=True)
try:
    with socket.create_connection(('127.0.0.1', 9881), timeout=2):
        service_reachable = True
except OSError:
    service_reachable = False
if service_reachable != (args.scenario == 'local'):
    raise SystemExit('Scenario precondition failed: check whether the local service should be on or off.')
(out / 'setup.json').write_text(json.dumps(dict(local_service_reachable=service_reachable,
                                              scenario=args.scenario)), encoding='utf-8')
os.environ["ATIVIA_TTS_EVENT_LOG"] = str(out / "events.jsonl")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from dataclasses import replace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from audio import SpeechPlayer
from runtime_settings import RuntimeSettings
from tts_pipeline import SpeechJob, SpeechPipeline

app = QApplication([])
player = SpeechPlayer()
pipeline = SpeechPipeline(player)
settings = replace(RuntimeSettings(), voice_backend="local", local_voice=args.voice,
                   tts_fallback=True, speech_quirk_enabled=False, voice_volume=args.volume)
rows = []
for i in range(args.count):
    job = SpeechJob(f"第{i + 1}条，准备好了。", settings, source="stress")
    rows.append(dict(reply_id=job.reply_id, index=i+1, text=job.text, started=False, finished=False, error=None))
    pipeline.submit(job)
by_id = {r["reply_id"]: r for r in rows}


def persist():
    (out / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def started(reply_id):
    by_id[reply_id]["started"] = True
    persist()


def finished(reply_id):
    by_id[reply_id]["finished"] = True
    print("PLAYED", by_id[reply_id]["index"], reply_id, flush=True)
    persist()


def failed(reply_id, reason):
    by_id[reply_id]["error"] = reason
    print("FAILED", by_id[reply_id]["index"], reason, flush=True)
    persist()


pipeline.reply_started.connect(started)
pipeline.reply_finished.connect(finished)
pipeline.reply_failed.connect(failed)
pipeline.idle.connect(app.quit)
QTimer.singleShot(max(120000, args.count * 180000), lambda: pipeline.close())
print("REPORT_DIR", out, flush=True)
app.exec()
events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
for row in rows:
    row["events"] = [e["event"] for e in events if e["reply_id"] == row["reply_id"]]
passed = all(r["started"] and r["finished"] and not r["error"] for r in rows)
fallbacks = sum("FALLBACK_TTS_SUCCESS" in r["events"] for r in rows)
if args.scenario == "fallback":
    passed = passed and fallbacks == args.count and all("LOCAL_TTS_SUCCESS" not in r["events"] for r in rows)
else:
    passed = passed and fallbacks == 0 and all("LOCAL_TTS_SUCCESS" in r["events"] for r in rows)
result = dict(scenario=args.scenario, count=args.count, played=sum(r["finished"] for r in rows),
              fallback_success=fallbacks, passed=passed, volume=args.volume,
              local_voice=settings.local_voice,
              local_service_reachable_at_start=service_reachable,
              playback_evidence=SpeechPlayer.EVIDENCE, rows=rows)
(out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("RESULT", json.dumps({k:v for k,v in result.items() if k != "rows"}), flush=True)
raise SystemExit(0 if passed else 1)
