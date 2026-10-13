"""One owned FIFO per app: TEXT_READY is not a speech-completion event."""
from collections import deque
from dataclasses import dataclass, field, replace
import threading
from time import monotonic

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from audio import synthesize_speech
from runtime_settings import RuntimeSettings
from speech_quirks import apply_speech_quirk
from tts_audio import InvalidAudio, delete_audio, validate_audio_file
from tts_events import TTSCancelled, event, new_reply_id
from gpu_policy import resource_settings


@dataclass
class SpeechJob:
    text: str
    settings: RuntimeSettings
    reply_id: str = field(default_factory=new_reply_id)
    source: str = "reply"
    serious: bool = False
    context: str = ""
    metrics: dict | None = None
    speech_end: float | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    prepared_text: str | None = None
    fallback_attempted: bool = False
    backend: str | None = None
    playback_began: bool = False
    parent_id: str = ''
    ticket: object = None
    queued_at: float = field(default_factory=monotonic)
    local_only: bool = False  # Explicit local test: no paid fallback, including GPU rejection.


class SynthesisWorker(QThread):
    def __init__(self, job, force_openai=False, parent=None, *, synthesizer=None):
        super().__init__(parent)
        self.job, self.force_openai = job, force_openai
        self.audio = None
        self.error = None
        self.synthesizer = synthesizer

    def run(self):
        began = monotonic()
        try:
            synthesize = self.synthesizer or synthesize_speech
            self.audio = synthesize(self.job.prepared_text, self.job.settings,
                reply_id=self.job.reply_id, cancelled=self.job.cancelled.is_set,
                force_openai=self.force_openai, prepared=True, **({'allow_cloud':False} if self.job.local_only else {}))
            validate_audio_file(self.audio)
        except Exception as exc:
            self.error = exc
        finally:
            if self.job.metrics is not None:
                self.job.metrics["tts_ms"] = round((monotonic() - began) * 1000, 2)


