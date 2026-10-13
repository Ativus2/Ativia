"""Causal 20 ms energy/band gate with hysteresis and bounded utterance buffering.

This is acoustic VAD, not semantic speech recognition or speaker identification.
"""
from collections import deque
from dataclasses import dataclass
import numpy as np

from runtime_settings import RuntimeSettings


@dataclass
class Utterance:
    samples: np.ndarray
    speech_end: float
    detected_at: float
    forced_split: bool = False


class SpeechActivity:
    sample_rate = 16000
    frame_size = 320

    def __init__(self, settings=None):
        self.settings = settings or RuntimeSettings()
        self.level = 0.0
        self.noise = 0.0005
        self.reset()

    def reset(self):
        self.pending = np.zeros(0, dtype=np.float32)
        self.preroll = deque(maxlen=10)
        self.frames = []
        self.active = False
        self.confirmed = False
        self.started = False
        self.voiced_seconds = 0.0
        self.last_voice = None
        self.run_seconds = 0.0

    @property
    def threshold(self):
        return max(self.settings.start_threshold, self.noise * 2.5)

    def feed(self, samples, end_at, detected_at=None):
        samples = np.nan_to_num(np.clip(np.asarray(samples, dtype=np.float32), -1, 1))
        self.pending = np.concatenate((self.pending, samples))
        output = []
        self.started = False
        while self.pending.size >= self.frame_size:
            at = end_at - (self.pending.size - self.frame_size) / self.sample_rate
            frame = self.pending[:self.frame_size].copy()
            self.pending = self.pending[self.frame_size:]
            self.level = float(np.sqrt(np.mean(frame * frame))) * self.settings.mic_sensitivity
            power = np.abs(np.fft.rfft(frame - np.mean(frame))) ** 2
            # Reject DC/low fan hum and broad high-frequency noise before duration gating.
            frequencies = np.fft.rfftfreq(self.frame_size, 1 / self.sample_rate)
            band = float(power[(frequencies >= 200) & (frequencies <= 3800)].sum() / max(1e-12, power.sum()))
            threshold = max(self.settings.stop_threshold, self.noise * 1.5) if self.active else self.threshold
            voiced = self.level >= threshold and band >= 0.62
            self.preroll.append(frame)
            if not self.active:
                self.run_seconds = self.run_seconds + 0.02 if voiced else 0.0
                if not voiced:
                    self.noise = 0.98 * self.noise + 0.02 * min(self.level, self.settings.start_threshold)
                if self.run_seconds >= 0.08:
                    self.active = True
                    self.frames = list(self.preroll)
                    self.voiced_seconds = self.run_seconds
                    self.last_voice = at
            else:
                self.frames.append(frame)
                if voiced:
                    self.last_voice = at
                    self.voiced_seconds += 0.02
                    if not self.confirmed and self.voiced_seconds >= self.settings.min_speech_seconds:
                        self.confirmed = True
                        self.started = True
                forced = len(self.frames) >= 1500  # 30 seconds, independent of UI polling.
                if forced or at - self.last_voice >= self.settings.silence_timeout - 1e-6:
                    if self.voiced_seconds >= self.settings.min_speech_seconds:
                        output.append(Utterance(np.concatenate(self.frames), self.last_voice,
                                               detected_at if detected_at is not None else at, forced))
                    self.frames = []
                    self.active = False
                    self.confirmed = False
                    self.voiced_seconds = self.run_seconds = 0.0
                    self.preroll.clear()
        return output
