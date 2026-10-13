"""Opt-in real cloud/device acceptance. Never substitutes fake cloud responses."""
import argparse,asyncio,json,os,sys,threading,wave
from dataclasses import replace
from pathlib import Path
from time import monotonic
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env',override=False)
from live_translation import TranslationStore
from translation.asr import make_provider
from translation.asr.audio_capture import AudioCapture
from translation.asr.base import ASRError
from translation.session import PipelineSession

async def capture_test(source,language,duration,audio=None,translate=False,output=None,mode=None,hold_formal=0):
    settings=replace(TranslationStore().load(),backend='openai',asr_language=language,audio_source=source)
    if mode:settings=replace(settings,recognition_mode=mode)
    provider=make_provider(settings);capture=AudioCapture(source)
    provider.sample_rate=await capture.prepare()
    path=output or ROOT/'logs'/('openai-'+source+'-'+language+'-live.jsonl')
    path.parent.mkdir(parents=True,exist_ok=True)
    events=[];blocks=0;started=monotonic();rms_max=0
    with path.open('w',encoding='utf-8') as log:
        def record(row):
            row=dict(row,elapsed_ms=round((monotonic()-started)*1000,1));events.append(row)
            log.write(json.dumps(row,ensure_ascii=False)+'\n');log.flush()
        record(dict(stage='metadata',provider='openai',model=provider.model,audio_source=source,
                    language=language,sample_rate=provider.sample_rate,wire_sample_rate=24000,
                    duration_seconds=duration,translation_enabled=translate,real_cloud=True,
                    recognition_mode=settings.recognition_mode,commit_config=vars(provider.controller.config),
                    deliberate_formal_hold_seconds=hold_formal,
                    input=('live WASAPI course playback' if source=='system' else 'microphone pickup of course playback') if audio else ('live microphone speech' if source=='microphone' else 'live system audio')))
        original_blocks=capture.blocks
        async def measured_blocks():
            nonlocal blocks,rms_max
            import audioop
            async for pcm,timestamp in original_blocks():
                blocks+=1;rms_max=max(rms_max,audioop.rms(pcm,2))
                yield pcm,timestamp
        capture.blocks=measured_blocks
        async def feed(session):
            await asyncio.wait_for(provider.ready.wait(),20)
            session.capture=capture
            pumping=asyncio.create_task(session._audio())
            try:
                deadline=monotonic()+10
                while (not capture.ready.is_set() or not blocks) and monotonic()<deadline:
                    if pumping.done():await pumping
                    await asyncio.sleep(.02)
                if not blocks:raise ASRError("音频设备尚未提供 PCM，测试未开始。")
                record(dict(stage="capture_ready",audio_blocks=blocks,timestamp=monotonic()))
                if audio:
                    import sounddevice as sd
                    with wave.open(str(audio),'rb') as w:rate=w.getframerate();pcm=w.readframes(round(duration*rate))
                    stream=sd.RawOutputStream(samplerate=rate,channels=1,dtype='int16')
                    await asyncio.to_thread(stream.start)
                    try:
                        await asyncio.sleep(.4)
                        record(dict(stage='playback_started',timestamp=monotonic()))
                        for i in range(0,len(pcm),rate//10*2):
                            await asyncio.to_thread(stream.write,pcm[i:i+rate//10*2])
                        await asyncio.to_thread(stream.stop)
                    finally:await asyncio.to_thread(stream.close)
                    await asyncio.sleep(1)
                else:
                    print('LIVE_CAPTURE_STARTED: '+source,flush=True)
                    await asyncio.sleep(duration)
            finally:
                pumping.cancel();await asyncio.gather(pumping,return_exceptions=True)
                await capture.stop()
            await session.finish_file()
        failure=None
        try:
            if translate:
                from openai import AsyncOpenAI
                from translation.pipeline import TranslationProvider
                settings.validate()
                async with AsyncOpenAI(api_key=settings.api_key,base_url=settings.base_url,
                        organization='',project='',timeout=10,max_retries=0) as client:
                    class HeldTranslation(TranslationProvider):
                        held=False
                        async def translate(self,c,context):
                            if c.state != "PARTIAL" and hold_formal and not self.held:
                                self.held=True
                                await asyncio.sleep(hold_formal)
                            return await super().translate(c,context)
                    session=PipelineSession(provider,settings,HeldTranslation(client,settings),diagnostic=record)
                    await session.run(feed=feed)
            else:
                session=PipelineSession(provider,settings,None,diagnostic=record)
                await session.run(feed=feed)
        except Exception as exc:
            failure=str(exc) if isinstance(exc,ASRError) else 'device_or_network_failure'
            record(dict(stage='error',message=failure))
        finally:await capture.stop()
        counts={stage:sum(e['stage']==stage for e in events) for stage in ('raw_partial','raw_final','stable_final','translated','provisional','provisional_request','reset')}
        latest={};translated={}
        for row in events:
            identity=(row.get('epoch'),row.get('caption_id'))
            if row['stage']=='stable_final':latest[identity]=row['revision']
            elif row['stage']=='translated':translated[identity]=row['revision']
        translation_complete=not translate or bool(latest) and all(translated.get(k)==v for k,v in latest.items())
        summary=dict(stage='summary',**counts,translation_complete=translation_complete,audio_blocks=blocks,dropped_blocks=capture.dropped,
                     maximum_rms=rms_max,driver_discontinuities=capture.discontinuities,cloud_task_stopped=bool(provider.task and provider.task.done()),
                     websocket_closed=provider.ws is None,capture_thread_stopped=not(capture.thread and capture.thread.is_alive()),
                     application_stream_continuous=not counts['reset'] and not capture.dropped,
                     continuous_stream=not counts['reset'] and not capture.dropped and not capture.discontinuities,
                     device_continuity_verified=not capture.discontinuities,
                     completed=bool(counts['raw_final'] and counts['stable_final']) and failure is None and not counts['reset'] and not capture.dropped and translation_complete)
        record(summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
        return 0 if summary['completed'] else 2

async def faults():
    from translation.asr.openai_asr import OpenAIASRProvider
    out={}
    p=OpenAIASRProvider(language='en');epochs=[];p.on_reset=epochs.append
    await p.start()
    try:
        await asyncio.wait_for(p.ready.wait(),20)
        p.ws.transport.abort()
        until=monotonic()+20
        while (not epochs or not p.ready.is_set()) and monotonic()<until:
            if p.task.done():await p.task
            await asyncio.sleep(.05)
        out['real_network_disconnect_reconnected']=bool(epochs and p.ready.is_set())
        out['epochs']=epochs
    finally:
        began=monotonic();await p.stop()
        out['stop_ms']=round((monotonic()-began)*1000,1)
        out['task_stopped']=p.task.done();out['websocket_closed']=p.ws is None
    # Deliberate invalid token verifies real server denial without using/logging the user's key.
    denied=OpenAIASRProvider(key_loader=lambda:'invalid-ativia-acceptance-token',retry_delays=())
    try:await asyncio.wait_for(denied.run(),15);out['api_error_handled']=False
    except ASRError:out['api_error_handled']=True
    finally:await denied.stop()
    out['still_running_after_api_error']=True
    (ROOT/'logs/openai-real-faults.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
    print(json.dumps(out),flush=True)
    return 0 if all(out.get(k) for k in ('real_network_disconnect_reconnected','task_stopped','websocket_closed','api_error_handled')) else 2

async def main(args):
    if args.faults:return await faults()
    else:return await capture_test(args.source,args.language,args.duration,args.audio,args.translate,args.output,args.mode,args.hold_formal)
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--source',choices=['microphone','system'],default='system')
    ap.add_argument('--language',choices=['en','ko','ja','zh','auto'],default='en');ap.add_argument('--duration',type=float,default=25)
    ap.add_argument('--audio',type=Path);ap.add_argument('--faults',action='store_true')
    ap.add_argument('--translate',action='store_true')
    ap.add_argument('--output',type=Path)
    ap.add_argument('--mode',choices=['low','balanced','accurate'])
    ap.add_argument('--hold-formal',type=float,default=0)
    args=ap.parse_args()
    if not 1<=args.duration<=120:ap.error('--duration must be between 1 and 120 seconds')
    raise SystemExit(asyncio.run(main(args)))

