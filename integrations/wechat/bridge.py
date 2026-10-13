"""Optional wxauto-only child process. No API keys, LLM, Memory, Qt or history crawling."""
import inspect,json,os,queue,sys,threading,time
from .config import WechatConfig
from .sender import group_matches,send_reply_checked
from .message_contract import serialize_message,VisibleBaseline

def read_field(obj,key,default=''):
    try:
        value=getattr(obj,key,default)
        return value if isinstance(value,(str,int,float,bool,type(None))) else default
    except Exception:return default

def run():
    protocol=sys.stdout;write_lock=threading.Lock();commands=queue.Queue(maxsize=64);stop=threading.Event()
    def emit(row):
        with write_lock:protocol.write(json.dumps(row,ensure_ascii=False)+'\n');protocol.flush()
    def reader():
        try:
            for line in sys.stdin:
                if len(line)>32000:continue
                try:row=json.loads(line)
                except ValueError:continue
                if not isinstance(row,dict):continue
                if row.get('action')=='stop':stop.set();return
                try:commands.put_nowait(row)
                except queue.Full:stop.set();return
        finally:stop.set()
    thread=threading.Thread(target=reader,daemon=True);thread.start()
    first=commands.get(timeout=10);config=WechatConfig.parse(first['config'])
    # Third-party diagnostics can contain message text; protocol output is explicit and content logging stays opt-in in parent.
    null=open(os.devnull,'w',encoding='utf-8');sys.stdout=null;sys.stderr=null
    from .diagnostics import configure_vendor,install_send_block,create_client
    from importlib.metadata import version
    configure_vendor(config)
    if config.dry_run:install_send_block()
    emit({'stage':'connecting'})
    wx=create_client()
    account=read_field(wx,'nickname') or ''
    account_id=account
    try:
        profile=wx.GetMyInfo()
        if isinstance(profile,dict):
            account=profile.get('昵称') or profile.get('nickname') or account
            account_id=profile.get('微信号') or profile.get('wxid') or profile.get('wechat_id') or account
    except Exception:pass
    if config.account_name and account!=config.account_name:
        emit({'stage':'error','error_type':'AccountMismatch','status':'blocked'});return
    chats={};ready=set();seen={};sequences={};state_lock=threading.RLock()
    def callback(msg,chat):
        try:
            name=read_field(chat,'who') or read_field(chat,'nickname')
            if name not in config.allowed_groups:return
            row=serialize_message(msg)
            with state_lock:
                if name not in ready:return
                try:
                    if not seen[name].accept(row):return
                except RuntimeError:
                    emit({'stage':'error','error_type':'MessageCacheLimit','status':'reconnect'});stop.set();return
                sequences[name]+=1;row['sequence']=sequences[name]
            row['chat_type']='group'
            emit({'stage':'message','group':name,'message':row})
        except Exception as exc:emit({'stage':'error','error_type':type(exc).__name__,'status':'message_ignored'})
    try:
        for name in config.allowed_groups:
            # Exact matching on the main selector, then verify the returned bound group's identity/type.
            emit({'stage':'binding'})
            wx.ChatWith(name,exact=True)
            if not group_matches(wx.ChatInfo(),name):raise RuntimeError('TargetGroupMismatch')
            chat=wx.AddListenChat(name,callback)
            if not callable(getattr(chat,'ChatInfo',None)) or not group_matches(chat.ChatInfo(),name):raise RuntimeError('ListenGroupUnavailable')
            current=chat.GetAllMessage()  # Only visible/current loaded window; never scroll or load historical pages.
            with state_lock:
                chats[name]=chat;seen[name]=VisibleBaseline();seen[name].establish(current);sequences[name]=0;ready.add(name)
            emit({'stage':'baseline','group':name,'count':len(current)})
        wx.StartListening()
        emit({'stage':'ready','groups':len(chats),'wxauto_version':version('wxauto4'),'account_matches_bot_name':account in config.bot_names,'account_hash':__import__('hashlib').sha256(str(account_id).encode()).hexdigest()[:24] if account_id else ''})
        heartbeat=time.monotonic()
        while not stop.is_set():
            try:command=commands.get(timeout=.5)
            except queue.Empty:command=None
            if command and command.get('action')=='send':
                name=command.get('group');text=command.get('text');request_id=command.get('request_id')
                if name not in chats or not isinstance(text,str) or not text.strip() or len(text)>16000:
                    emit({'stage':'send_result','request_id':request_id,'status':'rejected'});continue
                expected=command.get('sequence')
                allowed=lambda:not stop.is_set() and sequences.get(name)==expected
                result=send_reply_checked(chats[name],name,text,allowed,dry_run=config.dry_run)
                emit({'stage':'send_result','request_id':request_id,**result})
            if time.monotonic()-heartbeat>=2:
                heartbeat=time.monotonic();emit({'stage':'heartbeat'})
    finally:
        ready.clear()
        try:wx.StopListening()
        except Exception:pass

if __name__=='__main__':
    try:run()
    except Exception as exc:
        # sys.__stdout__ remains the protocol stream after vendor stdout redirection.
        reason='logged_in_window_missing' if '未找到已登录' in str(exc) else 'unsupported_client_version' if '版本' in str(exc) and '支持' in str(exc) else 'bridge_failed'
        sys.__stdout__.write(json.dumps({'stage':'error','error_type':type(exc).__name__,'status':reason})+'\n');sys.__stdout__.flush()
