import asyncio
import argparse
import json
from pathlib import Path
import sys
from time import monotonic
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from openai import AsyncOpenAI
from tools.realtime_benchmark.common import read_fixture
from experiments.realtime_prototype.realtime_stt import RealtimeTranscription
OUT=ROOT/'docs/quick-dialogue-diagnostic'

async def main():
    path=OUT/'stt-preflight.json';OUT.mkdir(parents=True,exist_ok=True)
    if path.exists():raise FileExistsError(path)
    samples,end=read_fixture(ROOT/'docs/realtime-benchmark/fixtures/03.wav');record={}
    async with AsyncOpenAI(max_retries=0) as api:
        stt=RealtimeTranscription(api,record)
        try:
            await stt.open();start=monotonic();record['speech_end']=start+end
            for i in range(0,len(samples),512):
                await asyncio.sleep(max(0,start+(i+512)/16000-monotonic()))
                stt.append(samples[i:i+512])
            text=await stt.finish();record['final_at']=monotonic();record['status']='complete'
            print(json.dumps(dict(text=text,latency_ms=(record['final_at']-record['speech_end'])*1000),ensure_ascii=True))
        except Exception as exc:
            record.update(status='failed',error_type=type(exc).__name__,error=str(exc));print(type(exc).__name__+':'+str(exc))
        finally:
            await stt.close();path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=OUT);args=parser.parse_args()
    OUT=args.output;asyncio.run(main())
