"""Fixed opt-in real requests and labeled local memory checks; safe failure output."""
import argparse,json,os,sys,tempfile,time,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))

def save(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')

def cloud(out):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from companion_runtime import CompanionRuntime
    from runtime_settings import SettingsStore
    from vision import respond_to_voice
    from uuid import uuid4
    cases=json.loads((ROOT/'tests/real_use_benchmark/personality-prompts.json').read_text(encoding='utf-8'))
    rows=[];settings=SettingsStore().load()
    with tempfile.TemporaryDirectory() as directory:
        runtime=CompanionRuntime(Path(directory)/'companion.json');runtime.memory.ready.wait(3)
        runtime.remember_input('以后我玩熟悉的游戏时，不要主动教我怎么玩。')
        runtime.memory.submit(lambda:None).result(3)
        for case in cases:
            metrics={'request':uuid4().hex};began=time.monotonic();deltas=[]
            runtime.set_mode(case['mode'],persist=False)
            context=runtime.context(case['text'],legacy_mode='learning' if case['mode']=='CLASS' else 'entertainment')
            try:
                answer=respond_to_voice(case['text'],'','[]',settings,metrics,lambda:False,
                    'learning' if case['mode']=='CLASS' else 'entertainment',companion_context=context,on_delta=deltas.append)
                row=dict(**case,reply=answer,success=True,delta_count=len(deltas),metrics=metrics,
                    memory_ids=list(runtime.last_recall.ids) if runtime.last_recall else [],human_rating=None)
            except Exception as exc:
                row=dict(**case,success=False,error_type=type(exc).__name__,status_code=getattr(exc,'status_code',None),human_rating=None)
            row['elapsed_ms']=(time.monotonic()-began)*1000;rows.append(row);save(out/'personality-cloud.json',rows)
            print(json.dumps(dict(case=case['id'],success=row['success'],ttft_ms=metrics.get('llm_first_token_ms')),ensure_ascii=False),flush=True)
        runtime.close();runtime.memory.close_future.result(5)
    lat=[r['metrics']['llm_first_token_ms'] for r in rows if r['success'] and r['metrics'].get('llm_first_token_ms') is not None]
    save(out/'cloud-summary.json',dict(requested=len(cases),success=sum(r['success'] for r in rows),model=os.getenv('OPENAI_MODEL','gpt-5.6-terra'),
        ttft_median_ms=statistics.median(lat) if lat else None,ttft_max_ms=max(lat) if lat else None,
        human_reviews=0,baseline='No reliable baseline available.',scope='Cloud LLM only; no microphone, speaker, screen, or live game.'))
    lines=['# 30 条真实模型回复 · 待人工评审','', '请逐条填：自然短回复 / 多余追问 / 客服措辞 / 未请求教学 / 错误记忆 / 备注。空白不算评分。','']
    for r in rows:
        lines += ['## '+str(r['id'])+' · '+r['mode'],'',r['text'],'',r.get('reply','请求失败：'+r.get('error_type','')),'',
            '- 人工结果：未评审','']
    (out/'human-review.md').write_text('\n'.join(lines),encoding='utf-8')
    return rows

def memory(out):
    from test_iteration4_memory import SAVE_CASES,IGNORE_CASES
    from memory.gate import MemoryGate
    from memory.store import MemoryStore
    from memory.retrieval import retrieve
    gate=MemoryGate();rows=[]
    for text,expected in [(t,True) for t in SAVE_CASES]+[(t,False) for t in IGNORE_CASES]:
        decision=gate.judge(text);saved=decision.classification in ('TEMPORARY','LONG_TERM','PERMANENT')
        rows.append(dict(text=text,expected_save=expected,actual=decision.classification,correct=saved==expected))
    with tempfile.TemporaryDirectory() as directory:
        store=MemoryStore(Path(directory)/'memory.sqlite3')
        for t in ('我擅长 Python','我不擅长韩语听力','我偏好较年轻的音色','以后玩熟悉游戏不要主动教我'):store.save(gate.judge(t))
        began=time.perf_counter();recall=retrieve(tuple(store.rows()),'这个任务怎么打','GAME','helldivers2.exe')
        recall_ms=(time.perf_counter()-began)*1000
        selected=[r['content'] for r in store.rows() if r['id'] in recall.ids]
    result=dict(samples=rows,correct_save=sum(r['correct'] and r['expected_save'] for r in rows),
        correct_ignore=sum(r['correct'] and not r['expected_save'] for r in rows),
        recall_context='GAME / helldivers2.exe',retrieved=selected,recall_ms=recall_ms,
        irrelevant_retrieved=sum(any(s in text for s in ('Python','韩语','音色')) for text in selected),
        note='40 curated labeled contract samples + one 4-record recall set; not population accuracy.')
    save(out/'memory-benchmark.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('samples','retrieved')},ensure_ascii=False))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cloud',action='store_true');parser.add_argument('--memory',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/iteration4/benchmark');args=parser.parse_args()
    if args.memory:memory(args.output)
    if args.cloud:cloud(args.output)
    if not args.cloud and not args.memory:parser.error('Choose --memory or --cloud')
