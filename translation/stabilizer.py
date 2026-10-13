"""Keep provider finals together without ever promoting an interim to final."""
from collections import OrderedDict
from dataclasses import dataclass, field
from time import monotonic
from .captions import Caption, normalized


def complete(text):
    text = text.rstrip().rstrip('\"\'”’」』）)')
    return bool(text) and text[-1] in '.?!。？！'


def join_text(left, right, overlapping=False):
    if not left: return right
    if not right: return left
    if overlapping:
        # Only callers with overlapping audio spans may delete shared words.
        if right.startswith(left): return right
        if left.endswith(right): return left
        for size in range(min(len(left), len(right)), 2, -1):
            if left[-size:] == right[:size]:
                boundary = left[-size].isspace() or (size == len(left)) or left[-size-1].isspace()
                cjk = any('\u3040' <= c <= '\u9fff' for c in right[:size])
                if boundary or cjk: return left + right[size:]
    tight = ('\u3040' <= left[-1] <= '\u9fff' and '\u3040' <= right[0] <= '\u9fff')
    return left.rstrip() + ('' if tight else ' ') + right.lstrip()


@dataclass
class _Group:
    row: Caption
    segments: OrderedDict = field(default_factory=OrderedDict)
    last_final: float = 0
    wait_seconds: float | None = None


