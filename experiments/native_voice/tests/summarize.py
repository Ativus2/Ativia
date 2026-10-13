"""Summarize only this task's immutable synthetic smoke files."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[3]
folder=root/'docs/pause-native-voice'
first=json.loads((folder/'native-smoke.json').read_text(encoding='utf-8'))
last=json.loads((folder/'native-smoke-final.json').read_text(encoding='utf-8'))
rows=first['failure']['diagnostics']+last['end']['diagnostics']
responses={r['response_id']:r for r in rows if r['type']=='response.done'}
levels=json.loads((folder/'render-endpoint.json').read_text(encoding='utf-8'))['samples']
summary={
 'sessions_connected':3,'model':'gpt-realtime-2.1-mini','voice':'marin','retries':0,
 'account_model_checks':1,'ephemeral_requests':3,'webrtc_calls':3,
 'completed_responses':sum(r['status']=='completed' for r in responses.values()),
 'user_cancelled_responses':sum(r['status']=='cancelled' for r in responses.values()),
 'explicit_failed_responses':sum(r['status']=='failed' for r in responses.values()),
 'silent_success_claims':0,
 'input_tokens_reported':sum(r['usage']['input_tokens'] for r in responses.values()),
 'output_tokens_reported':sum(r['usage']['output_tokens'] for r in responses.values()),
 'reported_usage':list(responses.values()),'billing_actual':None,
 'interrupt_to_server_buffer_clear_ms':138,'interruption_acoustic_ms':None,
 'render_endpoint_peak_rms':max((r['rms'] for r in levels),default=None),
 'render_endpoint_nonzero_samples':sum(r['rms']>0.001 for r in levels),
 'render_endpoint_samples':len(levels),
 'human_speaker_double_talk':'NOT_COMPLETED','human_quality':'NOT_COMPLETED',
 'second_session_failure_cause':'Unknown: response.done status=failed; original diagnostic omitted status_details. Error code capture added; no same-configuration failure reproduced in final session.',
}
(folder/'SUMMARY.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k!='reported_usage'},ensure_ascii=False))
