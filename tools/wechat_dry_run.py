"""Real wxauto -> shared core/personality/model -> shadow memory and local preview.

Only explicitly listed test messages enter the model/contract file in this acceptance
runner. Regular production UI retains its normal policy and privacy configuration.
"""
import argparse,hashlib,json,sys,time,tempfile,threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
TEST_TEXTS=('你好','@Ativia 你好','@Ativia 测试一下','这个呢','连续测试1','连续测试2','@Ativia 我不吃香菜')


def acceptance_config(raw):
    """Validate a WechatConfig document, never interpret a recovery manifest."""
    from integrations.wechat.config import WechatConfig
    if not isinstance(raw,dict):raise ValueError('acceptance_requires_wechat_config_document')
    for section in (raw,raw.get('wechat')):
        if isinstance(section,dict) and any(key in section for key in ('transport','model_enabled','send_enabled','voice_enabled')):
            raise ValueError('recovery_manifest_is_preflight_only_not_acceptance_config')
    if not isinstance(raw.get('wechat'),dict):raise ValueError('acceptance_requires_wechat_config_document')
    config=replace(WechatConfig.parse(raw),enabled=True,dry_run=True,proactive_enabled=False,debug=False)
    config=WechatConfig.parse(config.document())
    if config.allowed_groups!=('Ativia测试群',):raise ValueError('acceptance_requires_exact_target_group')
    return config


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value not in ('',None) else ''


def redact_sample(group,row):
    """Evidence contains hashes/lengths and metadata, never text or quote bodies."""
    text=row.get('content','');quote=row.get('quote_content','')
    sample={k:row.get(k) for k in ('attr','type','timestamp','timestamp_source','field_types','source_timestamp_available')}
    sample.update(group=group,sender_name='member-'+fingerprint(row.get('sender'))[:8],
        sender_id_hash=fingerprint(row.get('sender_id')),message_id_hash=fingerprint(row.get('id')),
        is_self=row.get('attr')=='self',content_hash=fingerprint(text),content_length=len(text) if isinstance(text,str) else 0,
        quoted_message=dict(sender_hash=fingerprint(row.get('quote_nickname')),
            content_hash=fingerprint(quote),content_length=len(quote) if isinstance(quote,str) else 0,
            present=bool(row.get('quote_nickname') or quote)))
    return sample


def create_shadow_runtime(root):
    """A fresh namespace each run; never load or copy the formal memory database."""
    from companion_runtime import CompanionRuntime
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    directory=Path(tempfile.mkdtemp(prefix='run-',dir=root))
    return CompanionRuntime(directory/'companion.json')


class BudgetedConversation:
    """Conservative admission budget for the existing one-attempt model call.

    vision.respond_to_voice uses OpenAI(max_retries=0) and exactly one create.
    An admission may fail before HTTP; counts are upper bounds, not billed usage.
    """
    def __init__(self,core,limit=6):
        if type(limit) is not int or not 1<=limit<=6:raise ValueError('invalid_model_budget')
        self.core=core;self.limit=limit;self.calls=0;self.blocked_reason='';self.lock=threading.Lock()
    @property
    def exhausted(self):return self.calls>=self.limit
    def handle(self,event,events,**kwargs):
        if kwargs['cancelled']() or not kwargs['ticket'].valid():return ''
        with self.lock:
            if self.blocked_reason:raise RuntimeError(self.blocked_reason)
            if self.exhausted:raise RuntimeError('recovery_model_budget_exhausted')
            self.calls+=1
        try:return self.core.handle(event,events,**kwargs)
        except Exception as exc:
            current=exc;seen=set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                if getattr(current,'status_code',None) in (401,403) or type(current).__name__ in ('AuthenticationError','PermissionDeniedError'):
                    self.blocked_reason='model_authentication_failed';break
                current=current.__cause__ or current.__context__
            raise


