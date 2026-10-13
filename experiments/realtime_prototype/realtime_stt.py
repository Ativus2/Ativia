"""Official transcription WebSocket; append during speech, commit once, no LLM on deltas."""
import asyncio
import base64
import json
from time import monotonic
from urllib.parse import urlsplit,urlunsplit

import aiohttp
import numpy as np


class RealtimeTranscription:
    def __init__(self,api,record,on_delta=None):
        self.api=api;self.record=record;self.on_delta=on_delta
        self.queue=asyncio.Queue(maxsize=1024);self.ready=asyncio.Event();self.final=asyncio.get_running_loop().create_future()
        self.ws=None;self.http=None;self.runner=None;self.receiver=None;self.sender=None
        self.item_id=None;self.completed={};self.committed=False;self.closed=False;self.audio_samples=0
        self.record.update(model='gpt-live-transcribe',server_turn_detection=None,ws_attempts=0,
                           append_count=0,events=[],audio_seconds=0)

    def mark(self,event,**fields):
        self.record['events'].append(dict(event=event,at=monotonic(),**fields))

    async def open(self):
        url=urlsplit(str(self.api.base_url))
        if url.scheme!='https' or url.hostname!='api.openai.com':
            raise ValueError('realtime_transcription_requires_verified_openai_host')
        target=urlunsplit(('wss',url.netloc,url.path.rstrip('/')+'/realtime','intent=transcription',''))
        self.record['host']=url.hostname;self.record['ws_attempts']+=1;self.mark('connect_start')
        self.http=aiohttp.ClientSession(trust_env=True,timeout=aiohttp.ClientTimeout(total=15))
        try:
            self.ws=await self.http.ws_connect(target,headers={'Authorization':'Bearer '+self.api.api_key},
                timeout=aiohttp.ClientWSTimeout(ws_receive=40,ws_close=3),heartbeat=15,max_msg_size=2**20)
            self.mark('connected')
            self.receiver=asyncio.create_task(self.receive())
            configuration=dict(type='transcription',audio=dict(input=dict(format=dict(type='audio/pcm',rate=24000),
                transcription=dict(model='gpt-live-transcribe',languages=['zh'],delay='low'),turn_detection=None)))
            self.record['requested_session']=configuration
            await self.ws.send_json(dict(type='session.update',session=configuration))
            await asyncio.wait_for(self.ready.wait(),12)
            if self.final.done():return self.final.result()
            self.sender=asyncio.create_task(self.send_audio())
        except BaseException:
            await self.close();raise

    async def receive(self):
        try:
            async for message in self.ws:
                if message.type!=aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.ERROR,aiohttp.WSMsgType.CLOSED):break
                    continue
                data=json.loads(message.data);kind=data.get('type','')
                if kind in ('session.created','session.updated'):
                    self.mark(kind)
                    if kind=='session.updated':
                        self.record['resolved_session']=data.get('session');self.ready.set()
                elif kind=='input_audio_buffer.committed':
                    self.item_id=data['item_id'];self.mark('commit_ack',item_id=self.item_id);self.resolve()
                elif kind=='conversation.item.input_audio_transcription.delta':
                    self.mark('transcript_delta',item_id=data['item_id'])
                    if self.on_delta:await self.on_delta(data.get('delta',''))
                elif kind=='conversation.item.input_audio_transcription.completed':
                    self.completed[data['item_id']]=data
                    self.mark('transcript_complete',item_id=data['item_id']);self.resolve()
                elif kind in ('error','conversation.item.input_audio_transcription.failed'):
                    error=data.get('error') or {};raise RuntimeError('realtime_stt:'+str(error.get('code'))+':'+str(error.get('message')))
            if not self.closed and not self.final.done():raise ConnectionError('realtime_stt_disconnected_before_final')
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.record.update(error_type=type(exc).__name__,error=str(exc));self.mark('failed')
            if not self.final.done():self.final.set_exception(exc)
            self.ready.set()

    def resolve(self):
        if self.item_id in self.completed and not self.final.done():
            data=self.completed[self.item_id];text=(data.get('transcript') or '').strip()
            self.record.update(transcript=text,usage=data.get('usage'),item_id=self.item_id)
            if text:self.final.set_result(text)
            else:self.final.set_exception(ValueError('empty_realtime_transcript'))

    def append(self,frame):
        if self.closed or self.committed:raise RuntimeError('append_after_commit_or_close')
        frame=np.asarray(frame,dtype=np.float32)
        if frame.ndim!=1 or frame.size>16000 or not np.isfinite(frame).all():raise ValueError('invalid_stt_frame')
        self.audio_samples+=len(frame)
        if self.audio_samples>16000*32:raise ValueError('stt_utterance_too_long')
        self.queue.put_nowait(frame.copy())

    async def send_audio(self):
        try:
            while True:
                frame=await self.queue.get()
                try:
                    # Same linear resampling method as existing Ativia; no ASR engine implemented here.
                    audio=np.interp(np.arange(len(frame)*3//2)/1.5,np.arange(len(frame)),frame)
                    pcm=(np.clip(audio,-1,1)*32767).astype('<i2').tobytes()
                    await self.ws.send_json(dict(type='input_audio_buffer.append',audio=base64.b64encode(pcm).decode()))
                    self.record['append_count']+=1;self.mark('audio_appended',samples_16k=len(frame))
                finally:self.queue.task_done()
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.record.update(error_type=type(exc).__name__,error=str(exc));self.mark('send_failed')
            if not self.final.done():self.final.set_exception(exc)

    async def finish(self):
        if self.committed:raise RuntimeError('duplicate_stt_commit')
        self.committed=True
        async with asyncio.timeout(30):
            await self.ready.wait()
            if self.final.done():return self.final.result()
            if self.sender is None:raise RuntimeError('stt_sender_not_ready')
            drained=asyncio.create_task(self.queue.join())
            try:
                await asyncio.wait((drained,self.final),return_when=asyncio.FIRST_COMPLETED)
                if self.final.done():return self.final.result()
                await drained
            finally:
                drained.cancel()
                try:await drained
                except asyncio.CancelledError:pass
            self.record['audio_seconds']=self.audio_samples/16000
            self.mark('commit_sent')
            await self.ws.send_json(dict(type='input_audio_buffer.commit'))
            return await asyncio.shield(self.final)

    async def close(self):
        self.closed=True
        for task in (self.sender,self.receiver):
            if task and task is not asyncio.current_task():
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
        if self.ws:await self.ws.close()
        if self.http:await self.http.close()
        if self.final.done() and not self.final.cancelled():self.final.exception()
        elif not self.final.done():self.final.cancel()
