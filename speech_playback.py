"""Validated PCM playback with an owned PortAudio stream and explicit lifecycle."""
import io
from pathlib import Path
import wave
import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt

from tts_audio import validate_wav_bytes, delete_audio
from tts_events import event, new_reply_id


def default_output_device():
    """Prefer the WASAPI endpoint matching the Windows default output's name."""
    devices = sd.query_devices()
    index = int(sd.default.device[1])
    if index < 0 or index >= len(devices) or not devices[index]['max_output_channels']:
        raise RuntimeError('no_audio_output_device')
    name = devices[index]['name']
    for i, candidate in enumerate(devices):
        if (candidate['name'] == name and candidate['max_output_channels'] and
                'WASAPI' in sd.query_hostapis(candidate['hostapi'])['name']):
            return i, candidate
    return index, devices[index]


def device_pcm(samples, source_rate, source_channels, rate, channels):
    pcm = np.frombuffer(samples, dtype='<i2').reshape(-1, source_channels).astype(np.float32) / 32768
    if source_rate != rate:
        indices = np.arange(max(1, round(len(pcm) * rate / source_rate))) * source_rate / rate
        pcm = np.stack([np.interp(indices, np.arange(len(pcm)), pcm[:, ch]) for ch in range(source_channels)], axis=1)
    if channels != source_channels:
        pcm = np.repeat(pcm.mean(axis=1, keepdims=True), channels, axis=1)
    return np.ascontiguousarray(pcm, dtype=np.float32)