def main():
    from dotenv import load_dotenv
    from companion_runtime import CompanionRuntime
    from assistant_pause import PauseGate
    from performance.scheduler import TaskScheduler
    from runtime_settings import SettingsStore
    from integrations.wechat.adapter import WechatAdapter
    from integrations.wechat.service import WechatService
    from integrations.wechat.config import WechatConfigStore
    from integrations.wechat.logging import WechatLog
    from conversation_core import AtiviaConversation
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=120)
    parser.add_argument('--show-text',action='store_true',help='Deprecated: acceptance evidence never prints message or reply bodies')
    parser.add_argument('--test-text',action='append',default=[])
    parser.add_argument('--paused',action='store_true',help='Verify PauseGate without connecting')
    parser.add_argument('--config',type=Path,help='Read-only {wechat: ...} config; recovery.dryrun.json is preflight-only and rejected')
    parser.add_argument('--shadow-root',type=Path,default=ROOT/'runtime/wechat-recovery/shadow')
    parser.add_argument('--model-budget',type=int,choices=range(1,7),default=6)
    parser.add_argument('--output',type=Path,default=ROOT/'benchmark/wechat-recovery/acceptance'/(uuid4().hex+'.json'))
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Refusing to overwrite existing acceptance evidence')
    config_path=args.config or WechatConfigStore().path
    try:config=acceptance_config(json.loads(config_path.read_text(encoding='utf-8-sig')))
    except (OSError,ValueError) as exc:raise SystemExit('acceptance_config_rejected: '+str(exc))
    # Do not create a second bot/model coordinator against the same account/Memory DB.
    from tools.wechat import rpc
    try:rpc({'action':'wechat-status'})
    except RuntimeError as exc:
        if '未运行' not in str(exc):raise SystemExit('Existing Ativia status unknown; use its WeChat UI')
    else:raise SystemExit('Ativia is already running; use its WeChat Dry Run UI or close it before this acceptance runner')
    load_dotenv(ROOT/'.env')
    allowed=set(TEST_TEXTS+tuple(args.test_text));samples=[];stages=[];previews=[]
    def emit(row):print(json.dumps(row,ensure_ascii=False),flush=True)
    class CapturingAdapter(WechatAdapter):
        def receive_event(self):
            admitted=[]
            for packet in super().receive_event():
                if packet.get('stage')=='message':
                    row=packet.get('message',{})
                    if packet.get('group')!='Ativia测试群' or row.get('content') not in allowed:continue
                    sample=redact_sample(packet['group'],row)
                    samples.append(sample);emit(dict(stage='real_test_message',**sample))
                elif packet.get('stage') not in ('heartbeat',):
                    safe={k:v for k,v in packet.items() if k in ('stage','status','error_type','groups','count','wxauto_version','chunks','completed','account_hash')}
                    stages.append(safe);emit(safe)
                admitted.append(packet)
            return admitted
    runtime=create_shadow_runtime(args.shadow_root);runtime.memory.ready.wait(5)
    gate=PauseGate(args.paused);scheduler=TaskScheduler()
    settings=SettingsStore().load();budget=BudgetedConversation(AtiviaConversation(runtime,settings),args.model_budget)
    service=WechatService(runtime,gate,scheduler,settings,config,adapter_factory=CapturingAdapter,
        core=budget,require_stable_memory=True,
        log=WechatLog(runtime.path.parent/'logs',False,enabled=True))
    began=time.monotonic();last_preview=None;service.start()
    emit(dict(stage='acceptance_started',dry_run=True,model_budget=budget.limit,shadow_memory=True,
        test_message_count=len(allowed),test_message_hashes=sorted(fingerprint(text) for text in allowed),
        note='Local preview only. Wait for listener ready and baseline before manually creating any test event.'))
    try:
        while time.monotonic()-began<max(3,min(600,args.seconds)):
            preview=service.preview
            if preview is not None and preview is not last_preview:
                last_preview=preview
                safe={k:v for k,v in preview.items() if k not in ('sender','input','reply','chunks')}
                safe['sender_name']='member-'+fingerprint(preview['sender'])[:8]
                safe['would_send_messages']=len(preview['chunks'])
                previews.append(safe);emit(dict(stage='Wechat Dry Run',**safe))
            if service.status in ('logged_in_window_missing','unsupported_client_version','bridge_failed') and not service.connected:break
            if budget.blocked_reason or (budget.exhausted and service.job is None):break
            time.sleep(.1)
    except KeyboardInterrupt:pass
    finally:
        gate.transition('PAUSING');service.close()
        deadline=time.monotonic()+8
        while not service.closed_finished() and time.monotonic()<deadline:time.sleep(.05)
        runtime.close();runtime.memory.close_future.result(5)
        result=dict(kind='real wxauto acceptance; no synthetic events',status='PASS' if previews else 'NOT VERIFIED',
            real_read=bool(samples),real_model=bool(previews),dry_run=bool(previews),
            real_SendMsg_calls=0,send_block='local preview sink; dry-run send IPC rejected; child SendMsg guard',
            model_admissions=budget.calls,model_attempts_upper_bound=budget.calls,model_budget=budget.limit,
            model_stop_reason=budget.blocked_reason or ('budget_exhausted' if budget.exhausted else ''),
            shadow_memory=str(runtime.memory.path),formal_memory_opened=False,
            samples=samples,stages=stages,previews=previews,counts=service.counts,
            closed=service.closed_finished(),duration_seconds=round(time.monotonic()-began,2),
            unverified_cases=[name for name in ('ordinary','mention','self','quote','consecutive')])
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        emit({k:v for k,v in result.items() if k not in ('samples','stages','previews')})
    return 0 if previews else 2


if __name__=='__main__':raise SystemExit(main())

