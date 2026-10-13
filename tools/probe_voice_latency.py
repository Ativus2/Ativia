"""Controlled offline scheduling probe, runnable against an extracted previous ZIP.

Service delays are deliberately fixed. Results measure scheduling, not live API speed.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument('--project', required=True)
parser.add_argument('--site-packages', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--version', choices=['before', 'after'], required=True)
args = parser.parse_args()
project = Path(args.project).resolve()
sys.path[:0] = [str(project), str(project / 'tests'), args.site_packages]
import numpy as np
from PySide6.QtCore import QTimer
from unittest.mock import patch
import behavior_probes

behavior_probes.AppCase.setUpClass()
case = behavior_probes.AppCase()
case.setUp()
case.clock.side_effect = time.monotonic
case.app.control._on_speak_clicked()
case.tts.side_effect = lambda *a, **k: (time.sleep(.05), '')[1]
entered = threading.Event()
responded = threading.Event()
stages = {}

def stt(*a, **k):
    began = time.monotonic()
    time.sleep(.05)
    stages['stt_ms'] = (time.monotonic() - began) * 1000
    return '这个怎么打？'

def answer(*a, **k):
    if not entered.is_set():
        entered.set()
        time.sleep(.4)
        return '屏幕反应'
    time.sleep(.08)
    return '用户回复'

case.ask.side_effect = answer
case.mock('overlay.transcribe_speech', side_effect=stt)
def shown(text, *a, **k):
    if text == '用户回复':
        stages['response_at'] = time.monotonic()
        responded.set()
case.show_text.side_effect = shown

def pump(predicate, timeout):
    until = time.monotonic() + timeout
    while not predicate() and time.monotonic() < until:
        case.qt.processEvents()
        time.sleep(.005)
    if not predicate():
        raise RuntimeError('Probe timed out')

try:
    case.app._auto = True
    case.app._start_auto_observe()
    pump(entered.is_set, 3)
    speech_end = time.monotonic()
    if args.version == 'before':
        case.mic.snapshot.return_value = np.full(16000, .1, dtype=np.float32)
        case.app._timer.start()  # Speech ended just after a poll; next poll uses the real 12 s interval.
        case.app._start_auto_observe()  # Busy background prevents immediate service.
    else:
        from speech_activity import Utterance
        case.mock('voice_pipeline.transcribe_speech', side_effect=stt)
        def response(spoken, recent, observation, settings, metrics, cancelled, mode):
            began = time.monotonic()
            time.sleep(.02)
            metrics['llm_first_token_ms'] = (time.monotonic() - began) * 1000
            time.sleep(.06)
            metrics['llm_total_ms'] = (time.monotonic() - began) * 1000
            return '用户回复'
        case.mock('voice_pipeline.respond_to_voice', side_effect=response)
        case.mock('voice_pipeline.synthesize_speech', side_effect=case.tts.side_effect)
        QTimer.singleShot(600, lambda: case.app._enqueue_voice(Utterance(np.zeros(16000, dtype=np.float32), speech_end, time.monotonic())))
    pump(responded.is_set, 20)
    case.app._timer.stop()
    pump(lambda: not case.app._busy(), 3)
    report = dict(version=args.version, synthetic_services=True, configured_poll_ms=12000,
                  background_delay_ms=400, configured_stt_ms=50, configured_llm_ms=80, configured_tts_ms=50,
                  speech_to_text_ms=round((stages['response_at'] - speech_end) * 1000, 2),
                  stage_metrics=getattr(case.app, '_latest_voice_metrics', None), live_api_calls=0,
                  limitation='One controlled worst-poll-phase sample; not live microphone/network latency.')
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
finally:
    case.tearDown()
    case.doCleanups()
