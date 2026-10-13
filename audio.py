from __future__ import annotations

import io
import os
import threading
import wave
from collections import deque
from time import monotonic

import numpy as np
import soundcard as sc
import sounddevice as sd
from openai import OpenAI
from persona import PROFILE
from runtime_settings import RuntimeSettings
from speech_quirks import apply_speech_quirk
from speech_playback import SpeechPlayer
from tts_audio import InvalidAudio, SpeechAudio, delete_audio, validate_audio_file
from tts_events import TTSCancelled, TTSFailure, event, new_reply_id
from tts_process import operation_context, run_backend
from gpu_policy import choose_route
from dataclasses import asdict

SAMPLE_RATE = 16_000
BUFFER_SECONDS = 8.0
SPEECH_WINDOW_SECONDS = 0.25
SPEECH_RMS_THRESHOLD = 0.0045
SPEECH_PEAK_THRESHOLD = 0.012
SPEECH_NOISE_RATIO = 3.5
SYSTEM_AUDIO_RMS_THRESHOLD = 0.012
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "nova"
TTS_SPEED = 1.2
TTS_INSTRUCTIONS = PROFILE.voice_direction
WHISPER_MODEL = "whisper-1"


def _hostapi_name(index: int) -> str:
    try:
        return str(sd.query_hostapis()[index]["name"])
    except Exception:  # noqa: BLE001
        return ""


def pick_input_device() -> int | None:
    """Prefer the Windows default mic, especially its WASAPI endpoint."""
    devices = sd.query_devices()
    default_idx = sd.default.device[0]
    if isinstance(default_idx, (list, tuple)):
        default_idx = default_idx[0]
    default_name = ""
    if isinstance(default_idx, int) and 0 <= default_idx < len(devices):
        default_name = str(devices[default_idx]["name"])

    preferred = os.getenv("MIC_DEVICE_NAME", "").strip().lower()
    scored: list[tuple[int, int]] = []
    for i, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) < 1:
            continue
        name = str(device["name"])
        api = _hostapi_name(int(device["hostapi"]))
        score = 0
        if preferred and preferred in name.lower():
            score += 100
        if default_name and name == default_name:
            score += 50
        if "WASAPI" in api:
            score += 25
        elif "DirectSound" in api:
            score += 5
        if "podcast" in name.lower() or "pd100" in name.lower():
            score += 8
        scored.append((score, i))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][1]


