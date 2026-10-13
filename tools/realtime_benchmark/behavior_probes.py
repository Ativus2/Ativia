"""Run AFTER latency trials: these tests intentionally use the actual speakers."""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from tools.realtime_benchmark.common import REPORTS,read_fixture


def baseline():
    from overlay import OverlayApp
    from runtime_settings import SettingsStore
    from speech_activity import SpeechActivity
    rows=[]
    for index in range(1,21):
        samples,_=read_fixture(REPORTS/'fixtures'/f'{index:02}.wav')
        detector=SpeechActivity(SettingsStore().load())
        feed=Mock(wraps=detector.feed)
        fake=NS(_mic=NS(segmented_input=True,read_pending=lambda:[(samples,1.0)]),
            _voice_dialog=None,_settings=NS(mic_enabled=True,echo_guard=True),
            _player=NS(is_busy=lambda:True),_echo_until=0,_vad=NS(reset=detector.reset,feed=feed))
        # Invoke frozen production method, while the output reports busy.
        OverlayApp._poll_microphone(fake)
        rows.append(dict(index=index,accepted_by_vad=feed.call_count>0,
            interruption_ms=None,outcome='mic_discarded_by_echo_guard'))
    return dict(method='actual OverlayApp._poll_microphone with fixture input and controlled busy output state',
                acoustic_test=False,rows=rows)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--kind',choices=['interruption','echo','baseline'],required=True)
    args=p.parse_args()
    if args.kind=='baseline': result=baseline()
    else:
        from experiments.realtime_prototype.benchmark_adapter import command_sync,close
        try: result=command_sync(dict(type=args.kind,count=20,seconds=6))
        finally: close()
    path=REPORTS/f'{args.kind}-probe.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))
