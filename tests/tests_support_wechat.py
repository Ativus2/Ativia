import time
from uuid import uuid4
from chat_events import ChatEvent

def make_event(text='hello',person='A',group='g',timestamp=None,**kwargs):
    return ChatEvent('wechat','group',group,person,person,text,time.time() if timestamp is None else timestamp,uuid4().hex,**kwargs)
