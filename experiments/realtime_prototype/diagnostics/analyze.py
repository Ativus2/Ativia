"""Read completed finite runs; derive metrics without rewriting raw evidence."""
from collections import Counter
import argparse
import hashlib,json,math,statistics
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'docs/quick-dialogue-diagnostic'
def read(name):return json.loads((OUT/name).read_text(encoding='utf-8'))
def stats(values):
    values=sorted(x for x in values if x is not None)
    return dict(n=len(values),mean=statistics.mean(values) if values else None,
        median=statistics.median(values) if values else None,p95=values[math.ceil(.95*len(values))-1] if values else None)
def at(row,event):return next((e['at'] for e in row.get('events',[]) if e['event']==event),None)
def norm(text):return ''.join(c for c in text if c.isalnum())
def seconds(value):return '未获得' if value is None else f'{value/1000:.3f}秒'
def llm_cost(record):
    usage=record.get('usage') or {};model=record.get('request_model')
    rates={'gpt-5.6-sol':(4,.4,20),'gpt-5.6-terra':(2,.2,12)}
    if not usage or model not in rates:return None
    a,b,c=rates[model];cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)
    return ((usage['prompt_tokens']-cached)*a+cached*b+usage['completion_tokens']*c)/1e6

def main():
    rows=read('voice/results.json');screen=read('llm-screen/results.json');preflight=read('stt-preflight.json')
    assert len(rows)==40,'final report requires all 40 attempts'
    summary={};calls=[dict(r,kind='llm',phase='screen') for r in screen]
    calls.append(dict(preflight,kind='stt_realtime',phase='preflight'))
    details=[]
    for r in rows:
        calls.extend(dict(c,phase='voice') for c in r.get('api_calls',[]))
        tts=[c for c in r.get('api_calls',[]) if c['kind']=='tts'];first=tts[0] if tts else {}
        # Conservative post-run text review, using recorded segment device clocks.
        # Raw rows and their original heuristic are never rewritten.
        filler={'嗯','好的','好','我看看','让我看看','稍等','稍等一下','收到','我想想','可以','好呀','当然','当然可以'}
        selected=next((c for c in tts if norm(c.get('text','')) not in filler),None)
        selected_event=next((e for e in r.get('events',[]) if e['event']=='segment_audible' and selected and e.get('segment')==selected['segment']),None)
        r['reviewed_first_meaningful_device_ms']=(selected_event['at']-at(r,'speech_end'))*1000 if selected_event else None
        pcm=first.get('times',{});device=at(r,'audible');meaningful=at(r,'meaningful_audible')
        stt=next((c for c in r.get('api_calls',[]) if c['kind'].startswith('stt_')),{});commit=at(stt,'commit_sent')
        deltas=[e['at'] for e in stt.get('events',[]) if e['event']=='transcript_delta']
        http_start=stt.get('times',{}).get('http_request');request_start=stt.get('times',{}).get('request_start')
        after_commit=((at(r,'stt_final')-commit)*1000 if commit and at(r,'stt_final') else None)
        details.append(dict(index=r['index'],stt=r['stt_adapter'],status=r['status'],
            first_pcm_before_response_end=bool(device is not None and pcm.get('response_end') and device<pcm['response_end']),
            first_pcm_before_all_tts_end=bool(device is not None and tts and device<max(c.get('times',{}).get('response_end',0) for c in tts)),
            audio_receipt_to_device_ms=(device-pcm['valid_pcm'])*1000 if device and pcm.get('valid_pcm') else None,
            stt_final_after_commit_ms=after_commit,
            stt_file_pre_http_ms=(http_start-request_start)*1000 if http_start and request_start else None,
            realtime_delta_before_speech_end=bool(deltas and min(deltas)<at(r,'speech_end')),
            realtime_delta_before_commit=bool(deltas and commit and min(deltas)<commit),
            first_append_before_speech_end=bool(at(stt,'audio_appended') and at(stt,'audio_appended')<at(r,'speech_end')),
            captured_samples=r.get('input_timing',{}).get('submitted_samples'),
            streamed_samples=r.get('input_timing',{}).get('streamed_samples'),
            first_tts_text=first.get('text'),meaningful_evidence_at=meaningful))
        details[-1].update(reviewed_first_meaningful_device_ms=r['reviewed_first_meaningful_device_ms'],
            selected_meaningful_segment=selected.get('segment') if selected else None,
            excluded_filler_segments=[c['segment'] for c in tts if selected and c['segment']<selected['segment']],
            semantic_review='conservative exact filler exclusion plus agent text inspection; not human listening')
    for variant in ['file','realtime']:
        group=[r for r in rows if r['stt_adapter']==variant];d=[x for x in details if x['stt']==variant]
        summary[variant]=dict(rounds=len(group),statuses=dict(Counter(r['status'] for r in group)),
            complete=sum(r['status']=='complete' for r in group),failed=sum(r['status']=='failed' for r in group),
            silent=sum(bool(r.get('silent_reply')) for r in group),user_cancelled=sum(r['status']=='cancelled' for r in group),
            missing_meaningful=sum(r.get('first_meaningful_audible_ms') is None and r['status']!='cancelled' for r in group),
            **{name:stats([r.get(name) for r in group]) for name in ['reviewed_first_meaningful_device_ms','first_meaningful_audible_ms','first_audible_ms',
                'speech_end_to_stt_final_ms','stt_final_to_first_token_ms','first_token_to_audio_ms','endpoint_ms']},
            browser_supply_gaps_ms=stats([gap for r in group for gap in r.get('browser_gaps_ms',[])]),
            rounds_with_supply_gap=sum(bool(r.get('browser_gaps_ms')) for r in group),
            device_before_first_tts_response_end=sum(x['first_pcm_before_response_end'] for x in d),
            device_before_all_tts_responses_end=sum(x['first_pcm_before_all_tts_end'] for x in d),
            audio_receipt_to_device_ms=stats([x['audio_receipt_to_device_ms'] for x in d]),
            stt_commit_to_final_ms=stats([x['stt_final_after_commit_ms'] for x in d]),
            gpu_peak_percent=max((r['gpu_peak_percent'] for r in group if r.get('gpu_peak_percent') is not None),default=None),
            vram_peak_mb=max((r['vram_peak_mb'] for r in group if r.get('vram_peak_mb') is not None),default=None),
            early_commit=sum(r.get('input_timing',{}).get('endpoint_detection_ms',0)<0 for r in group),
            false_interruption=None,human_double_talk='pending',subjective_quality='pending')
    count=Counter(c['kind'] for c in calls);count['models_list']=1
    llms=[c for c in calls if c['kind']=='llm'];live=[c for c in calls if c['kind']=='stt_realtime'];files=[c for c in calls if c['kind']=='stt_file']
    duration=lambda c:(c.get('usage') or {}).get('seconds',c.get('audio_seconds',0))
    cost=dict(llm_estimated_usd=sum(llm_cost(c) or 0 for c in llms),
        realtime_stt_estimated_usd=sum(duration(c) for c in live)/60*.017,
        whisper_stt_estimated_usd=sum(c.get('audio_seconds',0) for c in files)/60*.006,
        tts_estimated_usd=None,tts_reason='raw PCM response has no billed token usage; do not infer invoice from PCM bytes',
        llm_prompt_tokens=sum((c.get('usage') or {}).get('prompt_tokens',0) for c in llms),
        llm_completion_tokens=sum((c.get('usage') or {}).get('completion_tokens',0) for c in llms),
        llm_reasoning_tokens=sum(((c.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens',0) for c in llms),
        live_reported_or_audio_seconds=sum(duration(c) for c in live),whisper_submitted_seconds=sum(c.get('audio_seconds',0) for c in files),
        tts_generated_audio_seconds=sum(c.get('audio_seconds',0) for c in calls if c['kind']=='tts'),
        note='usage-based estimate, not an account bill; input cache-write charges unavailable; STT billing rounding may differ')
    llm_transport=dict(requests=len(llms),http_attempts=sum(c.get('http_attempts',0) for c in llms),
        new_tcp_requests=sum(any(e['event']=='transport:connection.connect_tcp.started' for e in c.get('events',[])) for c in llms),
        note='Use transport trace to corroborate reuse; Python object identity alone can be recycled. receive_response_body.failed after DONE may be generator close, not an API failure.')
    for key,value in summary.items():
        metric=value['reviewed_first_meaningful_device_ms'];value['development_goal_pass']=bool(metric['n']==20 and metric['mean']<=3000 and metric['p95']<=5000 and not value['failed'] and not value['silent'])
    result=dict(summary=summary,details=details,api_calls=dict(count),http_attempts=sum(c.get('http_attempts',0) for c in calls)+1,
        websocket_sessions=sum(c.get('ws_attempts',0) for c in live),cost=cost,llm_transport=llm_transport,
        boundaries='fixed-cloud, same Sol none on both arms; old Auto baseline not pooled; digital replay is not human external-speaker double-talk')
    (OUT/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    answers=['# LLM-only回答审阅（真人评价尚未完成）','同一问题与人格，各配置仅10次。请查看准确性、是否直接回答、自然程度及人格；速度不是质量证明。']
    for index in sorted({r['index'] for r in screen}):
        group=sorted([r for r in screen if r['index']==index],key=lambda r:r['config'])
        answers+=['',f'## {index}. {group[0]["prompt"]}']
        for r in group:answers+=['',f'### {r["config"]} · {r["request_model"]} · effort={r["reasoning_effort_sent"]}',r['answer']]
    (OUT/'LLM-ANSWERS.md').write_text('\n'.join(answers)+'\n',encoding='utf-8')
    prompts=json.loads((ROOT/'docs/realtime-benchmark/fixtures/prompts.json').read_text(encoding='utf-8'))
    transcript=['# 固定输入转写与回答审阅','保留原转写（含繁简体和标点差异）。音频边界审计不等于ASR无漏字；真人判断未完成。']
    for r in rows:transcript+=['',f'## {r["index"]} · {r["stt_adapter"]}',f'原句：{prompts[r["index"]-1]}',f'转写：{r.get("transcript")}',f'回答：{r.get("reply")}',f'分句：{[c.get("text") for c in r.get("api_calls",[]) if c["kind"]=="tts" ]}']
    (OUT/'VOICE-ANSWERS.md').write_text('\n'.join(transcript)+'\n',encoding='utf-8')
    print(json.dumps(result['summary'],ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=OUT);args=parser.parse_args()
    OUT=args.output;main()
