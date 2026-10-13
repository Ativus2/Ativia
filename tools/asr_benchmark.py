"""Same-WAV recognition/translation benchmark. No semantic accuracy is inferred.
Examples:
  python tools/asr_benchmark.py --audio test.wav --provider openai --language ko --output logs/openai-ko.jsonl
  python tools/asr_benchmark.py --audio test.wav --provider deepgram --language ko --translate --output logs/dg-ko.jsonl
  python tools/asr_benchmark.py --audio test.wav --provider windows --language ko --windows-language-confirmed ko --translate --output logs/win-ko.jsonl
Windows cannot accept PCM: this command plays exactly the same WAV to the default
speaker, while Windows Live Captions captures system audio. Set its language first.
"""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

async def benchmark(args):
    from live_translation import TranslationStore
    from translation.asr import make_provider
    from translation.session import PipelineSession
    from translation.pipeline import TranslationProvider
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env', override=False)
    settings=replace(TranslationStore().load(), backend=args.provider, asr_language=args.language)
    if args.mode:settings=replace(settings,recognition_mode=args.mode)
    if args.context is not None: settings=replace(settings,context_sentence_count=str(args.context))
    if args.provider=='windows' and args.windows_language_confirmed != args.language:
        raise ValueError('先在 Windows 实时字幕选择源语言，然后加 --windows-language-confirmed '+args.language)
    with wave.open(str(args.audio),'rb') as audio:
        if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getcomptype()!='NONE':
            raise ValueError('测试音频需为 PCM16 单声道 WAV；请预先转换后给两个 Provider 使用同一文件。')
        rate=audio.getframerate(); pcm=audio.readframes(min(audio.getnframes(),round(args.max_seconds*rate)))
    if not pcm:raise ValueError('WAV 中没有音频。')
    provider=make_provider(settings)
    if provider.accepts_audio:provider.sample_rate=rate
    args.output.parent.mkdir(parents=True,exist_ok=True)
    counts={};records=[];started=monotonic()
    with args.output.open('w',encoding='utf-8') as output:
        def record(event):
            event=dict(event);event['elapsed_ms']=round((monotonic()-started)*1000,1)
            output.write(json.dumps(event,ensure_ascii=False)+'\n');output.flush()
            counts[event['stage']]=counts.get(event['stage'],0)+1
            records.append(event)
        record(dict(stage='metadata',provider=args.provider,model=getattr(provider,'model','nova-3' if args.provider=='deepgram' else 'Windows Live Captions'),
            wire_sample_rate=getattr(provider,'wire_sample_rate',None),
            recognition_mode=settings.recognition_mode,commit_config=vars(provider.controller.config) if args.provider=='openai' else None,
            latency_basis=getattr(provider,'latency_basis',None),endpointing=getattr(provider,'endpointing',None),
            audio_file=args.audio.name,audio_sha256=hashlib.sha256(args.audio.read_bytes()).hexdigest(),
            pcm_sha256=hashlib.sha256(pcm).hexdigest(),sample_rate=rate,duration_seconds=len(pcm)/(rate*2),
            language=args.language,endpoint_ms=int(settings.endpoint_ms),merge_window_ms=int(settings.final_merge_window_ms),
            context_sentence_count=int(settings.context_sentence_count),translation_enabled=args.translate,
            hotwords_count=len(provider.hotwords),language_hint_sent=args.provider!='windows',
            evaluation='人工评估；没有 Ground Truth，不计算准确率'))
        if args.provider=='windows':
            original_process=provider.process
            def process(event,now=None):
                if event.get('kind')=='snapshot':
                    record(dict(stage='raw_snapshot',provider='windows',text=event.get('text',''),
                                language_hint=args.language,timestamp=monotonic()))
                elif event.get('kind')=='ready':
                    record(dict(stage='source_ready',provider='windows',
                                description=event.get('description',''),
                                windows_language_confirmed=args.windows_language_confirmed))
                original_process(event,now)
            provider.process=process
        async def feed(session):
            await asyncio.wait_for(provider.ready.wait(),30)
            if args.provider=='windows':
                import sounddevice as sd
                # Output is a benchmark-only playback adapter, not a capture redesign.
                stream=sd.RawOutputStream(samplerate=rate,channels=1,dtype='int16')
                await asyncio.to_thread(stream.start)
                try:
                    await asyncio.sleep(1)
                    # The UIA source sometimes sends ready without an empty
                    # snapshot. Finish its history baseline BEFORE the new WAV,
                    # or the first recognized sentence would be treated as history.
                    provider.assembler.initial=False
                    record(dict(stage='audio_started',provider='windows',timestamp=monotonic()))
                    for offset in range(0,len(pcm),rate//10*2):
                        await asyncio.to_thread(stream.write,pcm[offset:offset+rate//10*2])
                    await asyncio.to_thread(stream.stop)
                finally: await asyncio.to_thread(stream.close)
                await asyncio.sleep(3+int(settings.endpoint_ms)/1000)
            else:
                stream_started=monotonic()
                record(dict(stage='audio_started',provider=provider.name,timestamp=stream_started))
                for offset in range(0,len(pcm),rate//50*2):
                    block=pcm[offset:offset+rate//50*2]
                    due=stream_started+(offset+len(block))/(rate*2)
                    await asyncio.sleep(max(0,due-monotonic()))
                    await session.push_audio(block,monotonic())
                # No synthetic padding: commit the exact shared WAV prefix at EOF.
                await asyncio.sleep(.15)
            await session.finish_file()
        async def run(translator):
            session=PipelineSession(provider,settings,translator,diagnostic=record,
                status=lambda message:None)
            await session.run(feed=feed)
        try:
            if args.translate:
                settings.validate()
                from openai import AsyncOpenAI
                async with AsyncOpenAI(api_key=settings.api_key,base_url=settings.base_url,
                        organization='',project='',timeout=10,max_retries=0) as client:
                    await run(TranslationProvider(client,settings))
            else:await run(None)
        except Exception as exc:
            from translation.asr.base import ASRError
            from translation.windows_source import SourceError
            record(dict(stage='error',provider=provider.name,
                message=str(exc) if isinstance(exc,(ASRError,SourceError,ValueError)) else
                '测试未完成：连接、音频或翻译失败；错误详情不记录以免泄露请求凭据。'))
        latest={};translated={}
        for row in records:
            identity=(row.get('epoch'),row.get('caption_id'))
            if row['stage']=='stable_final':latest[identity]=row['revision']
            elif row['stage']=='translated':translated[identity]=row['revision']
        translation_complete=not args.translate or bool(latest) and all(translated.get(k)==v for k,v in latest.items())
        complete=bool(latest) and not counts.get('error') and not counts.get('reset') and translation_complete
        record(dict(stage='summary',provider=provider.name,counts=dict(counts),
                    completed=complete,translation_complete=translation_complete,latest_sentence_count=len(latest),
                    continuous_stream=not bool(counts.get('reset')),evaluation='人工评估'))
    print(json.dumps(dict(output=str(args.output),counts=counts),ensure_ascii=False))
    return 0 if complete else 2

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--provider',choices=('windows','openai','deepgram'),required=True)
    parser.add_argument('--language',choices=('ko','en','ja','zh','auto'),default='ko')
    parser.add_argument('--windows-language-confirmed',choices=('ko','en','ja','zh','auto'))
    parser.add_argument('--translate',action='store_true')
    parser.add_argument('--mode',choices=['low','balanced','accurate'])
    parser.add_argument('--context',type=int,choices=range(6))
    parser.add_argument('--max-seconds',type=float,default=30)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.max_seconds<=120:parser.error('--max-seconds 必须为 1–120')
    try:return asyncio.run(benchmark(args))
    except (ValueError,OSError):
        print('测试无法开始：请检查 WAV 路径、格式及 Windows 语言确认参数。');return 2

if __name__=='__main__':raise SystemExit(main())
