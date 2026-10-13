"""Validate the exact bound group immediately before the external send."""

def group_matches(info,name):
    return isinstance(info,dict) and info.get('chat_type')=='group' and info.get('chat_name')==name

def split_reply(text,max_chars=1800):
    """Lossless splitting; separators stay in chunks and concatenation is exact."""
    if not isinstance(text,str):raise TypeError('reply_must_be_text')
    if type(max_chars) is not int or max_chars<1:raise ValueError('invalid_chunk_size')
    chunks=[]
    while len(text)>max_chars:
        end=0
        for separators in (('\n\n',),('\n',),('。','！','？','. ','! ','? ')):
            choices=[text.rfind(s,0,max_chars)+len(s) for s in separators if text.rfind(s,0,max_chars)>=0]
            if choices:
                end=max(choices)
                if end>=max_chars//3:break
                end=0
        end=end or max_chars
        chunks.append(text[:end]);text=text[end:]
    if text:chunks.append(text)
    return chunks


def send_checked(chat,name,text,allowed,*,dry_run=True):
    if not allowed():return 'cancelled'
    try:
        info=chat.ChatInfo()
        if not group_matches(info,name):return 'target_mismatch'
        if not allowed():return 'cancelled'
        if dry_run is not False:return 'dry_run'
        result=chat.SendMsg(text)  # Bound subwindow only; never fall back to current main-window recipient.
        if result is True:return 'sent'
        checker=getattr(result,'is_success',None)
        if callable(checker):return 'sent' if checker() else 'unknown'
        if isinstance(result,dict) and result.get('status')=='成功':return 'sent'
        return 'unknown'  # May have reached the recipient; an automatic retry could duplicate it.
    except Exception:return 'unknown'


def send_reply_checked(chat,name,text,allowed,*,dry_run=True):
    chunks=split_reply(text)
    completed=0
    for chunk in chunks:
        status=send_checked(chat,name,chunk,allowed,dry_run=dry_run)
        if status not in ('sent','dry_run'):return dict(status=status,chunks=len(chunks),completed=completed)
        completed+=1
    return dict(status='dry_run' if dry_run else 'sent',chunks=len(chunks),completed=completed)
