from dataclasses import dataclass, asdict
from functools import lru_cache
import json
from pathlib import Path
import re

@lru_cache(maxsize=1)
def core():
    return json.loads((Path(__file__).with_name('core.json')).read_text(encoding='utf-8-sig'))

@lru_cache(maxsize=1)
def core_prompt():
    c=core();i=c['identity']
    intro=f"你是女性桌面角色 {i['name']}，{i['temperament']}。偏好{'、'.join(i['interests'])}；{i['humor']}。"
    return intro+'\n'+'\n'.join(c['principles'])

@dataclass(frozen=True)
class ModeRules:
    initiative: float
    reply_length: str
    tts: bool
    vision_seconds: int
    animation: str
    threshold: float
    allow_unasked_teaching: bool=False

MODES={
 'NORMAL':ModeRules(.25,'short',True,60,'IDLE',.55),
 'GAME':ModeRules(.3,'very_short',True,60,'IDLE',.65),
 'CLASS':ModeRules(0,'short',False,300,'IDLE',1),
 'FOCUS':ModeRules(0,'short',False,300,'IDLE',1),
 'VOICE':ModeRules(.15,'short',True,120,'LISTENING',.65),
 'SLEEP':ModeRules(0,'short',False,0,'SLEEPING',1)
}

@dataclass
class RelationshipState:
    familiarity: float=0.1
    conversation_frequency: int=0
    initiative_tolerance: float=0.45
    playfulness_tolerance: float=0.4
    user_receptiveness: float=0.5
    def engaged(self,text):
        self.conversation_frequency+=1
        self.familiarity=min(.9,.1+self.conversation_frequency/500)
        # Only explicit feedback changes tolerances; silence is not rejection.
        if re.search(r'别(?:主动)?打扰|少(?:说话|开口)|不要主动(?:说话|发言|打扰)',text):
            self.initiative_tolerance=.1
        if re.search(r'别开玩笑|不要开玩笑|不喜欢.*玩笑',text):
            self.playfulness_tolerance=.05
        if re.search(r'可以多聊|可以主动(?:说话|开口)|多陪我说话',text):
            self.initiative_tolerance=.55;self.user_receptiveness=.7
        if re.search(r'可以开玩笑|喜欢.*玩笑',text) and not re.search(r'不喜欢|不要|别',text):self.playfulness_tolerance=.6

@dataclass(frozen=True)
class BehaviorContext:
    user_input: str=''
    mode: str='NORMAL'
    application: str=''
    screen_event: str='none'
    paused: bool=False
    focused: bool=False
    confident: bool=True
    initiative_tolerance: float=.45
    familiarity: float=.1
    playfulness_tolerance: float=.4
    recent_reactions: int=0

@dataclass(frozen=True)
class Decision:
    action: str
    tone: str='casual'
    length: str='short'
    allow_question: bool=False
    allow_teaching: bool=False
    initiative: float=0
    reason: str=''
    def prompt(self):
        return '本轮行为约束：'+json.dumps(asdict(self),ensure_ascii=False)+(
            '。自然回应；缺少必要信息才澄清，不例行追问。' if self.action!='stay_silent' else '。本轮保持沉默。')

class BehaviorPlanner:
    def plan(self,c):
        mode=c.mode if c.mode in MODES else 'NORMAL';rules=MODES[mode]
        text=c.user_input.strip()
        if c.paused:return Decision('stay_silent',reason='paused')
        if text:
            help_requested=bool(re.search(r'怎么|如何|为什么|解释|教我|帮我|攻略|建议|[?？]|为什么|어떻게|왜|도와|설명|教えて|\b(?:how|why|help|explain)\b',text,re.I))
            if re.search(r'不要.*(?:教我|教学|攻略)|不用.*(?:教|建议)|别.*(?:教|指导)|不需要.*(?:攻略|建议)',text):help_requested=False
            clarify=bool(re.search(r'你(?:要|需要)问我什么|ask me (?:a |one )?question',text,re.I))
            action='ask_question' if clarify else 'acknowledge' if re.fullmatch(r'(?:好[的吧]?|嗯+|谢谢|知道了|ok|thanks)[。.!！]*',text,re.I) else 'respond'
            tone='warm' if c.familiarity<.25 else 'casual'
            return Decision(action,tone,rules.reply_length,clarify,help_requested,0,'explicit_user')
        if mode in ('CLASS','FOCUS','SLEEP') or c.focused:
            return Decision('stay_silent',reason='quiet_mode_or_focus')
        if not c.confident or c.recent_reactions>=3 or c.initiative_tolerance<.15:
            return Decision('stay_silent',reason='uncertain_or_low_tolerance')
        if c.screen_event in ('none','normal','ordinary_combat'):
            return Decision('stay_silent',reason='ordinary_event')
        if c.screen_event in ('screen_scan','window_changed','large_visual_change'):
            return Decision('observe',length='very_short',reason='inspect_changed_screen')
        if c.screen_event in ('death_screen','mission_complete','success','surprise','resolved','error','important_notification','user_returns'):
            return Decision('screen_comment','gentle' if c.screen_event in ('death_screen','error') else 'casual',
                'very_short',False,False,min(rules.initiative,c.initiative_tolerance),'grounded_event')
        return Decision('stay_silent',reason='no_response_needed')
