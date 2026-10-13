"""Read-only process/window evidence. No chat text or credentials in diagnostics."""
import ctypes,hashlib,importlib.metadata,inspect,os,platform,re


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value not in ('',None) else ''


def environment():
    import psutil,win32gui,win32process,win32api,win32security,win32con
    def integrity(pid):
        process=token=None
        try:
            process=win32api.OpenProcess(0x1000,False,pid)
            token=win32security.OpenProcessToken(process,win32con.TOKEN_QUERY)
            sid=win32security.GetTokenInformation(token,win32security.TokenIntegrityLevel)
            if isinstance(sid,tuple):sid=sid[0]
            level=sid.GetSubAuthority(sid.GetSubAuthorityCount()-1)
            return {0x1000:'low',0x2000:'medium',0x3000:'high',0x4000:'system'}.get(level,str(level))
        except Exception as exc:return type(exc).__name__
        finally:
            if token:token.Close()
            if process:process.Close()
    processes=[]
    for p in psutil.process_iter(['pid','name']):
        if (p.info['name'] or '').lower() not in ('wechat.exe','weixin.exe'):continue
        row=dict(pid=p.pid,name=p.info['name'],integrity=integrity(p.pid))
        try:
            info=win32api.GetFileVersionInfo(p.exe(),'\\')
            hi,lo=info['FileVersionMS'],info['FileVersionLS']
            row['version']='.'.join(str(x) for x in (hi>>16,hi&65535,lo>>16,lo&65535))
        except Exception as exc:row['version_error']=type(exc).__name__
        processes.append(row)
    pids={p['pid'] for p in processes};windows=[]
    def visit(hwnd,_):
        _,pid=win32process.GetWindowThreadProcessId(hwnd)
        if pid not in pids:return
        rect=win32gui.GetWindowRect(hwnd)
        title=win32gui.GetWindowText(hwnd)
        windows.append(dict(hwnd=hwnd,pid=pid,title=title if title in ('微信','WeChat','Weixin','') else '[other WeChat window]',
            class_name=win32gui.GetClassName(hwnd),rect=rect,visible=bool(win32gui.IsWindowVisible(hwnd)),
            minimized=bool(win32gui.IsIconic(hwnd)),owner=win32gui.GetWindow(hwnd,4)))
    win32gui.EnumWindows(visit,None)
    return dict(python=platform.python_version(),wxauto4=importlib.metadata.version('wxauto4'),
                python_integrity=integrity(os.getpid()),processes=processes,windows=windows)


def configure_vendor(config):
    from wxauto4 import WxParam
    WxParam.ENABLE_FILE_LOGGER=False
    WxParam.TELEMETRY_ENABLED=False
    WxParam.LISTEN_INTERVAL=max(1,config.poll_seconds)
    try:
        from wxauto4.logger import wxlog
        wxlog.set_debug(False)
    except Exception:pass


def install_send_block():
    """Defense in depth for diagnostic/dry-run children, before constructing WeChat."""
    from wxauto4.wx import Chat,WeChat
    counter={'blocked_attempts':0,'real_calls':0}
    def blocked(*args,**kwargs):
        counter['blocked_attempts']+=1
        raise RuntimeError('DRY_RUN_SENDMSG_BLOCKED')
    for cls in (Chat,WeChat):
        setattr(cls,'SendMsg',blocked)
    if Chat.SendMsg is not blocked or WeChat.SendMsg is not blocked:
        raise RuntimeError('SendBlockInstallationFailed')
    return counter


def create_client(hwnd=None):
    from wxauto4 import WeChat
    kwargs=dict(start_listener=False,debug=False,resize=False,ads=False)
    if hwnd is not None:kwargs['hwnd']=hwnd
    signature=inspect.signature(WeChat.__init__)
    if not any(p.kind==p.VAR_KEYWORD for p in signature.parameters.values()):
        kwargs={k:v for k,v in kwargs.items() if k in signature.parameters}
    return WeChat(**kwargs)


def failure_reason(exc):
    # Original Chinese error is emitted only if it matches known non-content errors.
    value=str(exc)
    if '未找到已登录' in value:
        return dict(status='logged_in_window_missing',error_type=type(exc).__name__,
                    error_origin='wxauto4.WeChat',raw_error='未找到已登录的客户端主窗口')
    if isinstance(exc,AttributeError) and re.fullmatch(r"'[A-Za-z0-9_.]+' object has no attribute '[A-Za-z0-9_]+'",value):
        return dict(status='vendor_ui_binding_failed',error_type='AttributeError',raw_error=value)
    return dict(status='bridge_failed',error_type=type(exc).__name__,raw_error='details_omitted')


def restore_main(evidence):
    import win32gui,win32con
    candidates=[w for w in evidence['windows'] if w['title'] in ('微信','WeChat','Weixin') and w['visible'] and not w['owner']]
    if len(candidates)!=1:raise RuntimeError('AmbiguousMainWindow')
    window=candidates[0]
    if window['minimized']:win32gui.ShowWindow(window['hwnd'],win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(window['hwnd'])
    return window['hwnd']


def uia_evidence(hwnd):
    from wxauto4 import uia
    from wxauto4.ui import WeChatMainWnd
    root=uia.ControlFromHandle(hwnd);stack=[(root,0)];nodes=[]
    while stack and len(nodes)<100:
        control,depth=stack.pop()
        # Control names/values deliberately omitted: they may contain chat bodies.
        nodes.append(dict(kind=control.ControlTypeName,class_name=control.ClassName,depth=depth))
        if depth<5:stack.extend((child,depth+1) for child in control.GetChildren())
    return dict(nodes=nodes,expected_ui_class=getattr(WeChatMainWnd,'_ui_cls_name',None),
                expected_win_class=getattr(WeChatMainWnd,'_win_cls_name',None))
