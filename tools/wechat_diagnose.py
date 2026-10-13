"""Bounded, send-blocked real wxauto4 diagnostic. No model calls or message bodies."""
import argparse,contextlib,io,json,os,subprocess,sys,time,traceback
from dataclasses import replace
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def worker(args):
    from integrations.wechat.config import WechatConfigStore
    from integrations.wechat.diagnostics import environment,configure_vendor,install_send_block,create_client,failure_reason,fingerprint,restore_main,uia_evidence
    from integrations.wechat.sender import group_matches
    config=replace(WechatConfigStore().load(),dry_run=True)
    emit=lambda **r:print(json.dumps(r,ensure_ascii=False),flush=True)
    evidence=environment();emit(stage='environment',**evidence)
    if args.group not in config.allowed_groups:emit(stage='error',status='group_not_allowlisted');return
    if args.hwnd is not None and not any(w['hwnd']==args.hwnd and w['visible'] and not w['owner'] and w['title'] in ('微信','WeChat','Weixin') for w in evidence['windows']):
        emit(stage='error',status='hwnd_not_a_visible_wechat_window');return
    # Bound vendor stdout/stderr by discarding, not storing unbounded/private messages.
    with open(os.devnull,'w') as null,contextlib.redirect_stdout(null),contextlib.redirect_stderr(null):
        configure_vendor(config);counter=install_send_block()
    wx=None
    try:
        if args.restore:
            hwnd=restore_main(evidence);time.sleep(.5)
            emit(stage='restored_environment',**environment())
            emit(stage='uia',**uia_evidence(hwnd))
        emit(stage='WeChat()',status='STARTING',hwnd_override=args.hwnd)
        with open(os.devnull,'w') as null,contextlib.redirect_stdout(null),contextlib.redirect_stderr(null):
            wx=create_client(args.hwnd)
        emit(stage='WeChat()',status='PASS')
        with open(os.devnull,'w') as null,contextlib.redirect_stdout(null),contextlib.redirect_stderr(null):
            profile=wx.GetMyInfo()
        if not isinstance(profile,dict) or not profile:raise RuntimeError('AccountInfoUnavailable')
        emit(stage='GetMyInfo()',status='PASS',fields=list(profile),account_hash=fingerprint(profile.get('微信号') or profile.get('wxid') or profile.get('昵称')))
        with open(os.devnull,'w') as null,contextlib.redirect_stdout(null),contextlib.redirect_stderr(null):
            wx.ChatWith(args.group,exact=True);info=wx.ChatInfo()
        emit(stage='ChatWith()/ChatInfo()',status='PASS' if group_matches(info,args.group) else 'FAIL',
             chat_type=info.get('chat_type') if isinstance(info,dict) else None,target_matches=group_matches(info,args.group))
        if not group_matches(info,args.group):return
        emit(stage='AddListenChat()',status='NOT TESTED',next='Run wechat_dry_run.py for baseline and callback acceptance')
    except Exception as exc:
        # Actual frames only: no locals, source lines, or chat-bearing exception strings.
        frames=[dict(file=frame.filename.replace('\\','/').split('site-packages/')[-1],line=frame.lineno,function=frame.name)
                for frame in traceback.extract_tb(exc.__traceback__)]
        emit(stage='error',**failure_reason(exc),traceback=frames)
    finally:
        if wx:
            with open(os.devnull,'w') as null,contextlib.redirect_stdout(null),contextlib.redirect_stderr(null):
                try:wx.StopListening()
                except Exception:pass
        emit(stage='send_statistics',**counter)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',default='Ativia测试群');p.add_argument('--hwnd',type=int);p.add_argument('--restore',action='store_true')
    p.add_argument('--timeout',type=int,default=30);p.add_argument('--worker',action='store_true')
    p.add_argument('--output',type=Path,default=ROOT/'docs/wechat-integration/benchmark/diagnostic-20260928.json')
    args=p.parse_args()
    if args.worker:return worker(args)
    cmd=[sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--worker','--group',args.group]
    if args.hwnd:cmd+=['--hwnd',str(args.hwnd)]
    if args.restore:cmd+=['--restore']
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('KEY','TOKEN','SECRET','COOKIE'))}
    child=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:out,err=child.communicate(timeout=max(5,min(60,args.timeout)))
    except subprocess.TimeoutExpired:
        child.kill();out,err=child.communicate();out+='\n'+json.dumps({'stage':'error','status':'diagnostic_timeout'})
    rows=[]
    for line in out.splitlines():
        try:rows.append(json.loads(line))
        except ValueError:pass
    if child.returncode and not any(r.get('stage')=='error' for r in rows):rows.append(dict(stage='error',status='diagnostic_worker_failed',exit_code=child.returncode))
    checks={name:'NOT TESTED' for name in ('WeChat()','GetMyInfo()','ChatWith()/ChatInfo()','AddListenChat()','Message callback')}
    for row in rows:
        if row.get('stage') in checks:checks[row['stage']]=row.get('status','NOT TESTED')
    if checks['WeChat()']=='STARTING':checks['WeChat()']='FAIL'
    result=dict(kind='real read-only wxauto diagnostic',send_enabled=False,checks=checks,events=rows)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 2 if any(r.get('stage')=='error' or r.get('status')=='FAIL' for r in rows) else 0


if __name__=='__main__':raise SystemExit(main())
