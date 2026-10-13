"""One foreground utterance with stage measurements and early text delivery."""
from time import monotonic
import os
from PySide6.QtCore import QThread, Signal

from audio import transcribe_speech, synthesize_speech
from character_trace import capture_model
from vision import respond_to_voice, ScreenshotAnalysisError
from tts_events import new_reply_id, event

PREVIEW_TEXT = "嗯，我在。刚才那一下，还挺有意思的。"


class VoiceWorker(QThread):
    text_ready = Signal(str)
    delta = Signal(str)
    sentence = Signal(int,str)
    transcription = Signal(str)
    audio_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, utterance, settings, recent_context, observation_context, speak, mode="entertainment", *, text_input=None):
        super().__init__()
        self.utterance, self.settings = utterance, settings
        self.recent_context, self.observation_context = recent_context, observation_context
        self.speak, self.mode = speak, mode
        self.text_input = text_input
        self.spoken = ""
        self.reply_id = new_reply_id()
        self.trace = None
        self.valid = lambda: True
        self.companion = None
        self._cancel_transport=None;self._close_started=False;self.streamed=False;self.chunk_count=0
        from sentence_chunks import SentenceChunks
        self.chunker=SentenceChunks()
        self.metrics = dict(vad_end_detection_ms=round((utterance.detected_at - utterance.speech_end) * 1000, 2),
                            queue_wait_ms=None, stt_ms=None, context_assembly_ms=None, llm_first_token_ms=None,
                            llm_total_ms=None, tts_ms=None, speech_to_text_ms=None, speech_to_audio_ms=None)
        self.metrics["speech_to_audio_ready_ms"] = None
        self.metrics['request'] = self.reply_id

    def cancelled(self):
        return self.isInterruptionRequested() or not self.valid()

    def abort(self):
        self.requestInterruption()
        callback=self._cancel_transport
        if callback and not self._close_started:
            self._close_started=True
            import threading
            def close():
                try:callback()
                except Exception:pass
            threading.Thread(target=close,name='ativia-stream-close',daemon=True).start()

    def bind_cancel(self,callback):
        self._cancel_transport=callback
        if callback and self.cancelled():self.abort()

    def on_delta(self,text):
        if self.cancelled():return
        self.streamed=True;self.delta.emit(text)
        for index,sentence in self.chunker.feed(text):
            self.chunk_count=index;self.sentence.emit(index,sentence)

    def run(self):
        self.metrics["queue_wait_ms"] = round((monotonic() - self.utterance.detected_at) * 1000, 2)
        try:
            with capture_model(self.trace):
                if self.cancelled():
                    return
                began = monotonic()
                event(self.reply_id, 'INPUT_READY', source='text' if self.text_input is not None else 'microphone',
                      endpoint_ms=self.metrics['vad_end_detection_ms'])
                try:
                    self.spoken = self.text_input if self.text_input is not None else transcribe_speech(self.utterance.samples, 16000)
                finally:
                    self.metrics["stt_ms"] = round((monotonic() - began) * 1000, 2)
                event(self.reply_id, 'STT_SKIPPED' if self.text_input is not None else 'STT_FINAL', duration_ms=self.metrics['stt_ms'])
                if self.trace is not None:
                    self.trace["context"]["user_spoken"] = self.spoken
                if self.cancelled():
                    return
                if not self.spoken:
                    self.failed.emit("这句没听清，可以再说一次。")
                    return
                self.transcription.emit(self.spoken)
                if self.companion:self.companion.remember_input(self.spoken)
                text = respond_to_voice(self.spoken, self.recent_context, self.observation_context,
                                        self.settings, self.metrics, self.cancelled, self.mode,
                                        companion_context=self.companion.context(self.spoken,legacy_mode=self.mode) if self.companion else None,
                                        on_delta=self.on_delta,bind_cancel=self.bind_cancel)
                if self.cancelled() or not text:
                    return
                if self.trace is not None:
                    self.trace.update(candidate=text, decision="SPEAK", selected_response=text)
                for index,sentence in self.chunker.feed('',final=True):
                    self.chunk_count=index;self.sentence.emit(index,sentence)
                self.text_ready.emit(text)
                event(self.reply_id, 'LLM_TEXT_READY', **self.metrics)
                # The UI hands accepted text to its owned SpeechPipeline. Finishing
                # this LLM thread never cancels or completes the speech job.
        except ScreenshotAnalysisError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit("语音处理失败，请检查连接后再试。")


class VoicePreviewWorker(QThread):
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def run(self):
        try:
            path = synthesize_speech(PREVIEW_TEXT, self.settings)
            if self.isInterruptionRequested():
                if path:
                    os.remove(path)
            else:
                self.ready.emit(path)
        except Exception:
            self.failed.emit("试听失败，请检查 API 或网络。")
