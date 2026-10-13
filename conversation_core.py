"""A platform-neutral group entry into the existing Ativia reply transport."""
from dataclasses import asdict
import json,os
from chat_events import OWNER_ID,PERSONALITY_ID
from personality.engine import BehaviorContext,RelationshipState

class AtiviaConversation:
    personality_id=PERSONALITY_ID
    def __init__(self,runtime,settings):self.runtime=runtime;self.settings=settings
    def handle(self,event,events,*,ticket,cancelled,bind_cancel=None,memory_enabled=True,relationship=None,memory_subjects=None):
        if cancelled() or not ticket.valid():return ''
        people=tuple(dict.fromkeys([event.sender_id]+[e.sender_id for e in events if e.sender_id!='ativia']))
        relationship=relationship or (self.runtime.relationship if event.sender_id==OWNER_ID else
            getattr(self.runtime.memory,'person_relationship',lambda identity:RelationshipState())(event.sender_id))
        plan=self.runtime.planner.plan(BehaviorContext(user_input=event.text,mode=self.runtime.mode,
            familiarity=relationship.familiarity,initiative_tolerance=relationship.initiative_tolerance,
            playfulness_tolerance=relationship.playfulness_tolerance))
        recall=None
        if memory_enabled:
            from memory.scope import RecallScope
            scope=(RecallScope.group(event.conversation_id,people) if memory_subjects is None else
                RecallScope('group',event.conversation_id,tuple(dict.fromkeys((*memory_subjects,'ativia',event.conversation_id)))))
            recall=self.runtime.memory.recall(event.text,self.runtime.mode,scope=scope,guard=lambda:not cancelled())
        rows=[]
        for item in reversed(events):
            candidate=[item.context_row()]+rows
            if len(json.dumps(candidate,ensure_ascii=False))>6500:break
            rows=candidate
        context=dict(channel='group',mode=self.runtime.mode,decision=asdict(plan),relationship=asdict(relationship),
            memory=recall.text if recall else '',group_id=event.conversation_id,participants=list(people),
            group_messages=rows,current_speaker_id=event.sender_id,current_speaker_name=event.sender_name,
            is_owner=event.sender_id==OWNER_ID,identity_quality=event.identity_quality)
        from vision import respond_to_voice
        settings=self.settings() if callable(self.settings) else self.settings
        ticket.wechat_trace=dict(personality_called=True,memory_recalled=len(recall.ids) if recall else 0,
            model=os.getenv('OPENAI_MODEL','gpt-5.6-terra').strip() or 'gpt-5.6-terra')
        metrics={'request':ticket.request_id}
        # Recall may take time; pause/cancellation can occur after the entry
        # check. Recheck immediately before admitting the model operation.
        if cancelled() or not ticket.valid():return ''
        ticket.wechat_model_started=True
        result=respond_to_voice(event.text,'','[]',settings,metrics,cancelled,'entertainment',
            companion_context=context,bind_cancel=bind_cancel)
        if cancelled() or not ticket.valid():return ''
        return result
