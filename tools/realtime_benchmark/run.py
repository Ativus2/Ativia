"""Fixed-fixture live A/B acceptance. Original application stays untouched."""
import argparse
import asyncio
from dataclasses import replace, asdict
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from PySide6.QtWidgets import QApplication
from runtime_settings import SettingsStore
from gpu_monitor import get_monitor
from tools.realtime_benchmark.common import read_fixture, Metrics, GpuTrace, write_result, REPORTS

parser=argparse.ArgumentParser()
parser.add_argument('--variants',default='A')
parser.add_argument('--count',type=int,default=20)
parser.add_argument('--label',required=True)
parser.add_argument('--mode',choices=['auto','gaming'],default='auto')
args=parser.parse_args()
folder=REPORTS/args.label
folder.mkdir(parents=True,exist_ok=False)
os.environ['ATIVIA_TTS_EVENT_LOG']=str(folder/'tts-events.jsonl')
qt=QApplication([])
settings=replace(SettingsStore().load(),tts_mode=args.mode,voice_backend='local',voice_volume=.12)
monitor=get_monitor()
gpu=GpuTrace(); gpu.start()
rows=[]
setup=dict(model=os.getenv('OPENAI_MODEL','gpt-5.6-terra'),settings=asdict(settings),
           source='Windows SAPI synthetic replay; live STT/LLM/TTS; A PortAudio/WASAPI, B Edge Web Audio output clocks',
           live_human_microphone=False,baseline_commit='7a0d69502b070ac092bfff7a8167abdbca494003')

async def main():
    await asyncio.sleep(2)
    for index in range(1,min(args.count,20)+1):
        samples,end=read_fixture(REPORTS/'fixtures'/f'{index:02}.wav')
        variants=args.variants.split(',')
        if index%2==0: variants.reverse()
        for variant in variants:
            metrics=Metrics(variant,index); began=monotonic()
            try:
                if variant=='A':
                    from tools.realtime_benchmark.baseline import run
                    await run(samples,end,settings,metrics,qt)
                elif variant=='B':
                    from experiments.realtime_prototype.benchmark_adapter import run
                    await run(samples,end,settings,metrics,qt)
                else: raise ValueError('unknown_variant')
            except Exception as exc:
                metrics.row.update(status='failed',error=type(exc).__name__+':'+str(exc))
            row=metrics.finalize(); row.update(gpu.peaks(began,monotonic())); rows.append(row)
            write_result(folder,rows,gpu,setup)
            print(json.dumps({k:row.get(k) for k in ['variant','index','status','first_audible_ms','silent_reply','tts_backend','error']},ensure_ascii=True),flush=True)
            await asyncio.sleep(1)

try: asyncio.run(main())
finally:
    gpu.stop(); monitor.stop(); write_result(folder,rows,gpu,setup)
