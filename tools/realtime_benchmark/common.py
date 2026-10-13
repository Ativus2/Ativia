"""Replay and metric definitions shared by A/B; no production monkeypatches here."""
import hashlib
import json
import math
from pathlib import Path
import threading
from time import monotonic
import wave

import numpy as np
from gpu_monitor import NvidiaReader

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'docs/realtime-benchmark'


def read_fixture(path):
    with wave.open(str(path), 'rb') as wav:
        assert wav.getsampwidth() == 2
        rate, channels = wav.getframerate(), wav.getnchannels()
        raw = np.frombuffer(wav.readframes(wav.getnframes()), '<i2').reshape(-1,channels).mean(axis=1)/32768
    if rate != 16000:
        raw = np.interp(np.arange(round(len(raw)*16000/rate))*rate/16000,np.arange(len(raw)),raw)
    raw = np.asarray(raw,dtype=np.float32)
    voiced = np.flatnonzero(np.abs(raw) > .008)
    if not len(voiced): raise ValueError('silent_input_fixture')
    # Remove padding; identical voiced input and 200ms preroll. B supplies 2s
    # detector-only silence, because Silero can need >1s to finish this corpus.
    raw = raw[max(0,voiced[0]-3200):voiced[-1]+1]
    speech_end = len(raw)/16000
    return np.concatenate([raw,np.zeros(16000,dtype=np.float32)]), speech_end


class Metrics:
    def __init__(self, variant, index):
        self.row = dict(variant=variant,index=index,events=[],status='pending',error=None)
        self.times = {}

    def mark(self, event, *, at=None, **fields):
        stamp = monotonic() if at is None else at
        self.times.setdefault(event,stamp)
        self.row['events'].append(dict(event=event,at=stamp,**fields))

    def finalize(self):
        pairs = dict(speech_end_to_stt_final_ms=('speech_end','stt_final'),
                     stt_final_to_first_token_ms=('stt_final','llm_first_token'),
                     first_token_to_audio_ms=('llm_first_token','tts_first_audio'),
                     first_audible_ms=('speech_end','audible'),
                     endpoint_ms=('speech_end','vad_final'),
                     interruption_ms=('interrupt_speech_start','output_stopped'))
        for name,(start,end) in pairs.items():
            self.row[name]=(self.times[end]-self.times[start])*1000 if start in self.times and end in self.times else None
        self.row['silent_reply'] = 'text_ready' in self.times and 'audible' not in self.times and self.row['status'] != 'cancelled'
        return self.row


class GpuTrace:
    def __init__(self):
        self.stop_event=threading.Event(); self.samples=[]; self.thread=None

    def start(self):
        def run():
            reader=None
            try:
                reader=NvidiaReader()
                while not self.stop_event.is_set():
                    self.samples.append(dict(at=monotonic(),**reader.read()))
                    self.stop_event.wait(.1)
            except Exception as exc: self.samples.append(dict(at=monotonic(),error=type(exc).__name__))
            finally:
                if reader: reader.close()
        self.thread=threading.Thread(target=run,daemon=True); self.thread.start()

    def stop(self):
        self.stop_event.set(); self.thread.join(2)

    def peaks(self,start,end):
        values=[s for s in self.samples if start <= s['at'] <= end and 'error' not in s]
        return dict(gpu_peak_percent=max((s['utilization'] for s in values),default=None),
                    vram_peak_mb=max((s['used_mb'] for s in values),default=None))


def summary(rows):
    result={}
    for variant in sorted({r['variant'] for r in rows}):
        group=[r for r in rows if r['variant']==variant]
        values=sorted(r['first_audible_ms'] for r in group if r.get('first_audible_ms') is not None)
        result[variant]=dict(rounds=len(group),audible=len(values),
            mean_first_audible_ms=sum(values)/len(values) if values else None,
            p95_first_audible_ms=values[math.ceil(.95*len(values))-1] if values else None,
            silent_replies=sum(bool(r.get('silent_reply')) for r in group),
            failed=sum(r['status']=='failed' for r in group),
            complete=sum(r['status']=='complete' for r in group),
            gpu_peak_percent=max((r['gpu_peak_percent'] for r in group if r.get('gpu_peak_percent') is not None),default=None),
            vram_peak_mb=max((r['vram_peak_mb'] for r in group if r.get('vram_peak_mb') is not None),default=None))
    return result


def write_result(folder, rows, gpu, setup):
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'result.json').write_text(json.dumps(dict(setup=setup,summary=summary(rows),rows=rows),ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'gpu-samples.json').write_text(json.dumps(gpu.samples,indent=2),encoding='utf-8')
