"""Hysteretic, fail-closed admission BEFORE local synthesis, shared with the service."""
from dataclasses import dataclass
import threading
from time import monotonic
from gpu_monitor import get_monitor

RESOURCE_FIELDS = ('tts_mode', 'gpu_enter_percent', 'gpu_resume_percent', 'gpu_high_seconds',
                   'gpu_recovery_seconds', 'gpu_vram_percent', 'gpu_reserve_mb', 'game_processes')


def resource_settings(settings):
    return {name: getattr(settings, name) for name in RESOURCE_FIELDS}


def effective_mode(settings):
    return settings.tts_mode or ('local' if settings.voice_backend == 'local' else 'openai')


@dataclass(frozen=True)
class Route:
    backend: str
    reason: str
    gpu_percent: float | None = None
    used_mb: float | None = None
    total_mb: float | None = None
    game: str = ''


def continuous_seconds(history, predicate, now):
    earliest = None
    previous = now
    for sample in reversed(history):
        if previous - sample.at > 2.5 or not sample.valid or not predicate(sample):
            break
        earliest, previous = sample.at, sample.at
    return 0 if earliest is None else max(0, now - earliest)


class GpuPolicy:
    def __init__(self):
        self.protected = False
        self._lock = threading.Lock()

    def decide(self, settings, history, now=None, *, own_inference=False):
        with self._lock:
            return self._decide(settings, history, monotonic() if now is None else now, own_inference)

    def _decide(self, settings, history, now, own_inference):
        mode = effective_mode(settings)
        sample = history[-1] if history else None
        values = dict(gpu_percent=sample.utilization, used_mb=sample.used_mb, total_mb=sample.total_mb) if sample else {}
        def cloud(reason, **extra):
            return Route('openai', reason, **values, **extra)
        if mode in ('gaming', 'openai'):
            return cloud('manual_gaming' if mode == 'gaming' else 'openai_only')
        if sample is None or not sample.valid or now - sample.at > 2.5 or sample.at > now + .1:
            self.protected = True
            return cloud('gpu_telemetry_unavailable')
        games = set(settings.game_processes)
        if games and sample.process_error:
            self.protected = True
            return cloud('game_detection_unavailable')
        running = sorted(games & sample.processes)
        if running:
            self.protected = True
            return cloud('configured_game_running', game=running[0])
        memory_busy = sample.used_mb / sample.total_mb * 100 >= settings.gpu_vram_percent or sample.free_mb < settings.gpu_reserve_mb
        if memory_busy:
            self.protected = True
            return cloud('vram_headroom_low')
        # Do not mistake the current serial job's own kernels for a new game.
        # Game processes, manual mode and VRAM remain checked per fragment.
        if own_inference:
            return Route('local', 'existing_serial_inference', **values)
        if sample.utilization >= settings.gpu_enter_percent:
            sustained = continuous_seconds(history, lambda s: s.utilization >= settings.gpu_enter_percent, now)
            if sustained >= settings.gpu_high_seconds:
                self.protected = True
            # First high sample already defers NEW inference. The sustained
            # threshold controls the latch, not permission to compete with games.
            return cloud('gpu_high_sustained' if self.protected else 'gpu_high_pending')
        if self.protected:
            def safe(s):
                return (s.utilization <= settings.gpu_resume_percent
                        and s.used_mb / s.total_mb * 100 < settings.gpu_vram_percent - 5
                        and s.free_mb >= settings.gpu_reserve_mb + 512
                        and not (games & s.processes) and not (games and s.process_error))
            if continuous_seconds(history, safe, now) < settings.gpu_recovery_seconds:
                return cloud('gpu_recovery_hold')
            self.protected = False
        return Route('local', 'gpu_idle', **values)


_policy = GpuPolicy()


def choose_route(settings):
    # Legacy library calls still receive the server-side admission check.
    # SettingsStore/UI use an explicit mode for proactive client-side routing.
    if not settings.tts_mode:
        return Route(settings.voice_backend, 'legacy_backend')
    return _policy.decide(settings, get_monitor().history())
