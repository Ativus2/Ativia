"""Visibility is enforced before ranking or prompt construction, not left to an LLM."""
from dataclasses import dataclass
from chat_events import OWNER_ID

@dataclass(frozen=True)
class RecallScope:
    channel:str='desktop'
    group_id:str=''
    subjects:tuple=(OWNER_ID,)
    @classmethod
    def group(cls,group_id,subjects):return cls('group',group_id,tuple(dict.fromkeys((*subjects,OWNER_ID,'ativia',group_id))))

def visible(row,scope=None):
    scope=scope or RecallScope()
    subject=row.get('subject_id',OWNER_ID);visibility=row.get('visibility','private');group=row.get('group_id','')
    if scope.channel=='group':
        if not scope.group_id or visibility not in ('group','public'):return False
        if visibility=='group' and group!=scope.group_id:return False
        return subject in scope.subjects
    # Default desktop recall cannot mistake a group member's "I" for the owner.
    return subject in scope.subjects and visibility in ('private','public') and not (visibility=='private' and group)
