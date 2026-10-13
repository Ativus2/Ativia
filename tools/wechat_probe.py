"""Read-only authorized whitelist probe. No model, no send, no memory writes."""
import sys,json,time,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
def main():
    from integrations.wechat.adapter import WechatAdapter
    from integrations.wechat.config import WechatConfigStore
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--seconds',type=int,default=15);parser.add_argument('--output',type=Path,default=ROOT/'docs/wechat-integration/benchmark/real-listener-probe.json');args=parser.parse_args()
    config=WechatConfigStore().load()
    if not config.allowed_groups:raise SystemExit('请先在设置中填写白名单群。')
    adapter=WechatAdapter(config);events=[];start=time.monotonic()
    try:
        adapter.start()
        while time.monotonic()-start<max(3,min(60,args.seconds)):
            for row in adapter.receive_event():
                safe={k:v for k,v in row.items() if k in ('stage','groups','wxauto_version','error_type','status')}
                if row.get('stage')=='message':safe={'stage':'message','text_logged':False}
                safe['elapsed_ms']=round((time.monotonic()-start)*1000);events.append(safe);print(json.dumps(safe,ensure_ascii=False),flush=True)
            if not adapter.alive():break
            time.sleep(.2)
    finally:
        adapter.close();args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(dict(kind='real wxauto read-only probe',model_called=False,sends=0,memory_writes=0,events=events,closed=not adapter.alive()),ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
