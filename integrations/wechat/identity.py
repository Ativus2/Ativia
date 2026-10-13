"""Stable IDs where provided; explicitly provisional, group-scoped display-name fallback."""
from dataclasses import dataclass
import hashlib
from chat_events import OWNER_ID

def group_id(name):return 'wechat:group:'+name

@dataclass(frozen=True)
class PersonIdentity:
    person_id:str
    display_name:str
    platform:str='wechat'
    quality:str='group_display_name'
    aliases:tuple=()

class IdentityResolver:
    def __init__(self,config):self.config=config
    def resolve(self,group,name,stable_id=''):
        if not name or not isinstance(name,str):raise ValueError('missing_sender')
        if stable_id:
            identity=OWNER_ID if stable_id in self.config.owner_ids else 'wechat:person:'+stable_id
            return PersonIdentity(identity,name,quality='stable_id')
        if name in self.config.owner_names:return PersonIdentity(OWNER_ID,name,quality='configured_display_name')
        digest=hashlib.sha256((group+'\0'+name).encode()).hexdigest()[:24]
        return PersonIdentity('wechat:person:provisional:'+digest,name)