class SpeechPipeline(QObject):
    reply_started = Signal(str)
    reply_finished = Signal(str)
    reply_failed = Signal(str, str)
    reply_cancelled = Signal(str, str)
    idle = Signal()
    MAX_PENDING = 64

    def __init__(self, player, parent=None, *, synthesizer=None):
        super().__init__(parent)
        self.player = player
        self._queue = deque()
        self._active = None
        self._worker = None
        self._seen = set();self._seen_order=deque();self.scheduler=None
        self._closed = False
        self._paused = False
        self.allow_output = lambda: True
        self._synthesizer = synthesizer
        self._resource_settings = None

        player.playback_started.connect(self._started)
        player.playback_finished.connect(self._finished)
        player.playback_failed.connect(self._playback_failed)
        player.playback_cancelled.connect(self._playback_cancelled)

    def update_resource_settings(self, settings):
        self._resource_settings = resource_settings(settings)

    def is_busy(self):
        return self._active is not None or self._worker is not None or bool(self._queue)

    def has_workers(self):
        return self._worker is not None

    def submit(self, job):
        if not self.allow_output():
            job.cancelled.set()
            event(job.reply_id,'TTS_CANCELLED',reason='mode_disables_tts',stage='admission')
            self.reply_cancelled.emit(job.reply_id,'mode_disables_tts')
            return False
        if self._paused:
            job.cancelled.set()
            event(job.reply_id, 'TTS_CANCELLED', reason='USER_PAUSED', stage='admission')
            self.reply_cancelled.emit(job.reply_id, 'USER_PAUSED')
            return False
        if job.reply_id in self._seen:
            event(job.reply_id, "DUPLICATE_AUDIO_SUBMISSION_IGNORED")
            return False
        self._seen.add(job.reply_id);self._seen_order.append(job.reply_id)
        while len(self._seen_order)>1024:self._seen.discard(self._seen_order.popleft())
        event(job.reply_id, "TEXT_READY", source=job.source, characters=len(job.text))
        if self._closed or len(self._queue) >= self.MAX_PENDING:
            event(job.reply_id, "ERROR", reason="speech_queue_closed_or_full")
            self.reply_failed.emit(job.reply_id, "speech_queue_closed_or_full")
            return False
        job.prepared_text = apply_speech_quirk(job.text, enabled=job.settings.speech_quirk_enabled,
                                              probability=job.settings.speech_quirk_probability,
                                              serious=job.serious, context=job.context)
        if self.scheduler:
            job.ticket=self.scheduler.admit('tts',request_id=job.reply_id+'-tts',parent_id=job.parent_id or job.reply_id,
                timeout=90,cancel=lambda:self.cancel('scheduler_cancel'))
        self._queue.append(job)
        event(job.reply_id, "TTS_QUEUED", position=len(self._queue))
        self._pump()
        return True

    def _pump(self):
        if self._paused or self._closed:
            return
        if self._active is not None or self._worker is not None:
            return
        if not self._queue:
            self.idle.emit()
            return
        self._active = self._queue.popleft()
        if self.scheduler and self._active.ticket:self.scheduler.start(self._active.ticket)
        self._synthesize()

    def _synthesize(self, force_openai=False):
        if not self.allow_output():
            self.cancel('mode_disables_tts');return
        if self._paused or self._closed or self._active is None or self._active.cancelled.is_set():
            return
        # Apply current resource policy when dequeuing, not the snapshot from
        # when text first arrived. Preview can use unsaved settings unless the
        # currently saved mode explicitly protects gaming.
        if self._resource_settings is not None and (self._active.source != 'preview'
                or self._resource_settings['tts_mode'] in ('gaming', 'openai')):
            self._active.settings = replace(self._active.settings, **self._resource_settings)
        worker = SynthesisWorker(self._active, force_openai, self, synthesizer=self._synthesizer)
        self._worker = worker  # Keep ownership until QThread.finished, including cancellations.
        worker.finished.connect(self._synthesis_finished)
        worker.start()

    @Slot()
    def _synthesis_finished(self):
        worker = self.sender()
        if worker is not self._worker:
            return
        job = worker.job
        path, error = worker.audio, worker.error
        self._worker = None
        worker.deleteLater()
        if self._paused or job.cancelled.is_set() or isinstance(error, TTSCancelled):
            delete_audio(path)
            self._complete()
            return
        if error is not None:
            delete_audio(path)
            if isinstance(error, InvalidAudio) and job.settings.voice_backend == "local" and job.settings.tts_fallback and not job.fallback_attempted and not job.local_only:
                job.fallback_attempted = True
                event(job.reply_id, "LOCAL_TTS_INVALID_AUDIO", stage="handoff", reason=str(error))
                self._synthesize(force_openai=True)
                return
            event(job.reply_id, "ERROR", stage="speech_queue", reason=str(error), error_type=type(error).__name__)
            if job.metrics is not None:
                job.metrics["tts_error"] = True
            self.reply_failed.emit(job.reply_id, str(error))
            self._complete()
            return
        job.backend = getattr(path, "backend", "openai" if worker.force_openai else job.settings.voice_backend)
        job.fallback_attempted = job.fallback_attempted or job.backend == "openai"
        if job.metrics is not None and job.speech_end is not None:
            job.metrics["speech_to_audio_ready_ms"] = round((monotonic() - job.speech_end) * 1000, 2)
        try:
            self.player.set_volume(job.settings.voice_volume)
            self.player.play_file(path, job.reply_id)
        except Exception as exc:
            delete_audio(path)
            reason = 'playback_handoff_' + type(exc).__name__
            event(job.reply_id, 'PLAYBACK_FAILED', reason=reason)
            self._playback_failed(job.reply_id, reason)

    @Slot(str)
    def _started(self, reply_id):
        if self._active is None or self._active.reply_id != reply_id:
            return
        job = self._active
        job.playback_began = True
        if job.metrics is not None and job.speech_end is not None:
            job.metrics["speech_to_audio_ms"] = round((monotonic() - job.speech_end) * 1000, 2)
        from performance.metrics import metrics
        metrics.mark(job.parent_id or reply_id,'tts_first_audio',ms=(monotonic()-job.queued_at)*1000)
        self.reply_started.emit(reply_id)

    @Slot(str)
    def _finished(self, reply_id):
        if self._active is not None and self._active.reply_id == reply_id:
            self.reply_finished.emit(reply_id)
            self._complete()

    @Slot(str, str)
    def _playback_failed(self, reply_id, reason):
        job = self._active
        if self._paused or job is None or job.reply_id != reply_id or job.cancelled.is_set():
            return
        if job.backend == "local" and job.settings.tts_fallback and not job.fallback_attempted and not job.playback_began and not job.local_only:
            job.fallback_attempted = True
            event(reply_id, "PLAYBACK_RECOVERY", reason=reason, target="openai")
            self._synthesize(force_openai=True)
        else:
            event(reply_id, "ERROR", stage="playback", reason=reason)
            self.reply_failed.emit(reply_id, reason)
            self._complete()

    @Slot(str, str)
    def _playback_cancelled(self, reply_id, reason):
        if self._active is not None and self._active.reply_id == reply_id:
            self._active.cancelled.set()
            self.reply_cancelled.emit(reply_id, reason)
            self._complete()

    def _end_ticket(self,job,reason=''):
        if self.scheduler and job and job.ticket:
            if reason:job.ticket.cancelled.set();job.ticket.reason=reason
            self.scheduler.finish(job.ticket)
    def _complete(self):
        self._end_ticket(self._active)
        self._active = None
        # Avoid reentrancy with late Qt signals from the previous decoder.
        QTimer.singleShot(0, self, self._pump)

    def cancel(self, reason, source=None):
        kept = deque()
        for job in self._queue:
            if source is not None and job.source != source:
                kept.append(job)
                continue
            job.cancelled.set();self._end_ticket(job,reason)
            event(job.reply_id, "TTS_CANCELLED", stage="queue", reason=reason)
            self.reply_cancelled.emit(job.reply_id, reason)
        self._queue = kept
        job = self._active
        if job is not None and (source is None or job.source == source):
            job.cancelled.set();self._end_ticket(job,reason)
            if self.player.is_busy():
                self.player.stop(reason=reason)
            elif self._worker is not None:
                event(job.reply_id, "TTS_CANCELLED", stage="synthesis_requested", reason=reason)
                self.reply_cancelled.emit(job.reply_id, reason)
            else:
                self._complete()
        elif not self.is_busy():
            self.idle.emit()

    def close(self):
        self._closed = True
        self.cancel("application_shutdown")

    def pause(self):
        self._paused = True
        self.cancel('USER_PAUSED')

    def resume(self):
        if self._closed:
            raise RuntimeError('speech_pipeline_closed')
        self._paused = False
        self._pump()
