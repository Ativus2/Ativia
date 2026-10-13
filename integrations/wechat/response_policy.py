"""Local group participation decisions; no decision-model API calls."""
from collections import deque
from dataclasses import dataclass
import random,re
from .message_parser import mentioned

@dataclass(frozen=True)
class ResponseDecision:
    respond:bool
    reason:str
    strong:bool=False

class GroupResponsePolicy:
    def __init__(self,config,random_value=None):
        self.config=config;self.random_value=random_value or random.random
        self.last_proactive=-1e12;self.consecutive=0;self.calls=deque();self.last_sent=0
    def decide(self,event,buffer,now,*,familiarity=.1,relevant_memory=False,aliases=()):
        names=self.config.bot_names+tuple(aliases)
        direct=event.is_at_me or event.is_reply_to_me or mentioned(event.text,names)
        followup=(now-buffer.last_bot_at<60 and buffer.last_addressed_sender==event.sender_id
            and bool(re.search(r'[?？]|为什么|什么意思|你觉得|怎么|还记得|能不能',event.text)))
        if direct or followup:return ResponseDecision(True,'direct_mention' if event.is_at_me else 'reply_to_ativia' if event.is_reply_to_me else 'direct_question',True)
        return self.should_join_conversation(event,buffer,now,familiarity,relevant_memory)
    def should_join_conversation(self,event,buffer,now,familiarity=.1,relevant_memory=False):
        if not self.config.proactive_enabled:return ResponseDecision(False,'proactive_disabled')
        if now-self.last_sent>1800:self.consecutive=0
        if now-self.last_proactive<self.config.proactive_cooldown or self.consecutive>=self.config.max_consecutive_messages:
            return ResponseDecision(False,'proactive_cooldown')
        active={e.sender_id for e in buffer.snapshot(now) if e.sender_id!='ativia' and now-e.timestamp<=180}
        if len(active)<3:return ResponseDecision(False,'two_person_or_quiet_conversation')
        if re.search(r'私聊|秘密|别插话|别说话|别打扰|吵架|分手|失恋|自杀|想死|密码|病历|诊断|银行卡',event.text):
            return ResponseDecision(False,'sensitive_or_private_topic')
        interesting=bool(re.search(r'老师|拖堂|考试|游戏|笑死|搞笑|终于|烤肉|交换|聚餐|一起',event.text))
        if not (interesting or relevant_memory):return ResponseDecision(False,'no_interesting_context')
        chance=min(.3,self.config.proactive_probability+min(.08,familiarity*.08)+(.04 if relevant_memory else 0))
        return ResponseDecision(self.random_value()<chance,'occasional_group_participation')
    def admit_call(self,now):
        while self.calls and now-self.calls[0]>=60:self.calls.popleft()
        if len(self.calls)>=self.config.max_calls_per_minute:return False
        self.calls.append(now);return True
    def sent(self,decision,now):
        self.last_sent=now
        if decision.strong:self.consecutive=0
        else:self.last_proactive=now;self.consecutive+=1
