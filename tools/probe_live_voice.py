"""Explicit bounded live probe: one synthetic input + STT + reply + TTS (4 calls).

Never loaded by tests. No real microphone, desktop or user conversation is sent.
"""
import io
import json
from pathlib import Path
import sys
from time import monotonic
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.venv/Lib/site-packages'))
from dotenv import load_dotenv
import numpy as np
from audio import _client, TTS_MODEL
from runtime_settings import RuntimeSettings
from speech_activity import SpeechActivity
from voice_pipeline import VoiceWorker

load_dotenv(ROOT / '.env')
OUT = ROOT / 'docs/real-use-fixes'
report = dict(synthetic_input=True, actual_microphone=False, playback_device_tested=False,
              max_logical_api_calls=4, logical_api_calls=0, status='started')
paths = []
worker = None
try:
    text = '刚才队友冲过去那一下，真把我看愣了。'
    report['logical_api_calls'] += 1
    with _client() as client:
        response = client.audio.speech.create(model=TTS_MODEL, voice='nova', input=text, response_format='wav')
        wav_bytes = response.read()
    (OUT / 'synthetic-voice-input.wav').write_bytes(wav_bytes)
    with wave.open(io.BytesIO(wav_bytes)) as source:
        assert source.getsampwidth() == 2
        rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if source.getnchannels() > 1:
            samples = samples.reshape(-1, source.getnchannels()).mean(axis=1)
    samples = np.interp(np.arange(round(samples.size * 16000 / rate)) * rate / 16000,
                        np.arange(samples.size), samples).astype(np.float32)
    vad = SpeechActivity()
    signal = np.concatenate((samples, np.zeros(16000, dtype=np.float32)))
    began = monotonic() - len(signal) / 16000
    utterances = []
    for at in range(0, len(signal), 320):
        utterances.extend(vad.feed(signal[at:at + 320], began + (at + 320) / 16000))
    report['vad_fixture_segments'] = len(utterances)
    if len(utterances) != 1:
        raise RuntimeError('Fixture was not a single utterance; live processing stopped')
    utterance = utterances[0]
    # Offline WAV replay timestamps are not real capture wall time.
    utterance.detected_at = monotonic()
    utterance.speech_end = utterance.detected_at - .6
    worker = VoiceWorker(utterance, RuntimeSettings(user_expertise='experienced'), '',
                         '[{"scene":"game","subject":"HELLDIVERS 2","event":"surprise","evidence":"队友冲进敌群"}]', True)
    worker.trace = dict(context={}, delivery=dict(status='pending', response=''))
    def arrived(text):
        worker.metrics['speech_to_text_ms'] = round((monotonic() - utterance.speech_end) * 1000, 2)
        report['response'] = text
    worker.text_ready.connect(arrived)
    worker.audio_ready.connect(paths.append)
    worker.failed.connect(lambda message: report.update(error=message))
    report['logical_api_calls'] += 1  # STT; later stages counted from measured execution.
    worker.run()
    if worker.metrics['llm_total_ms'] is not None:
        report['logical_api_calls'] += 1
    if worker.metrics['tts_ms'] is not None:
        report['logical_api_calls'] += 1
    report.update(status='passed' if report.get('response') and paths else 'incomplete',
                  recognized=worker.spoken, metrics=worker.metrics, model=worker.trace.get('model'),
                  vad_latency_basis='Offline WAV replay, configured 600 ms hangover; not human endpoint ground truth.')
    for path in paths:
        (OUT / 'synthetic-voice-response.mp3').write_bytes(Path(path).read_bytes())
except Exception as exc:
    # Do not dump SDK request headers, credentials or raw exception payloads.
    report.update(status='failed', error_type=type(exc).__name__)
finally:
    for path in paths:
        Path(path).unlink(missing_ok=True)
    (OUT / 'live-voice-probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
