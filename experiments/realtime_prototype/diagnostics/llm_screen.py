"""Exactly 10 requests/config, rotated A/B/C; no STT/TTS calls."""
import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
from time import monotonic
from statistics import mean,median

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from openai import AsyncOpenAI
from runtime_settings import SettingsStore
from experiments.realtime_prototype.core import Engine
from experiments.realtime_prototype.llm_diagnostics import MeasuredLLM

OUT=ROOT/'docs/quick-dialogue-diagnostic'

async def main():
    folder=OUT/'llm-screen';folder.mkdir(parents=True,exist_ok=False)
    prompts=json.loads((ROOT/'docs/realtime-benchmark/fixtures/prompts.json').read_text(encoding='utf-8'))
    indices=[1,3,5,7,9,11,13,15,16,20]
    model=os.getenv('OPENAI_MODEL','gpt-5.6-sol')
    configs=[dict(id='A',model=model,reasoning=None),dict(id='B',model=model,reasoning='none'),
             dict(id='C',model='gpt-5.6-terra',reasoning='none')]
    discovery=json.loads((OUT/'discovery.json').read_text(encoding='utf-8'))
    assert configs[2]['model'] in discovery['model_ids']
    (folder/'config.json').write_text(json.dumps(dict(configs=configs,prompt_indices=indices,parity=True,
        context=[],budget=480,requests_planned=30,source_commit='0356966'),ensure_ascii=False,indent=2),encoding='utf-8')
    rows=[]
    async with AsyncOpenAI(timeout=30,max_retries=0) as api:
        measured=MeasuredLLM(api);engine=Engine(client=api);settings=SettingsStore().load()
        for i,index in enumerate(indices):
            order=configs[i%3:]+configs[:i%3]
            for config in order:
                record=dict(config=config['id'],index=index,prompt=prompts[index-1],events=[])
                start=monotonic();messages=engine.messages(prompts[index-1],settings,True,[])
                record['prompt_assembly_ms']=(monotonic()-start)*1000
                try:
                    async for _ in measured.stream(messages=messages,model=config['model'],record=record,reasoning=config['reasoning']):pass
                except Exception:pass
                rows.append(record)
                (folder/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(dict(config=config['id'],index=index,status=record['status'],
                    first_text_ms=record.get('first_text_ms'),usage=record.get('usage'),model=record.get('response_model'))),flush=True)
                await asyncio.sleep(.3)
        summary={}
        for config in configs:
            group=[r for r in rows if r['config']==config['id']]
            times=sorted(r['first_text_ms'] for r in group if r.get('first_text_ms') is not None)
            summary[config['id']]=dict(requests=len(group),answers=len(times),
                mean_ms=mean(times) if times else None,median_ms=median(times) if times else None,
                p95_ms=max(times) if times else None,failures=sum(r['status']!='complete' for r in group),
                reasoning_tokens=[(r.get('usage') or {}).get('completion_tokens_details',{}).get('reasoning_tokens') for r in group])
        (folder/'summary.json').write_text(json.dumps(dict(configurations=summary,http_calls=measured.http_calls),indent=2),encoding='utf-8')
        print(json.dumps(summary),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=OUT);args=parser.parse_args()
    OUT=args.output;asyncio.run(main())
