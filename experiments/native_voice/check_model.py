"""Read-only account check; output allowlisted metadata, never credentials."""
import asyncio,json,os
from aiohttp import ClientSession,ClientTimeout
from server import MODEL,VOICE
async def main():
    async with ClientSession(timeout=ClientTimeout(total=20),trust_env=True) as client:
        async with client.get('https://api.openai.com/v1/models/'+MODEL,
            headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']}) as response:
            body=await response.json()
            print(json.dumps({'endpoint_host':'api.openai.com','requested_model':MODEL,'voice':VOICE,
                'status':response.status,'actual_model':body.get('id'),'owned_by':body.get('owned_by'),
                'request_id':response.headers.get('x-request-id'),'error_code':body.get('error',{}).get('code')},ensure_ascii=False))
asyncio.run(main())
