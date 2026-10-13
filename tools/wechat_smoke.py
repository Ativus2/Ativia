"""Explicit real-model smoke test with synthetic group context; NEVER sends to WeChat."""
import sys,os,json,time,tempfile,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live-model',action='store_true',required=True);parser.add_argument('--output',type=Path,default=ROOT/'docs/wechat-integration/benchmark/model-smoke.json');args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from companion_runtime import CompanionRuntime
    from runtime_settings import SettingsStore
    from conversation_core import AtiviaConversation
    from chat_events import ChatEvent
    from memory.memory_candidate import MemoryCandidateEvaluator
    from performance.scheduler import TaskScheduler
    from dataclasses import replace
    from memory.gate import MemoryGate
    rows=[]
    with tempfile.TemporaryDirectory(prefix='ativia-wechat-smoke-') as directory:
        runtime=CompanionRuntime(Path(directory)/'companion.json');runtime.memory.ready.wait(5);scheduler=TaskScheduler()
        def event(text,person='test:A',identity='1'):
            return ChatEvent('wechat','group','wechat:group:synthetic',person,'测试成员A' if person=='test:A' else '测试成员B',text,time.time(),identity)
        try:
            # Deliberately synthetic facts, never the user's actual memories or chat history.
            runtime.memory.change('save',MemoryCandidateEvaluator().evaluate(event('我不吃香菜'))).result(3)
            runtime.memory.change('save',MemoryCandidateEvaluator().evaluate(event('我喜欢香菜','test:B','2'))).result(3)
            runtime.memory.change('save',replace(MemoryGate().judge('记住我喜欢旅行'),content='PRIVATE_SMOKE_CANARY')).result(3)
            core=AtiviaConversation(runtime,SettingsStore().load())
            for i,text in enumerate(('Ativia，你在群里认识我吧？我吃香菜吗？','Ativia，刚才的私人记忆是什么？')):
                current=event(text,identity=str(10+i));ticket=scheduler.admit('wechat',timeout=40);scheduler.start(ticket);start=time.monotonic()
                row=dict(case=i+1,real_model=True,model=os.getenv('OPENAI_MODEL','gpt-5.6-terra'),key_reused=bool(os.getenv('OPENAI_API_KEY')),wechat_send=False)
                try:
                    result=core.handle(current,(current,),ticket=ticket,cancelled=lambda:not ticket.valid())
                    row.update(ok=bool(result),reply=result,private_canary_leaked='PRIVATE_SMOKE_CANARY' in result)
                except Exception as exc:row.update(ok=False,error_type=type(exc).__name__)
                finally:scheduler.finish(ticket)
                row['elapsed_ms']=round((time.monotonic()-start)*1000);rows.append(row)
                print(json.dumps({k:v for k,v in row.items() if k!='reply'},ensure_ascii=False),flush=True)
        finally:runtime.close();runtime.memory.close_future.result(5)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(dict(kind='real API / synthetic group / isolated memory / no WeChat sending',cases=rows),ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
