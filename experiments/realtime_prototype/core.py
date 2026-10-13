"""One cancellable turn, bounded sentence queue, one TTS worker. No observation."""
import asyncio
import base64
from dataclasses import dataclass, field
import io
import os
import re
import threading
from time import monotonic
import wave

import numpy as np
from openai import AsyncOpenAI
from audio import samples_to_wav, boost_samples, TTS_INSTRUCTIONS, TTS_MODEL
from gpu_policy import choose_route
from local_tts_client import synthesize_local
from tts_audio import validate_audio_file, delete_audio, MAX_AUDIO_BYTES
from tts_events import GPUProtected
from tts_process import operation_context
from speech_quirks import apply_speech_quirk
from experiments.realtime_prototype.vendor.sentence_divider import SentenceDivider
from experiments.realtime_prototype.llm_diagnostics import MeasuredLLM,active_record,stamp


@dataclass
class Turn:
    id: str
    metrics: object
    settings: object
    cancelled: threading.Event = field(default_factory=threading.Event)
    played: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    reply: str = ''
    backend: str | None = None
    segment: int = 0


class Engine:
    def __init__(self, allow_local=False, client=None, *, diagnostic=False, reasoning=None, model=None):
        self.client=client or AsyncOpenAI(timeout=30,max_retries=0)
        self.allow_local=allow_local
        self.local_lock=asyncio.Lock()
        self.model=model or os.getenv('OPENAI_MODEL','gpt-5.6-terra')
        self.reasoning=reasoning
        self.measured=MeasuredLLM(self.client) if diagnostic else None

    async def close(self):
        await self.client.close()

    async def _local_audio(self,text,turn):
        async with self.local_lock:
            def work():
                token=operation_context.set(dict(reply_id=turn.id,cancelled=turn.cancelled.is_set))
                path=None
                try:
                    path=synthesize_local(text,turn.settings)
                    info=validate_audio_file(path)
                    with wave.open(str(path),'rb') as wav: pcm=wav.readframes(wav.getnframes())
                    return pcm,info.sample_rate,info.channels
                finally:
                    delete_audio(path); operation_context.reset(token)
            task=asyncio.create_task(asyncio.to_thread(work))
            try: return await asyncio.shield(task)
            except asyncio.CancelledError:
                turn.cancelled.set()
                # Reap the bounded transport before another local request owns the gate.
                try: await asyncio.shield(task)
                except Exception: pass
                raise

    async def synthesize(self,text,turn,send):
        settings=turn.settings
        turn.segment+=1
        normalized=re.sub(r'[^\w\u4e00-\u9fff]','',text)
        meaningful=normalized not in {'嗯','好的','好','我看看','让我看看','稍等','稍等一下','收到','我想想'}
        original_send=send
        async def send(kind,**fields):
            await original_send(kind,segment=turn.segment,spoken_text=text,meaningful=meaningful,**fields)
        if not text.rstrip().endswith(('，',',','、',';','；')):
            text=apply_speech_quirk(text,enabled=settings.speech_quirk_enabled,
                                   probability=settings.speech_quirk_probability)
        if turn.backend is None:
            route=choose_route(settings) if self.allow_local else None
            turn.backend='local' if route and route.backend=='local' else 'openai'
            turn.metrics.mark('tts_admission',backend=turn.backend,reason=route.reason if route else 'fixed_cloud')
        backend=turn.backend
        turn.metrics.mark('tts_route',backend=backend,reason='turn_admission',segment=turn.segment)
        turn.metrics.row.setdefault('tts_backends',[]).append(backend)
        if backend=='local':
            try:
                pcm,rate,channels=await self._local_audio(text,turn)
                if channels != 1:
                    pcm=np.frombuffer(pcm,'<i2').reshape(-1,channels).mean(axis=1).astype('<i2').tobytes()
                turn.metrics.mark('tts_first_audio',backend='local')
                await send('pcm',pcm=base64.b64encode(pcm).decode(),rate=rate,backend='local')
                return
            except asyncio.CancelledError: raise
            except Exception as exc:
                turn.metrics.mark('local_failed',error=type(exc).__name__)
                if turn.cancelled.is_set():raise asyncio.CancelledError
                if not settings.tts_fallback and not isinstance(exc,GPUProtected): raise
                turn.backend='openai'
                turn.metrics.mark('tts_degraded',reason=type(exc).__name__,segment=turn.segment,
                                  replayed_prior_segments=False)
                turn.metrics.row['tts_backends'].append('openai')
        record=dict(kind='tts',model=TTS_MODEL,voice=settings.voice_name,speed=settings.voice_speed,
                    text=text,segment=turn.segment,meaningful=meaningful,events=[])
        turn.metrics.row.setdefault('api_calls',[]).append(record)
        token=active_record.set(record)
        try:
            await self._cloud_audio(text,turn,send,record)
            record['status']='complete'
        except asyncio.CancelledError:
            record['status']='cancelled';raise
        except Exception as exc:
            record.update(status='failed',error_type=type(exc).__name__);raise
        finally:active_record.reset(token)

    async def _cloud_audio(self,text,turn,send,record):
        settings=turn.settings
        total=0; pending=bytearray(); started=False
        tone={'original':'','light':' Use a light, bright adult tone.','soft':' Use a soft, relaxed adult tone.'}[settings.voice_tone]
        stamp(record,'request_start')
        async with self.client.audio.speech.with_streaming_response.create(model=TTS_MODEL,
                voice=settings.voice_name,input=text,instructions=TTS_INSTRUCTIONS+tone,
                speed=settings.voice_speed,response_format='pcm') as response:
            mime=response.headers.get('content-type','').lower()
            if mime.split(';')[0] not in ('application/octet-stream','audio/pcm','audio/l16'):
                raise ValueError('tts_not_pcm_content_type')
            async for chunk in response.iter_bytes(4096):
                if not total:stamp(record,'first_bytes')
                if turn.cancelled.is_set(): raise asyncio.CancelledError
                total+=len(chunk)
                if total>MAX_AUDIO_BYTES: raise ValueError('tts_pcm_too_large')
                pending.extend(chunk)
                if not started and pending[:4] in (b'RIFF',b'OggS',b'fLaC'):
                    raise ValueError('tts_container_instead_of_raw_pcm')
                even=len(pending)//2*2
                if not started:
                    if even<5760: continue  # 120 ms at 24kHz; reject empty/DC before first delivery.
                    probe=np.frombuffer(bytes(pending[:even]),'<i2').astype(np.float32)
                    if np.std(probe)<8 or np.max(np.abs(probe))<64: continue
                    started=True; turn.metrics.mark('tts_first_audio',backend='openai')
                    stamp(record,'valid_pcm')
                if even:
                    payload=bytes(pending[:even]); del pending[:even]
                    await send('pcm',pcm=base64.b64encode(payload).decode(),rate=24000,backend='openai')
            if pending or not started or total<5760: raise ValueError('tts_empty_truncated_or_silent_pcm')
            stamp(record,'response_end');record.update(pcm_bytes=total,audio_seconds=total/48000,
                request_id=response.headers.get('x-request-id'),usage=None)

    def messages(self,spoken,settings,parity,history):
        if parity:
            # Same prompt/LLM/budget as the frozen foreground baseline, only for controlled A/B.
            from vision import ENTERTAINMENT_PROMPT
            from companion_policy import help_context
            user=('用户刚刚直接说：'+spoken+'\n'+help_context(settings.user_expertise,True)
                  +'\n近期交流（仅作背景）：'+'\n已有屏幕观察（可能过时，本轮没有新截图）：[]'
                  +'\n先接用户这句话；无法从已有信息判断时说明缺少当前画面，不猜测。')
            return [dict(role='system',content=ENTERTAINMENT_PROMPT),dict(role='user',content=user)]
        system='你是 Ativia，一个私人语音助手。用自然、简短的中文直接回答用户，通常一到两句话。不要输出动作、角色扮演、表情标签或Markdown。没有屏幕输入时不要猜测屏幕。'
        return [dict(role='system',content=system),*history[-6:],dict(role='user',content=spoken)]

    async def respond(self,turn,samples,send,*,parity=False,history=(),spoken=None):
        metrics=turn.metrics
        async with asyncio.timeout(150):
            if spoken is None:
                metrics.mark('stt_started')
                record=dict(kind='stt_file',model='whisper-1',audio_seconds=len(samples)/16000,events=[])
                metrics.row.setdefault('api_calls',[]).append(record);token=active_record.set(record)
                try:
                    stamp(record,'request_start')
                    result=await self.client.audio.transcriptions.create(model='whisper-1',
                        file=('speech.wav',samples_to_wav(boost_samples(samples)),'audio/wav'))
                    stamp(record,'response_end');record.update(status='complete',request_id=getattr(result,'_request_id',None))
                except Exception as exc:
                    record.update(status='failed',error_type=type(exc).__name__);raise
                finally:active_record.reset(token)
                spoken=(result.text or '').strip()
                metrics.mark('stt_final'); metrics.row['transcript']=spoken
                await send('transcript',text=spoken)
            if not spoken: raise ValueError('empty_transcript')
            queue=asyncio.Queue(maxsize=4)
            async def tokens():
                messages=self.messages(spoken,turn.settings,parity,history)
                if self.measured:
                    record=dict(kind='llm',events=[]);metrics.row['llm']=record
                    metrics.row.setdefault('api_calls',[]).append(record)
                    async for text in self.measured.stream(messages=messages,model=self.model,
                            reasoning=self.reasoning,record=record):
                        if turn.cancelled.is_set():raise asyncio.CancelledError
                        if 'llm_first_token' not in metrics.times:metrics.mark('llm_first_token')
                        if 'text_ready' not in metrics.times:metrics.mark('text_ready')
                        turn.reply+=text;await send('text_delta',text=text);yield text
                    return
                stream=await self.client.chat.completions.create(model=self.model,
                    messages=messages,max_completion_tokens=480,
                    stream=True,stream_options={'include_usage':True})
                async with stream:
                    async for chunk in stream:
                        if turn.cancelled.is_set(): raise asyncio.CancelledError
                        text=chunk.choices[0].delta.content if chunk.choices else None
                        if text:
                            if 'llm_first_token' not in metrics.times: metrics.mark('llm_first_token')
                            if 'text_ready' not in metrics.times: metrics.mark('text_ready')
                            turn.reply+=text
                            await send('text_delta',text=text)
                            yield text
            async def produce():
                async for sentence in SentenceDivider(faster_first_response=True).process_stream(tokens()):
                    if any(tag.name=='think' for tag in sentence.tags): continue
                    if re.search(r'[\w\u4e00-\u9fff]',sentence.text): await queue.put(sentence.text)
                await queue.put(None)
            async def consume():
                while True:
                    text=await queue.get()
                    if text is None: break
                    await self.synthesize(text,turn,send)
                if 'tts_first_audio' not in metrics.times: raise ValueError('no_spoken_audio')
            async with asyncio.TaskGroup() as group:
                group.create_task(produce()); group.create_task(consume())
            metrics.row['reply']=turn.reply
            await send('audio_eos')
            async with asyncio.timeout(90): await turn.played.wait()
            if metrics.row.get('error'): raise RuntimeError(metrics.row['error'])
            if 'audible' not in metrics.times: raise ValueError('playback_never_audible')
            metrics.row['status']='complete'
            return spoken,turn.reply
