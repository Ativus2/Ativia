"""Conservative group candidates and delayed consolidation, sharing MemoryStore."""
from dataclasses import replace
from collections import OrderedDict
import re,time,hashlib
from .gate import Candidate,sensitive,fact_key

class MemoryCandidateEvaluator:
    def __init__(self,bot_names=('Ativia',)):
        self.bot_names=tuple(bot_names)
    def evaluate(self,event,context=()):
        text=event.text.strip();now=event.timestamp
        if sensitive(text) or len(text)>1200 or re.search(r'别记|不要记|不许记|保密|秘密|开玩笑|哈哈|笑死|想死|自杀|病历|身份证|手机号',text):return None
        if re.match(r'他说|她说|引用|例如|假设|如果|转发|帮我翻译|翻译|听说',text):return None
        if text.endswith(('?','？')):return None
        names='|'.join(re.escape(name) for name in sorted(self.bot_names,key=len,reverse=True))
        prefix=r'(?:@?(?:'+names+r')(?![A-Za-z0-9_])[，,:：\s\u2005]*)?' if names else ''
        assertion=bool(re.match(r'^'+prefix+r'(?:我|最近|这几天|这周|现在|以后|叫我)',text,re.I))
        subject=event.sender_id;name=event.sender_name;category='';classification='LONG_TERM';layer='semantic';expires=None;key=''
        alias=re.search(r'(?:以后|大家)?叫你[“"\s]*([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff]{1,11})',text)
        if alias:
            value=alias.group(1).rstrip('吧啦哦')
            if value in ('大家','我们','你们','同学','同学们','所有人') or len(value)<2:return None
            subject='ativia';name='Ativia';category='alias';key='group_nickname';text='Ativia 在这个群的外号是 '+value
        elif re.search(r'(?:我们|大家|四个人|几个人|一起|咱们).*(?:一起|去|玩|约|吃|聚|讨论)|(?:周[一二三四五六日天]|明天|后天|今晚|下周).*一起',text):
            subject=event.conversation_id;name='本群';category='shared_experience' if re.search(r'昨天|上次|刚才|前天|玩到|一起.*过',text) else 'group_plan'
            classification='TEMPORARY';layer='episodic';expires=now+45*86400
            # Preserve that this is the named speaker's account/proposal, not unanimous consent.
            text=event.sender_name+' 提到：'+text
            topic=next((x for x in ('烤肉','弘大','聚餐','游戏','电影','旅行','吃饭') if x in text),'other')
            key=category+':'+topic+':'+time.strftime('%Y-%m-%d',time.localtime(now))
        elif assertion and re.search(r'(?:我|最近|这几天|这周).*(?:交换|留学|搬家|毕业|入职|考试|准备考|订婚|结婚|分手|失恋)',text):
            category='relationship_event' if re.search(r'结婚|订婚|分手|失恋',text) else 'life_event'
            layer='episodic';key='exchange_plan' if re.search(r'交换|留学',text) else 'life:'+hashlib.sha256(text.encode()).hexdigest()[:16]
            if category=='relationship_event':classification='TEMPORARY';expires=now+30*86400
        elif re.fullmatch(r'(?:大概|差不多)?[一二三四五六七八九十半\d]+(?:个)?[年月周天](?:左右)?[。.]?',text):
            previous=next((e for e in reversed(context) if e.message_id!=event.message_id and e.sender_id==event.sender_id and now-e.timestamp<600 and re.search(r'交换|留学',e.text)),None)
            if previous is None:return None
            text=previous.text+'；时长补充：'+text;category='life_event';layer='episodic';key='exchange_plan'
        elif assertion and (re.search(r'(?:我|最近|这几天).*(?:喜欢|讨厌|不吃|过敏|偏好|擅长|熟悉|最近在玩|在玩|习惯|不想被|不要主动教)',text) or re.match(r'最近在玩|我叫|叫我',text)):
            category='preference'
            if re.search(r'最近在玩|这几天.*玩',text):classification='TEMPORARY';layer='episodic';expires=now+21*86400
            obj=re.search(r'(?:喜欢|讨厌)(?:吃|玩)?(.{1,40}?)(?:[，,。！]|$)',text)
            food=next((word for word in ('香菜','辣椒','花生','海鲜','辣') if word in text and re.search(r'吃|过敏|喜欢|讨厌',text)),None)
            key='food:'+food if food else 'preference:'+obj.group(1).strip() if obj else fact_key(text)
        else:return None
        if not category:return None
        return Candidate(text[:400],classification,layer,category,.72,confidence=.88,source='wechat_group',mode='ANY',fact_key=key,
            expires_at=expires,subject_id=subject,subject_name=name,visibility='group',group_id=event.conversation_id,
            speaker_id=event.sender_id,source_timestamp=now,conversation_type='group',source_message_ids=(event.message_id,),identity_quality=event.identity_quality)

class CandidateQueue:
    def __init__(self,delay=20,limit=100):self.delay=delay;self.limit=limit;self.pending=OrderedDict()
    def add(self,candidate,now,epoch):
        key=(candidate.subject_id,candidate.group_id,candidate.visibility,candidate.fact_key)
        old=self.pending.get(key)
        if old:
            candidate=replace(candidate,source_message_ids=tuple(dict.fromkeys(old[1].source_message_ids+candidate.source_message_ids))[-8:])
        self.pending[key]=(now+self.delay,candidate,epoch)
        while len(self.pending)>self.limit:self.pending.popitem(last=False)
    def take_due(self,now,epoch):
        result=[]
        for key,(at,candidate,owner) in list(self.pending.items()):
            if owner!=epoch:self.pending.pop(key)
            elif now>=at:result.append(candidate);self.pending.pop(key)
        return tuple(result)
    def clear(self):self.pending.clear()
