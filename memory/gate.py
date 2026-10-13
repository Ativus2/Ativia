"""Conservative, explainable memory admission. No model call, no chat archive."""
from dataclasses import dataclass
import hashlib,re,time

@dataclass(frozen=True)
class Candidate:
    content: str
    classification: str
    layer: str='session'
    type: str='context'
    importance: float=.4
    confidence: float=.95
    source: str='explicit_user'
    mode: str='ANY'
    application: str=''
    fact_key: str=''
    expires_at: float|None=None
    mutable: bool=True
    subject_id: str='owner:local'
    subject_name: str=''
    visibility: str='private'
    group_id: str=''
    speaker_id: str='owner:local'
    source_timestamp: float|None=None
    conversation_type: str='desktop'
    source_message_ids: tuple=()
    identity_quality: str='local_owner'

def domain(text):
    if re.search(r'游戏|玩.*(?:熟悉|任务)|helldivers|elden ring|死亡搁浅|绝地潜兵|攻略|game',text,re.I):return 'GAME'
    if re.search(r'课堂|课程|韩语|英语课|日语课|上课|lecture|korean|class',text,re.I):return 'CLASS'
    if re.search(r'音色|语音|朗读|声音|tts|voice',text,re.I):return 'VOICE'
    return 'ANY'

def sensitive(text):
    return bool(re.search(r'sk-[A-Za-z0-9_-]+|authorization|bearer\s+\S+|(?:api.?key|密钥|密码|验证码|银行卡|password)\s*[:：=是]|\b\d{13,19}\b',text,re.I))

def fact_key(text):
    for pattern,key in ((r'(?:游戏|game).*(?:教|攻略|tutorial)','game_teaching'),
        (r'(?:课堂|上课|class).*(?:语音|声音|tts|说话)','class_voice'),
        (r'(?:回复|回答).*(?:简|短|长|详细)','reply_length'),
        (r'我叫|叫我|my name','preferred_name'),
        (r'(?:不要|别).*(?:追问|反问)','followup_questions')):
        if re.search(pattern,text,re.I):return key
    return hashlib.sha256(text.casefold().encode()).hexdigest()

class MemoryGate:
    def judge(self,text,*,application='',now=None):
        now=time.time() if now is None else now
        if not isinstance(text,str) or sensitive(text):return Candidate('','IGNORE')
        text=text.strip()
        if not text or len(text)>4000:return Candidate('','IGNORE')
        # Save only the explicit candidate sentence, never the whole conversation.
        sentences=[s.strip() for s in re.split(r'[\n。!?！？]',text) if s.strip()]
        stable=r'记住|以后|永远|我(?:不)?喜欢|我偏好|我习惯|我叫|叫我|我的(?:母语|名字|职业)|我熟悉|我擅长|我不擅长|\bi (?:prefer|like|dislike|am allergic)|my name|always|never|기억해|좋아해'
        temporary=r'这几天|最近在|这周|本周|these days|this week'
        def assertion(s):
            if re.match(r'他说|她说|朋友说|引用|例如|假设|如果|帮我翻译|翻译|文章|屏幕|视频里|小说|有人',s):return False
            if re.search(r'这条消息|这张图片|这个笑话|这次回答',s) and not re.search(r'记住|以后',s):return False
            if text.endswith(('?','？')) and not re.search(r'记住|以后|always|never',s,re.I):return False
            return bool(re.search(stable+'|'+temporary,s,re.I))
        selected=next((s for s in sentences if assertion(s)),None)
        if selected is None:
            if re.search(r'今天.*(?:测试|调试|上课)|刚才.*(?:延迟|报错)|现在.*(?:任务|测试)',text):
                return Candidate(text[:240],'SESSION')
            return Candidate('','IGNORE')
        selected=selected[:400]
        if re.search(r'不要记住|别记|不需要记住|forget this',selected,re.I):return Candidate('','IGNORE')
        mode=domain(selected)
        app=application if mode=='GAME' and application else ''
        if re.search(temporary,selected,re.I):
            return Candidate(selected,'TEMPORARY','episodic','interest',.55,mode=mode,application=app,
                fact_key=fact_key(selected),expires_at=now+14*86400)
        event=bool(re.search(r'记住今天|记住.*(?:完成|修改|解决)',selected))
        permanent=bool(re.search(r'记住|以后|永远|always|never',selected,re.I))
        return Candidate(selected,'PERMANENT' if permanent else 'LONG_TERM','episodic' if event else 'semantic',
            'event' if event else 'preference',.9 if permanent else .75,
            mode=mode,application=app,fact_key=fact_key(selected))
