"""Small environment-aware retrieval over an immutable cached snapshot."""
from dataclasses import dataclass
import json,math,re,time

def tokens(text):
    latin=set(re.findall(r'[a-z][a-z0-9_]{1,}',text.casefold()))
    chars=re.findall(r'[\u4e00-\u9fff]+',text)
    return latin|{s[i:i+2] for s in chars for i in range(len(s)-1)}|set(re.findall(r'[\uac00-\ud7af]{2,}',text))

def token_upper_bound(text):
    # A UTF-8 byte upper bound, deliberately conservative; not a measured tokenizer count.
    return len(text.encode('utf-8'))

@dataclass(frozen=True)
class Recall:
    text: str
    ids: tuple
    token_bound: int
    elapsed_ms: float
    scores: tuple=()

def retrieve(rows,query,mode='NORMAL',application='',budget=1200,now=None,scope=None):
    began=time.monotonic();now=time.time() if now is None else now
    budget=max(0,min(4000,int(budget)));qt=tokens(query);ranked=[]
    from .scope import visible
    from itertools import islice
    for row in islice((row for row in rows if visible(row,scope)),1000):
        if row.get('expires_at') and now>=row['expires_at'] and not row.get('pinned'):continue
        words=tokens(row['content']);overlap=len(qt&words)/max(1,min(len(qt),len(words)))
        app=row.get('application','')
        app_match=bool(app and application and app.casefold()==application.casefold())
        mode_match=row.get('mode','ANY')==mode
        if row.get('mode','ANY') not in ('ANY',mode) and overlap<.25:continue
        if app and application and not app_match and overlap<.35:continue
        core=row.get('fact_key') in ('preferred_name','reply_length','followup_questions')
        if not (core or mode_match or app_match or overlap>=.12):continue
        age=max(0,now-row['created_at'])/86400
        half_life=7 if row['classification']=='TEMPORARY' else 180
        decay=1 if row.get('pinned') or row['classification']=='PERMANENT' else .5**(age/half_life)
        score=(.4*overlap+.2*mode_match+.15*app_match+.15*row['importance']+.1*row['confidence'])*decay
        if core:score+=.2
        if row.get('pinned'):score+=.1
        ranked.append((score,row,core))
    ranked.sort(key=lambda item:(-item[0],-item[1].get('updated_at',0)))
    limits={'core':5,'semantic':8,'episodic':5};counts=dict.fromkeys(limits,0)
    chosen=[];ids=[];scores=[]
    for score,row,core in ranked:
        kind='core' if core else row['layer']
        if kind not in limits or counts[kind]>=limits[kind]:continue
        candidate=dict(content=row['content'],source=row['source'],layer=row['layer'])
        if scope is not None and scope.channel=='group':
            candidate.update(subject_id=row.get('subject_id','owner:local'),subject=row.get('subject_name',''),group_id=row.get('group_id',''),visibility=row.get('visibility','private'),speaker_id=row.get('speaker_id',''),source_timestamp=row.get('source_timestamp'))
        encoded=json.dumps(chosen+[candidate],ensure_ascii=False,separators=(',',':'))
        if token_upper_bound(encoded)>budget:continue
        chosen.append(candidate);ids.append(row['id']);scores.append(round(score,4));counts[kind]+=1
    text=json.dumps(chosen,ensure_ascii=False,separators=(',',':')) if chosen else ''
    return Recall(text,tuple(ids),token_upper_bound(text),(time.monotonic()-began)*1000,tuple(scores))
