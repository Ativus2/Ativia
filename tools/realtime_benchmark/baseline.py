"""Production VoiceWorker + SpeechPipeline + WASAPI with read-only timing taps."""
import asyncio
from dataclasses import replace
from pathlib import Path
from time import monotonic
from unittest.mock import patch
import wave
import numpy as np

from audio import transcribe_speech, synthesize_speech
from speech_activity import SpeechActivity, Utterance
from speech_playback import SpeechPlayer
from tts_pipeline import SpeechPipeline, SpeechJob
from voice_pipeline import VoiceWorker


class MeasuredPlayer(SpeechPlayer):
    def __init__(self, metrics):
        super().__init__(); self.metrics=metrics; self.onset_seconds=0

    def play_file(self,path,reply_id=None):
        with wave.open(str(path),'rb') as wav:
            pcm=np.frombuffer(wav.readframes(wav.getnframes()),'<i2').reshape(-1,wav.getnchannels())
            voiced=np.flatnonzero(np.max(np.abs(pcm.astype(np.int32)),axis=1)>256)
            if not len(voiced): raise ValueError('no_audible_pcm')
            self.onset_seconds=voiced[0]/wav.getframerate()
        return super().play_file(path,reply_id)

    def _check_progress(self):
        if self._stream and self._state and self._state['dac_start'] is not None:
            now=monotonic(); device_now=self._stream.time
            audible_at=self._state['dac_start']+self.onset_seconds
            if device_now >= audible_at and 'audible' not in self.metrics.times:
                self.metrics.mark('audible',at=now-(device_now-audible_at),evidence='WASAPI DAC time + first voiced PCM frame')
        return super()._check_progress()


async def run(samples, input_end, settings, metrics, qt):
    vad=SpeechActivity(settings)
    utterances=[]
    for offset in range(0,len(samples),320):
        utterances += vad.feed(samples[offset:offset+320],(offset+320)/16000)
    if len(utterances)!=1: raise RuntimeError(f'baseline_vad_utterances_{len(utterances)}')
    detected=utterances[0].detected_at
    metrics.row['vad_speech_end_error_ms']=(utterances[0].speech_end-input_end)*1000
    metrics.mark('speech_end')
    await asyncio.sleep(max(0,detected-input_end))
    metrics.mark('vad_final')
    utterance=Utterance(utterances[0].samples,metrics.times['speech_end'],monotonic())
    player=MeasuredPlayer(metrics)
    def synthesis(text,settings,**kwargs):
        path=synthesize_speech(text,settings,**kwargs)
        metrics.mark('tts_first_audio',backend=path.backend)
        metrics.row['tts_backend']=path.backend
        return path
    pipeline=SpeechPipeline(player,synthesizer=synthesis)
    pipeline.update_resource_settings(settings)
    worker=VoiceWorker(utterance,settings,'','[]',True)
    done=asyncio.Event()
    def stt(*args,**kwargs):
        metrics.mark('stt_started')
        result=transcribe_speech(*args,**kwargs)
        metrics.mark('stt_final'); metrics.row['transcript']=result
        return result
    def text_ready(text):
        metrics.mark('text_ready'); metrics.row['reply']=text
        first=worker.metrics.get('llm_first_token_ms')
        if first is not None:
            metrics.mark('llm_first_token',at=metrics.times['stt_final']+first/1000)
        pipeline.submit(SpeechJob(text,settings,reply_id='realtime-A-'+str(metrics.row['index'])))
    def failed(*args):
        metrics.row.update(status='failed',error=str(args[-1])); done.set()
    def finished(_):
        metrics.mark('playback_finished'); metrics.row['status']='complete'; done.set()
    worker.text_ready.connect(text_ready); worker.failed.connect(failed)
    pipeline.reply_failed.connect(failed); pipeline.reply_finished.connect(finished)
    try:
        with patch('voice_pipeline.transcribe_speech',side_effect=stt):
            worker.start()
            async with asyncio.timeout(180):
                while not done.is_set(): qt.processEvents(); await asyncio.sleep(.005)
            while worker.isRunning() or pipeline.has_workers(): qt.processEvents(); await asyncio.sleep(.005)
        metrics.row['production_metrics']=worker.metrics
    finally:
        worker.requestInterruption(); pipeline.close()
        while worker.isRunning() or pipeline.has_workers(): qt.processEvents(); await asyncio.sleep(.01)
