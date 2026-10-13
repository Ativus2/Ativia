"""Presentation state only; no microphone or network side effects."""
from enum import Enum
import unicodedata

class PetState(str,Enum):
    IDLE='IDLE'
    LISTENING='LISTENING'
    THINKING='THINKING'
    SPEAKING='SPEAKING'
    SCREEN_REACTION='SCREEN_REACTION'
    SLEEPING='SLEEPING'
    PAUSED='PAUSED'

def pet_state(state):
    if state.get('state')!='RUNNING':return PetState.PAUSED
    if state.get('mode')=='SLEEP':return PetState.SLEEPING
    if state.get('speaking'):return PetState.SPEAKING
    if state.get('thinking'):return PetState.THINKING
    if state.get('screen_reaction'):return PetState.SCREEN_REACTION
    if state.get('mic_active') and not state.get('translation_active'):return PetState.LISTENING
    return PetState.IDLE

def bubble_preview(text,columns=34,lines=3):
    rows=[''];width=0;truncated=False
    for ch in text.strip():
        size=2 if unicodedata.east_asian_width(ch) in ('W','F') else 1
        if ch=='\n' or width+size>columns:
            if len(rows)>=lines:truncated=True;break
            rows.append('');width=0
            if ch=='\n':continue
        rows[-1]+=ch;width+=size
    if truncated:rows[-1]=rows[-1].rstrip()+'…'
    return '\n'.join(rows)