class MicRecorder:
    segmented_input = True
    def __init__(self, seconds: float = BUFFER_SECONDS, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._n = int(seconds * sample_rate)
        self._buf = np.zeros(self._n, dtype=np.float32)
        self._idx = 0
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self.device_index: int | None = None
        self.device_name = ""
        self._pending = deque(maxlen=100)
        self.dropped_chunks = 0
        self._accepting = False

    def start(self, *, allow_fallback=True) -> None:
        if self._stream is not None:
            return
        self.clear()
        self._accepting = True
        self.device_index = pick_input_device()
        if self.device_index is not None:
            info = sd.query_devices(self.device_index)
            self.device_name = str(info["name"])
            native_sr = int(float(info.get("default_samplerate") or self.sample_rate))
            # Prefer native rate on WASAPI, then resample in snapshot if needed.
            open_sr = native_sr if native_sr in (16000, 44100, 48000) else self.sample_rate
        else:
            open_sr = self.sample_rate

        self._capture_rate = open_sr
        self._n = int(BUFFER_SECONDS * open_sr)
        self._buf = np.zeros(self._n, dtype=np.float32)
        self._idx = 0

        kwargs: dict = {
            "samplerate": open_sr,
            "channels": 1,
            "dtype": "float32",
            "callback": self._callback,
            "blocksize": max(1, int(open_sr * 0.02)),
        }
        if self.device_index is not None:
            kwargs["device"] = self.device_index
        try:
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
        except Exception:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            if not allow_fallback:
                self._accepting = False
                raise
            # Fallback to plain default device @ 16 kHz.
            self.device_index = None
            self.device_name = "default"
            self._capture_rate = self.sample_rate
            self._n = int(BUFFER_SECONDS * self.sample_rate)
            self._buf = np.zeros(self._n, dtype=np.float32)
            self._idx = 0
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=self._callback,
                blocksize=max(1, int(self.sample_rate * 0.02)),
            )
            self._stream.start()

    def stop(self) -> None:
        self.quiesce()
        if self._stream is None:
            self.clear()
            return
        try:
            self._stream.abort()
        finally:
            self._stream.close()
            self._stream = None
            self.clear()

    def quiesce(self):
        with self._lock:
            self._accepting = False

    def _callback(self, indata, frames, _time, _status) -> None:  # noqa: ANN001
        samples = np.asarray(indata[:, 0], dtype=np.float32)
        with self._lock:
            if not self._accepting:
                return
            if len(self._pending) == self._pending.maxlen:
                self.dropped_chunks += 1
            self._pending.append((samples.copy(), monotonic()))
            end = self._idx + frames
            if end <= self._n:
                self._buf[self._idx:end] = samples
            else:
                first = self._n - self._idx
                self._buf[self._idx:] = samples[:first]
                self._buf[: frames - first] = samples[first:]
            self._idx = end % self._n

    def read_pending(self):
        with self._lock:
            chunks = list(self._pending)
            self._pending.clear()
        capture_rate = getattr(self, "_capture_rate", self.sample_rate)
        if capture_rate != self.sample_rate:
            chunks = [(np.interp(np.arange(max(1, round(data.size * self.sample_rate / capture_rate)))
                                 * capture_rate / self.sample_rate, np.arange(data.size), data).astype(np.float32), at)
                      for data, at in chunks]
        return chunks

    def snapshot(self) -> np.ndarray:
        with self._lock:
            raw = np.concatenate((self._buf[self._idx :], self._buf[: self._idx])).copy()
        capture_rate = getattr(self, "_capture_rate", self.sample_rate)
        if capture_rate == self.sample_rate or raw.size == 0:
            return raw
        # Lightweight linear resample to 16 kHz for Whisper / VAD.
        duration = raw.size / float(capture_rate)
        target_n = max(1, int(duration * self.sample_rate))
        x_old = np.linspace(0.0, 1.0, raw.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, target_n, endpoint=False)
        return np.interp(x_new, x_old, raw).astype(np.float32)

    def clear(self) -> None:
        with self._lock:
            self._buf.fill(0.0)
            self._idx = 0
            self._pending.clear()