class TranscriptStabilizer:
    def __init__(self, merge_window_ms=650, isolate_partials=False):
        if not 300 <= merge_window_ms <= 1500: raise ValueError('final merge window must be 300–1500 ms')
        self.window = merge_window_ms/1000
        self.isolate_partials = isolate_partials
        self.records = OrderedDict()
        self.groups = OrderedDict()
        self.segment_groups = OrderedDict()
        self.pending = None
        self.next_id = 0
        self.epoch_id_base=0
        self.epoch = 0
        self.metrics = {'revisions': 0, 'finals': 0, 'merged': 0, 'duplicates': 0, 'resets': 0}

    def reset(self, epoch):
        self.epoch = epoch
        self.epoch_id_base=self.next_id
        self.pending = None; self.records.clear(); self.groups.clear(); self.segment_groups.clear()
        # IDs never repeat within a session, even after connection reset.
        self.metrics['resets'] += 1

    def feed(self, event, now=None):
        now = monotonic() if now is None else now
        if event.epoch != self.epoch or not event.text.strip(): return []
        key = (event.provider, event.epoch, event.segment_id)
        group = self.groups.get(self.segment_groups.get(key))
        events = []
        if group:
            previous = group.segments[key]
            if event.revision < previous.revision: return []
            if previous.is_final and not event.is_final and event.revision <= previous.revision: return []
            same_words = normalized(previous.text) == normalized(event.text)
            committed = group.row.state in ('FINAL','TRANSLATING','TRANSLATED','SKIPPED','ERROR')
            if previous.is_final == event.is_final and (previous.text == event.text or (same_words and committed)):
                self.metrics['duplicates'] += 1; return []
            group.row.version += 1; self.metrics['revisions'] += 1
        else:
            group = None if self.isolate_partials else self.pending
            if group and (complete(group.row.final_text) or now-group.last_final > self.window
                          or len(group.row.source) > 3000):
                # A running interim extends a not-yet-complete sentence, regardless
                # of how long the provider takes to finalize that audio segment.
                if all(part.is_final for part in group.segments.values()):
                    events += self._commit(group, now); group = None
            if group and complete(group.row.final_text):
                events += self._commit(group, now); group = None
            if group is None:
                requested=event.metadata.get('segment_number') if self.isolate_partials else None
                cid=self.epoch_id_base+requested if isinstance(requested,int) else self.next_id+1
                self.next_id=max(self.next_id,cid)
                row = Caption(cid, '', created=now, changed=now, epoch=event.epoch,
                              provider=event.provider, language=event.language)
                group = _Group(row)
                self.groups[row.id] = group; self.records[row.id] = row
                if self.isolate_partials:
                    self.groups=OrderedDict(sorted(self.groups.items()))
                    self.records=OrderedDict(sorted(self.records.items()))
            else:
                group.row.version += 1
                if event.is_final: self.metrics['merged'] += 1
            self.segment_groups[key] = group.row.id
        if self.isolate_partials and event.is_final:
            # Keep each live item independently visible. Only merge already-final
            # neighbours that finish inside the original merge window.
            ids=list(self.groups);index=ids.index(group.row.id)
            prior=self.groups[ids[index-1]] if index else None
            if (prior and prior.row.state=='RAW_FINAL' and not complete(prior.row.final_text) and
                    all(part.is_final for part in prior.segments.values()) and
                    now-prior.last_final <= (prior.wait_seconds or self.window)):
                retired=prior.row.snapshot();retired.state='MERGED';retired.merged_into=group.row.id
                events.append(retired)
                group.segments=OrderedDict(list(prior.segments.items())+list(group.segments.items()))
                for segment in prior.segments:self.segment_groups[segment]=group.row.id
                group.row.version+=1;self.groups.pop(prior.row.id);self.records.pop(prior.row.id)
                group.wait_seconds=max(prior.wait_seconds or self.window,group.wait_seconds or self.window)
                self.metrics['merged']+=1
        group.segments[key] = event
        row = group.row
        partial = ''; final = ''; source = ''; previous = None
        for part in group.segments.values():
            overlap = (previous is not None and previous.audio_end is not None and
                       part.audio_start is not None and part.audio_start < previous.audio_end-.02)
            source = join_text(source, part.text, overlap)
            if part.is_final: final = join_text(final, part.text, overlap)
            else: partial = join_text(partial, part.text, overlap)
            previous = part
        row.source, row.partial_text, row.final_text = source, partial, final
        row.translation = ''; row.translated_text = ''; row.final_at = 0
        row.changed = now; row.language = event.language; row.confidence = event.confidence
        row.asr_latency_ms = event.asr_latency_ms; row.translation_latency_ms = None
        row.segment_ids=tuple(part.segment_id for part in group.segments.values())
        row.metadata=dict(event.metadata) if event.metadata else {}
        if event.is_final:
            reason=event.metadata.get('commit_reason')
            wait=self.window
            if reason=='silence' and (event.metadata.get('silence_before_commit_ms') or 0)>=400:
                wait=min(self.window,.35)
            elif reason=='hard_timeout':wait=max(self.window,.9)
            group.wait_seconds=max(group.wait_seconds or 0,wait)
        row.state = 'PARTIAL' if partial else 'RAW_FINAL'
        if event.is_final: group.last_final = now
        self.pending = group
        events.append(row.snapshot())
        while len(self.records) > 96:
            cid, _ = self.records.popitem(last=False); self.groups.pop(cid, None)
            self.segment_groups = OrderedDict((k, v) for k, v in self.segment_groups.items() if v != cid)
        return events

    def _commit(self, group, now):
        row = group.row
        if row.partial_text or not row.final_text or row.state not in ('RAW_FINAL', 'PARTIAL'): return []
        row.source = row.final_text; row.state = 'FINAL'; row.final_at = now
        self.metrics['finals'] += 1
        if self.pending is group: self.pending = None
        return [row.snapshot()]

    def tick(self, now=None, force=False):
        now = monotonic() if now is None else now
        events = []
        for group in list(self.groups.values()):
            if force or now-group.last_final >= (group.wait_seconds or self.window): events += self._commit(group, now)
        return events

    def current(self, row):
        return row.epoch == self.epoch and row.id in self.records and self.records[row.id].key == row.key

    def update(self, row, state, translation='', translation_latency_ms=None):
        if not self.current(row): return None
        target = self.records[row.id]; target.state = state
        if translation:
            target.translation = translation; target.translated_text = translation
        target.translation_latency_ms = translation_latency_ms
        return target.snapshot()
