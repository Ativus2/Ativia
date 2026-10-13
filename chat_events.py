"""Platform-neutral messages. Raw UI objects never reach the conversation core."""
from dataclasses import dataclass,field

PERSONALITY_ID='ativia'
OWNER_ID='owner:local'

@dataclass(frozen=True)
class ChatEvent:
    platform:str
    conversation_type:str
    conversation_id:str
    sender_id:str
    sender_name:str
    text:str
    timestamp:float
    message_id:str
    is_at_me:bool=False
    is_reply_to_me:bool=False
    reply_to_name:str=''
    reply_to_text:str=''
    identity_quality:str='group_display_name'
    source_sequence:int=0
    timestamp_source:str='received'
    raw_message:dict=field(default_factory=dict,repr=False,compare=False)

    def context_row(self):
        return dict(speaker_id=self.sender_id,speaker=self.sender_name,text=self.text,
                    timestamp=self.timestamp,reply_to=self.reply_to_name,quote=self.reply_to_text,
                    identity_quality='bot' if self.sender_id=='ativia' else self.identity_quality,
                    delivery_status='confirmed' if self.sender_id=='ativia' else 'received')
