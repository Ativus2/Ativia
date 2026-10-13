"""Caption assembly adapted from upstream Translator.SyncLoop / TextUtil.
Copyright (c) 2024 SakiRinn and other contributors. Apache-2.0.
Ativia changes: keep every sentence, occurrence IDs/versions, rolling-window alignment,
bounded state and final-only translation; see docs/livecaptions-rebuild.md.
"""
from collections import OrderedDict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import re
from time import monotonic
import unicodedata

EOS = '.?!。？！'
IDLE_SECONDS = 50 * .025  # upstream Setting.MaxIdleInterval * SyncLoop sleep


def normalized(text):
    text = unicodedata.normalize('NFKC', text)
    # Keep punctuation inside numbers: 1.5, 15 and 1:5 must not deduplicate.
    return ''.join(c.casefold() for i,c in enumerate(text) if not c.isspace()
        and (not unicodedata.category(c).startswith('P') or
             (0<i<len(text)-1 and text[i-1].isdigit() and text[i+1].isdigit())))


def split_sentences(text):
    """EOS-aware, retaining *all* sentences, decimals and common English abbreviations."""
    text = re.sub(r'\s+', ' ', text).strip()
    result=[]; start=0
    for i,c in enumerate(text):
        if c not in EOS: continue
        if c=='.':
            if i and i+1<len(text) and text[i-1].isdigit() and text[i+1].isdigit(): continue
            word = text[start:i].split()[-1:] or ['']
            if word[0].lower() in ('mr','mrs','ms','dr','prof','vs','e.g','i.e') or (len(word[0])==1 and word[0].isupper()): continue
        if i+1<len(text) and text[i+1] in EOS: continue
        value=text[start:i+1].strip()
        if value: result.append(value)
        start=i+1
    tail=text[start:].strip()
    # Very long non-punctuated recognition is split at a clause/word boundary.
    # 220 UTF-8 bytes is upstream TextUtil.VERYLONG_THRESHOLD; don't split every word.
    while len(tail.encode('utf-8')) > 660:
        cut=max(tail.rfind(c,0,220) for c in ',，、— ')
        if cut<40: cut=220
        result.append(tail[:cut+1].strip()); tail=tail[cut+1:].strip()
    if tail: result.append(tail)
    bounded=[]
    for piece in result:
        while len(piece.encode('utf-8'))>1000:
            cut=piece.rfind(' ',0,300)
            if cut<50:cut=300
            bounded.append(piece[:cut].strip()); piece=piece[cut:].strip()
        if piece:bounded.append(piece)
    return bounded[-64:]


@dataclass
class Caption:
    id: int
    source: str
    version: int = 1
    state: str = 'LIVE'
    translation: str = ''
    created: float = 0
    changed: float = 0
    final_at: float = 0
    ignored: bool = False
    partial_text: str = ''
    final_text: str = ''
    translated_text: str = ''
    provider: str = ''
    language: str = 'und'
    confidence: float | None = None
    epoch: int = 0
    asr_latency_ms: float | None = None
    translation_latency_ms: float | None = None

    provisional_translation: str = ''
    provisional_source: str = ''
    provisional_version: int = 0
    segment_ids: tuple = ()
    metadata: dict | None = None
    merged_into: int | None = None

    @property
    def key(self): return (self.id,self.version,normalized(self.source))
    def snapshot(self): return replace(self)


class CaptionAssembler:
    def __init__(self, baseline=True, idle_seconds=None, strict_revisions=False):
        self.idle_seconds, self.strict_revisions = idle_seconds, strict_revisions
        self.records=OrderedDict(); self.window=[]; self.next_id=0
        self.initial=baseline; self.last_text=None
        self.metrics={'revisions':0,'finals':0,'resets':0}

    def feed(self,text,now=None):
        now=monotonic() if now is None else now
        text=text[-16384:]
        if text==self.last_text: return self.tick(now)
        self.last_text=text
        parts=split_sentences(text)
        old=list(self.window)
        keys=[normalized(c.source) for c in old]
        newkeys=[normalized(p) for p in parts]
        mapping={}
        # Anchor exact sentences by ordered occurrences, not a global text cache.
        # This preserves a second "Yes." when a snapshot becomes "Yes. Yes.".
        matcher=SequenceMatcher(None,keys,newkeys,autojunk=False)
        for a,b,size in matcher.get_matching_blocks():
            for offset in range(size): mapping[b+offset]=old[a+offset]
        used={c.id for c in mapping.values()}
        for j,p in enumerate(parts):
            if j in mapping: continue
            # Only nearby unmatched revisions can reuse an identity.
            candidates=[c for i,c in enumerate(old) if c.id not in used and
                        (i==j or (i==len(old)-1 and j==0) or
                         (self.strict_revisions and i==len(old)-1 and j==len(parts)-1))]
            for c in candidates:
                a,b=normalized(c.source),normalized(p)
                if self.strict_revisions:
                    similar = (a.startswith(b) or b.startswith(a) or
                               c.state in ('LIVE','STABLE'))
                else:
                    similar=(a.startswith(b) or b.startswith(a) or SequenceMatcher(None,a,b,autojunk=False).ratio()>.6)
                if a and b and similar:
                    mapping[j]=c; used.add(c.id); break
        events=[]; window=[]
        for j,source in enumerate(parts):
            c=mapping.get(j)
            changed_content=c is None or normalized(c.source)!=normalized(source)
            if c is None:
                self.next_id+=1
                c=Caption(self.next_id,source,created=now,changed=now,ignored=self.initial)
                self.records[c.id]=c
            elif normalized(c.source)!=normalized(source):
                c.version+=1; c.state='LIVE'; c.translation=''; c.changed=now; c.final_at=0
                self.metrics['revisions']+=1
            elif c.source==source:
                window.append(c); continue
            c.source=source
            if not c.ignored:
                if changed_content:c.state='STABLE' if (source[-1:] in EOS or j<len(parts)-1) else 'LIVE'
                events.append(c.snapshot())
            window.append(c)
        # A new sentence confirms the previous sentence, including an unpunctuated
        # tail removed when Windows rolls its visible text window forward.
        for c in old:
            if c.id not in {n.id for n in window} and c.state in ('LIVE','STABLE'):
                events.extend(self._final(c,now))
        for c in window[:-1]: events.extend(self._final(c,now))
        self.window=window; self.initial=False
        if not parts: self.metrics['resets']+=1
        while len(self.records)>96:
            self.records.popitem(last=False)
        events.extend(self.tick(now))
        return events

    def _final(self,c,now):
        if c.ignored or c.state not in ('LIVE','STABLE'): return []
        c.state='FINAL'; c.final_at=now; self.metrics['finals']+=1
        return [c.snapshot()]

    def tick(self,now=None):
        now=monotonic() if now is None else now
        events=[]
        for c in self.window:
            if c.ignored: continue
            # EOS is a strong boundary; short stabilization absorbs punctuation bursts.
            # Without EOS use upstream's idle fallback; no translation per character.
            boundary=c.source[-1:] in EOS
            if c.state=='LIVE' and now-c.changed>=.25:
                c.state='STABLE'; events.append(c.snapshot())
            if now-c.changed >= (self.idle_seconds if self.idle_seconds is not None else (.25 if boundary else IDLE_SECONDS)):
                events.extend(self._final(c,now))
        return events

    def current(self,c):
        return c.id in self.records and self.records[c.id].key==c.key

    def update(self,c,state,translation='',translation_latency_ms=None):
        if not self.current(c): return None
        row=self.records[c.id]; row.state=state; row.translation_latency_ms=translation_latency_ms
        if translation: row.translation=translation
        return row.snapshot()
