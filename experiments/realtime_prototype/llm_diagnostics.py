"""Credential-free timing taps around the actual SDK stream and HTTP transport."""
import asyncio
from contextvars import ContextVar
import hashlib
import json
from time import monotonic

active_record=ContextVar('llm_diagnostic_record',default=None)


def stamp(record,name,**fields):
    at=monotonic();record.setdefault('times',{}).setdefault(name,at)
    record.setdefault('events',[]).append(dict(event=name,at=at,**fields))


class MeasuredLLM:
    def __init__(self,api):
        self.api=api;self.connections={};self.http_calls=0
        api._client.event_hooks['request'].append(self.on_request)
        api._client.event_hooks['response'].append(self.on_response)

    async def on_request(self,request):
        self.http_calls+=1
        record=active_record.get()
        if record is None:return
        stamp(record,'http_request',host=request.url.host,path=request.url.path)
        record['http_attempts']=record.get('http_attempts',0)+1
        if request.url.path.endswith('/chat/completions'):
            body=json.loads(request.content)
            # Credentials are never copied from headers or client attributes.
            record['wire_request']=body
            record['request_sha256']=hashlib.sha256(request.content).hexdigest()
        async def trace(event,info):
            # Only timing/event names, never info (which may contain headers).
            stamp(record,'transport:'+event)
        request.extensions['trace']=trace

    async def on_response(self,response):
        record=active_record.get()
        if record is None:return
        connection=response.extensions.get('network_stream')
        key=id(connection) if connection is not None else None
        reused=key in self.connections if key is not None else None
        if key is not None:self.connections.setdefault(key,len(self.connections)+1)
        stamp(record,'response_headers',status=response.status_code)
        record.update(host=response.request.url.host,status_code=response.status_code,
            request_id=response.headers.get('x-request-id'),http_version=response.http_version,
            connection_id=self.connections.get(key),connection_reused=reused)

    async def stream(self,*,messages,model,record,reasoning=None,budget=480):
        record.update(request_model=model,reasoning_effort_sent=reasoning,output_budget=budget,
            timeout_seconds=30,sdk_retries=self.api.max_retries,client_reused=True)
        token=active_record.set(record);done=asyncio.Event();lags=[]
        async def heartbeat():
            expected=monotonic()+.01
            while not done.is_set():
                await asyncio.sleep(.01)
                if 'first_text' not in record.get('times',{}):lags.append(max(0,monotonic()-expected)*1000)
                expected=monotonic()+.01
        watcher=asyncio.create_task(heartbeat());text=[];stream=None
        try:
            stamp(record,'request_start')
            kwargs=dict(model=model,messages=messages,max_completion_tokens=budget,stream=True,
                        stream_options={'include_usage':True})
            if reasoning is not None:kwargs['reasoning_effort']=reasoning
            stream=await self.api.chat.completions.create(**kwargs)
            stamp(record,'sdk_stream_ready')
            async with stream:
                async for chunk in stream:
                    if 'first_event' not in record['times']:stamp(record,'first_event')
                    if chunk.model:record['response_model']=chunk.model
                    if getattr(chunk,'service_tier',None):record['service_tier']=chunk.service_tier
                    if chunk.usage:record['usage']=chunk.usage.model_dump()
                    for choice in chunk.choices:
                        if choice.finish_reason:record['finish_reason']=choice.finish_reason
                        content=choice.delta.content
                        if content:
                            if 'first_text' not in record['times']:stamp(record,'first_text')
                            text.append(content);yield content
            stamp(record,'response_end');record['status']='complete' if text else 'empty'
        except asyncio.CancelledError:
            record['status']='cancelled';stamp(record,'cancelled');raise
        except Exception as exc:
            record.update(status='failed',error_type=type(exc).__name__,http_status=getattr(exc,'status_code',None))
            stamp(record,'failed');raise
        finally:
            done.set();watcher.cancel()
            try:await watcher
            except asyncio.CancelledError:pass
            record['answer']=''.join(text)
            record['event_loop_max_lag_before_text_ms']=max(lags,default=0)
            active_record.reset(token)
            times=record.get('times',{});start=times.get('request_start')
            for name in ('http_request','response_headers','sdk_stream_ready','first_event','first_text','response_end'):
                record[name+'_ms']=(times[name]-start)*1000 if start is not None and name in times else None