class SystemAudioRecorder:
    """Capture Windows playback (WASAPI loopback) into a rolling buffer."""

    def __init__(self, seconds: float = BUFFER_SECONDS, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._n = int(seconds * sample_rate)
        self._buf = np.zeros(self._n, dtype=np.float32)
        self._idx = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._recorder = None
        self._chunk = max(800, int(sample_rate * 0.1))

    def start(self) -> None:
        if self._running:
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('previous_loopback_capture_not_stopped')
        self.clear()
        speaker = sc.default_speaker()
        if speaker is None:
            raise RuntimeError("找不到默认播放设备，无法监听系统声音。")
        loopback = sc.get_microphone(id=speaker.name, include_loopback=True)
        self._recorder = loopback.recorder(samplerate=self.sample_rate, channels=1)
        self._recorder.__enter__()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="system-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.quiesce()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError('loopback_capture_did_not_stop')
            self._thread = None
        if self._recorder is not None:
            self._recorder.__exit__(None, None, None)
            self._recorder = None
        self.clear()

    def quiesce(self):
        with self._lock:
            self._running = False

    def _loop(self) -> None:
        while self._running and self._recorder is not None:
            try:
                chunk = self._recorder.record(numframes=self._chunk)
            except Exception:  # noqa: BLE001
                break
            samples = np.asarray(chunk, dtype=np.float32).reshape(-1)
            self._write(samples)

    def _write(self, samples: np.ndarray) -> None:
        frames = int(samples.size)
        if frames <= 0:
            return
        with self._lock:
            if not self._running:
                return
            end = self._idx + frames
            if end <= self._n:
                self._buf[self._idx:end] = samples
            else:
                first = self._n - self._idx
                self._buf[self._idx:] = samples[:first]
                rest = frames - first
                self._buf[:rest] = samples[first:]
            self._idx = end % self._n

    def snapshot(self) -> np.ndarray:
        with self._lock:
            return np.concatenate((self._buf[self._idx :], self._buf[: self._idx])).copy()

    def clear(self) -> None:
        with self._lock:
            self._buf.fill(0.0)
            self._idx = 0


def has_speech(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bool:
    if samples.size == 0:
        return False
    peak = float(np.max(np.abs(samples)))
    if peak >= SPEECH_PEAK_THRESHOLD:
        return True
    win = max(1, int(SPEECH_WINDOW_SECONDS * sample_rate))
    levels: list[float] = []
    if samples.size < win:
        levels.append(float(np.sqrt(np.mean(np.square(samples)))))
    else:
        for start in range(0, samples.size - win + 1, win):
            chunk = samples[start : start + win]
            levels.append(float(np.sqrt(np.mean(np.square(chunk)))))
    if not levels:
        return False
    levels_sorted = sorted(levels)
    noise = float(np.median(levels_sorted[: max(1, len(levels_sorted) // 2)]))
    dynamic = max(SPEECH_RMS_THRESHOLD, noise * SPEECH_NOISE_RATIO)
    return any(level >= dynamic for level in levels)


def has_system_audio(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bool:
    return _has_energy(samples, sample_rate, SYSTEM_AUDIO_RMS_THRESHOLD)


def _has_energy(samples: np.ndarray, sample_rate: int, threshold: float) -> bool:
    if samples.size == 0:
        return False
    win = max(1, int(SPEECH_WINDOW_SECONDS * sample_rate))
    if samples.size < win:
        return float(np.sqrt(np.mean(np.square(samples)))) >= threshold
    for start in range(0, samples.size - win + 1, win):
        chunk = samples[start : start + win]
        if float(np.sqrt(np.mean(np.square(chunk)))) >= threshold:
            return True
    return False


def boost_samples(samples: np.ndarray, target_peak: float = 0.35, max_gain: float = 14.0) -> np.ndarray:
    """Raise quiet mic levels so Whisper can hear soft speech."""
    if samples.size == 0:
        return samples
    peak = float(np.max(np.abs(samples)))
    if peak < 1e-6:
        return samples
    if peak >= target_peak:
        return samples
    gain = min(target_peak / peak, max_gain)
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)


def samples_to_wav(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-your-key"):
        raise RuntimeError("还没有设置 API Key。请把密钥写进 .env 文件。")
    return OpenAI(api_key=api_key, timeout=30.0, max_retries=0)


def transcribe_speech(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    boosted = boost_samples(samples)
    wav_bytes = samples_to_wav(boosted, sample_rate)
    client = _client()
    result = client.audio.transcriptions.create(
        model=WHISPER_MODEL,
        file=("speech.wav", wav_bytes, "audio/wav"),
    )
    return (result.text or "").strip()


def synthesize_speech(text: str, settings: RuntimeSettings | None = None, *,
                      serious: bool = False, context: str = "", reply_id: str | None = None,
                      cancelled=None, force_openai: bool = False, prepared: bool = False, allow_cloud: bool = True) -> str:
    settings = settings or RuntimeSettings()
    reply_id = reply_id or new_reply_id()
    cancelled = cancelled or (lambda: False)
    if not prepared:
        text = apply_speech_quirk(text, enabled=settings.speech_quirk_enabled,
                              probability=settings.speech_quirk_probability,
                              serious=serious, context=context)
    token = operation_context.set(dict(reply_id=reply_id, cancelled=cancelled, allow_cloud=allow_cloud))
    try:
        if not text or not text.strip():
            raise TTSFailure("empty_speech_text")
        if cancelled():
            raise TTSCancelled("cancelled_before_synthesis")
        route = choose_route(settings) if not force_openai else None
        if route is not None:
            event(reply_id, 'TTS_ROUTE_SELECTED', mode=settings.tts_mode, selected_voice=settings.local_voice,
                  cloud_voice=settings.voice_name, **asdict(route))
            if route.backend == 'openai' and settings.voice_backend == 'local':
                event(reply_id, 'LOCAL_TTS_SKIPPED', reason=route.reason)
        if not force_openai and route.backend == "local":
            from local_tts_client import synthesize_local
            path = None
            event(reply_id, "LOCAL_TTS_STARTED")
            try:
                path = synthesize_local(text, settings)
                info = validate_audio_file(path)
                if cancelled():
                    raise TTSCancelled("cancelled_after_local_tts")
                event(reply_id, "LOCAL_TTS_SUCCESS", voice=settings.local_voice, bytes=info.size, duration=info.duration)
                return SpeechAudio(path, reply_id=reply_id, backend="local", info=info, spoken_text=text)
            except TTSCancelled:
                delete_audio(path)
                raise
            except Exception as exc:
                delete_audio(path)
                if cancelled():
                    raise TTSCancelled('USER_PAUSED_OR_CANCELLED') from exc
                from local_tts_service import LocalServiceStarting
                event(reply_id, 'LOCAL_TTS_SKIPPED' if isinstance(exc,LocalServiceStarting) else "LOCAL_TTS_INVALID_AUDIO" if isinstance(exc, InvalidAudio) else "LOCAL_TTS_FAILED",
                      error_type=type(exc).__name__, reason=str(exc))
                from tts_events import GPUProtected
                if isinstance(exc, GPUProtected):
                    event(reply_id, 'LOCAL_TTS_SKIPPED', reason=str(exc), stage='service_admission')
                elif not settings.tts_fallback:
                    raise TTSFailure("local_tts_failed_fallback_disabled") from exc
        if cancelled():
            raise TTSCancelled("cancelled_before_openai_tts")
        if not allow_cloud:
            raise TTSFailure('local_only_test_no_cloud: ' + (route.reason if route and route.backend=='openai' else 'local_synthesis_failed'))
        fallback = settings.voice_backend == "local" or force_openai
        stage = "FALLBACK_TTS" if fallback else "OPENAI_TTS"
        event(reply_id, stage + "_STARTED")
        path = None
        try:
            path = _synthesize_openai_speech(text, settings)
            info = validate_audio_file(path)
            if cancelled():
                raise TTSCancelled("cancelled_after_openai_tts")
            event(reply_id, stage + "_SUCCESS", voice=settings.voice_name, bytes=info.size, duration=info.duration)
            return SpeechAudio(path, reply_id=reply_id, backend="openai", info=info, spoken_text=text)
        except Exception as exc:
            delete_audio(path)
            if isinstance(exc, TTSCancelled):
                raise
            event(reply_id, stage + "_FAILED", error_type=type(exc).__name__, reason=str(exc))
            raise TTSFailure("openai_tts_failed_or_invalid_audio") from exc
    except TTSCancelled as exc:
        event(reply_id, "TTS_CANCELLED", reason=str(exc), stage="synthesis")
        raise
    except Exception as exc:
        event(reply_id, "ERROR", stage="synthesis", reason=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        operation_context.reset(token)


def _synthesize_openai_speech(text: str, settings: RuntimeSettings) -> str:
    tone = {"original": "", "light": " Use a youthful adult, light and bright tone with natural conversational energy. No cartoon exaggeration.",
            "soft": " Use a soft, relaxed and light adult tone, with natural warmth."}[settings.voice_tone]
    return run_backend("openai", text=text, model=TTS_MODEL, voice=settings.voice_name,
                       instructions=TTS_INSTRUCTIONS + tone, speed=settings.voice_speed)
