"""Read the actual client configuration without serializing any credentials."""
import asyncio
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from openai import AsyncOpenAI

OUT=ROOT/'docs/quick-dialogue-diagnostic'

async def main():
    OUT.mkdir(parents=True,exist_ok=True)
    path=OUT/'discovery.json'
    if path.exists():raise FileExistsError('Do not overwrite discovery evidence')
    async with AsyncOpenAI(timeout=30,max_retries=0) as api:
        url=urlsplit(str(api.base_url))
        info=dict(recorded_at=datetime.now(timezone.utc).isoformat(),host=url.hostname,
            request_model=os.getenv('OPENAI_MODEL','gpt-5.6-terra'),path=url.path+'chat/completions',
            reasoning_effort_sent=None,reasoning_default='medium per official GPT-5.6 Sol documentation; response verification pending',
            max_completion_tokens=480,timeout_seconds=30,sdk_retries=api.max_retries,
            client_reused_across_turns=True,http_client_type=type(api._client).__module__+'.'+type(api._client).__name__)
        try:
            result=await api.models.list()
            models=sorted(m.id for m in result.data)
            info.update(models_list_api_calls=1,model_ids=models)
        except Exception as exc:
            info.update(models_list_api_calls=1,models_error=type(exc).__name__,status=getattr(exc,'status_code',None))
        path=OUT/'discovery.json'
        if path.exists():raise FileExistsError('Do not overwrite discovery evidence')
        path.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(info,ensure_ascii=True))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=OUT);args=parser.parse_args()
    OUT=args.output;asyncio.run(main())
