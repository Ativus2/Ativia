"""Summarize measured cloud/device evidence, without estimating recognition accuracy."""
import json,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
NAMES=['ko-wasapi-final','ko-fast','ko-long','ko-short-pauses','ko-ambient-wasapi','en-held-first-wasapi','en-boundary-wasapi','ko-human-microphone','ko-human-microphone-stereo','ko-long-conservative','ko-wasapi-tuned','ko-fast-tuned','ko-short-pauses-tuned']
def describe(xs):
 xs=[x for x in xs if isinstance(x,(int,float))]
 return dict(n=len(xs),mean=round(statistics.mean(xs),1),median=round(statistics.median(xs),1),max=round(max(xs),1),min=round(min(xs),1)) if xs else dict(n=0)
def summarize(name):
 path=ROOT/'logs'/('latency-'+name+'.jsonl')
 if not path.exists():return {}
 rows=[json.loads(x) for x in path.read_text(encoding='utf-8-sig').splitlines()]
 stats={};finals=[];counts={}
 for row in rows:
  counts[row['stage']]=counts.get(row['stage'],0)+1
  if row['stage']=='segment_metrics':stats[row['local_segment_id']]=row
  if row['stage']=='raw_final':finals.append(row)
 spoken=[stats[f['metadata']['local_segment_id']] for f in finals]
 reasons={}
 for s in spoken:reasons[s['commit_reason']]=reasons.get(s['commit_reason'],0)+1
 fields=['segment_audio_ms','speech_end_to_raw_final_ms','speech_end_to_stable_final_ms',
    'speech_end_to_first_provisional_translation_ms','speech_end_to_formal_translation_ms',
    'first_speech_to_first_provisional_translation_ms','translation_latency_ms']
 result=dict(file=str(path.relative_to(ROOT)),metadata=rows[0],counts=counts,
  reasons=reasons,metrics={field:describe([s.get(field) for s in spoken]) for field in fields},
  capture_gap_ms=describe([s.get('capture_gap_after_commit_ms') for s in stats.values()]),
  asr_commit_to_completed_ms=describe([f.get('asr_latency_ms') for f in finals]),
  first_speech_to_partial_ms=describe([(s['first_partial_time']-s['first_speech_time'])*1000 for s in spoken if s.get('first_partial_time') and s.get('first_speech_time')]),
  provisional_requests_per_final=round(counts.get('provisional_request',0)/max(1,len(finals)),2),
  raw_finals=[f['text'] for f in finals],
  stable_finals=[r['text'] for r in rows if r['stage']=='stable_final'],
  translations=[r['translation'] for r in rows if r['stage']=='translated'],
  provisional_pairs=[dict(source=r['text'],translation=r['translation']) for r in rows if r['stage']=='provisional'],
  summary=next((r for r in reversed(rows) if r['stage']=='summary'),None),
  audio_continuity=next((r for r in rows if r['stage']=='audio_continuity'),None),
  capture_summary=next((r for r in rows if r['stage']=='capture_summary'),None))
 # Deliberate-hold evidence: finals B etc arrive before first formal translation A.
 first_formal=next((r['timestamp'] for r in rows if r['stage']=='translated'),None)
 result['raw_finals_before_first_formal']=sum(f['timestamp']<first_formal for f in finals) if first_formal else None
 playback=next((r['timestamp'] for r in rows if r['stage']=='playback_started'),None)
 if playback and result['capture_summary']:
  result['driver_notice_seconds_from_playback']=[round(r['timestamp']-playback,3) for r in result['capture_summary'].get('driver_notices',[])]
 return result
def main():
 result={name:summarize(name) for name in NAMES}
 (ROOT/'logs/latency-validation-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 for name,r in result.items():
  if not r:continue
  print(name,json.dumps(dict(finals=r['counts'].get('raw_final'),reasons=r['reasons'],metrics=r['metrics'],gap=r['capture_gap_ms'],provisional_requests_per_final=r['provisional_requests_per_final'],asr_ms=r['asr_commit_to_completed_ms']),ensure_ascii=False))
if __name__=='__main__':main()