class SpeechPlayer(QObject):
    finished = Signal()
    started = Signal()
    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)
    playback_cancelled = Signal(str, str)
    _drained = Signal(int)
    START_TIMEOUT_MS = 10000
    STALL_TIMEOUT_MS = 10000
    EVIDENCE = 'PortAudio DAC start time reached and finished_callback after all PCM frames drained; not a human listening score'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stream = None
        self._state = None
        self._volume = .8
        self._path = None
        self._reference_16k = None
        self._cleanup_threads=[];self._failed_streams=[]
        self._reply_id = None
        self._expecting_end = False
        self._started = False
        self._generation = 0
        self._position = 0
        self.cleanup_error = None
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_timeout)
        self._progress = QTimer(self)
        self._progress.setInterval(20)
        self._progress.timeout.connect(self._check_progress)
        self._drained.connect(self._on_drained, Qt.ConnectionType.QueuedConnection)

    def is_busy(self):
        return self._expecting_end

    def volume(self):
        return self._volume

    def set_volume(self, value):
        self._volume = max(0, min(1, float(value)))

    def play_file(self, path, reply_id=None):
        reply_id = reply_id or getattr(path, 'reply_id', None) or new_reply_id()
        if self._volume <= 0:
            delete_audio(path)
            event(reply_id, 'TTS_CANCELLED', reason='user_volume_zero', stage='playback')
            self.playback_cancelled.emit(reply_id, 'user_volume_zero')
            return
        if self._expecting_end:
            event(reply_id, 'PLAYBACK_FAILED', reason='player_busy')
            self.playback_failed.emit(reply_id, 'player_busy')
            return
        self._generation += 1
        generation = self._generation
        self._reply_id, self._path = reply_id, path
        self._expecting_end, self._started, self._position = True, False, 0
        try:
            content = Path(path).read_bytes()
            info = validate_wav_bytes(content)
            with wave.open(io.BytesIO(content), 'rb') as wav:
                samples = wav.readframes(wav.getnframes())
            index, device = default_output_device()
            rate = int(device['default_samplerate'])
            channels = min(2, int(device['max_output_channels']))
            sd.check_output_settings(device=index, channels=channels, dtype='float32', samplerate=rate)
            pcm = device_pcm(samples, info.sample_rate, info.channels, rate, channels)
            state = dict(cursor=0, total=len(pcm), dac_start=None, error=None, rate=rate)
            self._state = state
            self._reference_16k=device_pcm(samples,info.sample_rate,info.channels,16000,1).reshape(-1)

            def callback(outdata, frames, times, status):
                # Exceptions inside PortAudio callbacks do not propagate to Qt.
                # Capture every failure for the GUI watchdog/finished handler.
                try:
                    out = np.frombuffer(outdata, dtype=np.float32).reshape(frames, channels)
                    out.fill(0)
                    if generation != self._generation:
                        raise sd.CallbackAbort
                    if status:
                        state['error'] = 'audio_callback_' + str(status)
                        raise sd.CallbackAbort
                    offset = state['cursor']
                    count = min(frames, len(pcm) - offset)
                    out[:count] = pcm[offset:offset+count] * self._volume
                    if state['dac_start'] is None and count:
                        voiced = np.flatnonzero(np.max(np.abs(out[:count]), axis=1) > 2 / 32768)
                        if voiced.size:
                            state['dac_start'] = times.outputBufferDacTime + int(voiced[0]) / rate
                    state['cursor'] = offset + count
                    if state['cursor'] == len(pcm):
                        # CallbackStop drains queued samples; abort would truncate.
                        raise sd.CallbackStop
                except (sd.CallbackStop, sd.CallbackAbort):
                    raise
                except Exception as exc:
                    state['error'] = 'audio_callback_' + type(exc).__name__
                    raise sd.CallbackAbort

            def finished_callback():
                self._drained.emit(generation)
                # cffi void callbacks must return None; Signal.emit returns bool.

            self._stream = sd.RawOutputStream(device=index, samplerate=rate, channels=channels,
                dtype='float32', blocksize=0, latency='high', callback=callback,
                finished_callback=finished_callback)
            event(reply_id, 'PLAYBACK_REQUESTED', duration=info.duration, bytes=info.size,
                  volume=self.volume(), engine='PortAudio/WASAPI', device=device['name'])
            self._watchdog.start(self.START_TIMEOUT_MS)
            self._progress.start()
            self._stream.start()
        except Exception as exc:
            self._fail(type(exc).__name__ + ': ' + str(exc))

    def echo_reference(self):
        reference=self._reference_16k
        if reference is None or self._state is None:return None
        cursor=round(self._state['cursor']*16000/self._state['rate'])
        start=max(0,cursor-12800)
        return reference[start:min(len(reference),cursor+1600)]*self._volume

    def _check_progress(self):
        if not self._expecting_end or self._stream is None or self._state is None:
            return
        state = self._state
        if state['error']:
            self._fail(state['error'])
            return
        try:
            first = state['dac_start']
            elapsed = 0 if first is None else max(0, self._stream.time - first)
            position = min(state['cursor'], int(elapsed * state['rate']))
            if position > self._position:
                self._position = position
                self._watchdog.start(self.STALL_TIMEOUT_MS)
                if not self._started:
                    self._mark_started()
        except Exception as exc:
            self._fail('audio_stream_status_' + type(exc).__name__)

    def _mark_started(self):
        self._started = True
        event(self._reply_id, 'PLAYBACK_STARTED', position_frames=self._position, volume=self.volume())
        self.playback_started.emit(self._reply_id)
        self.started.emit()

    @Slot(int)
    def _on_drained(self, generation):
        if not self._expecting_end or generation != self._generation or self._state is None:
            return
        state = self._state
        if state['error'] or state['cursor'] != state['total'] or state['dac_start'] is None:
            self._fail(state['error'] or 'pcm_ended_before_complete_playback')
            return
        # PortAudio calls this only after CallbackStop's pending output drains.
        self._position = state['total']
        if not self._started:
            self._mark_started()
        if not self._expecting_end or generation != self._generation:
            return
        reply_id = self._reply_id
        event(reply_id, 'PLAYBACK_FINISHED', frames=state['total'], duration=state['total']/state['rate'])
        self._release()
        self.playback_finished.emit(reply_id)
        self.finished.emit()

    def _on_timeout(self):
        if self._expecting_end:
            self._fail('playback_stalled' if self._started else 'playback_never_started')

    def _fail(self, reason):
        if not self._expecting_end:
            return
        reply_id = self._reply_id
        event(reply_id, 'PLAYBACK_FAILED', reason=reason, started=self._started)
        self._release()
        self.playback_failed.emit(reply_id, reason)

    def has_cleanup(self):
        self._cleanup_threads=[thread for thread in self._cleanup_threads if thread.is_alive()]
        return bool(self._cleanup_threads)

    def wait_released(self,timeout=5):
        # Called only from ResourceChange's worker, never from the GUI.
        from time import monotonic
        deadline=monotonic()+timeout
        for thread in tuple(self._cleanup_threads):thread.join(max(0,deadline-monotonic()))
        if self.has_cleanup() or self._failed_streams or self.cleanup_error:
            raise RuntimeError('audio_output_cleanup_incomplete')

    def _release(self):
        self._generation += 1
        self._expecting_end = False
        self._watchdog.stop();self._progress.stop()
        stream,self._stream=self._stream,None
        streams=list(self._failed_streams);self._failed_streams.clear()
        if stream is not None:streams.append(stream)
        self._state=None;self._reference_16k=None
        delete_audio(self._path);self._path=None
        if not streams:return
        self.cleanup_error=None
        reply_id=self._reply_id
        import threading
        def close():
            for owned in streams:
                try:owned.abort()
                except Exception as exc:event(reply_id,'PLAYBACK_CLEANUP_ERROR',error_type=type(exc).__name__)
                finally:
                    try:owned.close()
                    except Exception as exc:
                        self._failed_streams.append(owned);self.cleanup_error=type(exc).__name__
                        event(reply_id,'PLAYBACK_CLEANUP_ERROR',error_type=type(exc).__name__)
        thread=threading.Thread(target=close,name='ativia-audio-close',daemon=True)
        self._cleanup_threads.append(thread);thread.start()

    def stop(self, emit_finished=False, reason='explicit_stop'):
        if not self._expecting_end and self._stream is None and not self._failed_streams:
            return
        reply_id = self._reply_id
        event(reply_id, 'TTS_CANCELLED', reason=reason, stage='playback')
        self._release()
        self.playback_cancelled.emit(reply_id, reason)
        if emit_finished:
            self.finished.emit()
