"""Small in-memory group context and bounded deduplication, never a history crawler."""
from collections import deque,OrderedDict
from dataclasses import replace

class GroupContextBuffer:
    def __init__(self,limit=40,seconds=900):
        self.rows=deque(maxlen=limit);self.seconds=seconds;self.version=0;self.seen=OrderedDict()
        self.last_bot_at=0;self.last_addressed_sender='';self.source_sequence=0
    def append(self,event,now):
        self.expire(now)
        if event.message_id in self.seen:return False
        self.seen[event.message_id]=now
        while len(self.seen)>2048:self.seen.popitem(last=False)
        self.rows.append(event);self.version+=1;self.source_sequence=max(self.source_sequence,event.source_sequence)
        return True
    def expire(self,now):
        while self.rows and now-self.rows[0].timestamp>self.seconds:self.rows.popleft()
        while self.seen and now-next(iter(self.seen.values()))>3600:self.seen.popitem(last=False)
    def snapshot(self,now):self.expire(now);return tuple(self.rows)
    def sent(self,event,text,now):
        self.last_bot_at=now;self.last_addressed_sender=event.sender_id
        self.rows.append(replace(event,sender_id='ativia',sender_name='Ativia',text=text,timestamp=now,
            message_id='reply:'+event.message_id,is_at_me=False,is_reply_to_me=False,reply_to_name='',reply_to_text=''))
        self.version+=1
