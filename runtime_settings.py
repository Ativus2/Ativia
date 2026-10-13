"""Small user-owned settings, independent of character identity and memory."""
from dataclasses import asdict, dataclass, fields
import json
import math
import os
import re
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class RuntimeSettings:
    mic_enabled: bool = True
    system_audio_enabled: bool = True
    mic_sensitivity: float = 1.0
    start_threshold: float = 0.006
    stop_threshold: float = 0.003
    silence_timeout: float = 0.6
    min_speech_seconds: float = 0.24
    echo_guard: bool = True
    voice_speed: float = 1.2
    voice_volume: float = 0.8
    voice_backend: str = "openai"
    tts_mode: str = ""  # Legacy direct callers; SettingsStore migrates to an explicit mode.
    gpu_enter_percent: float = 70.0
    gpu_resume_percent: float = 45.0
    gpu_high_seconds: float = 3.0
    gpu_recovery_seconds: float = 8.0
    gpu_vram_percent: float = 82.0
    gpu_reserve_mb: float = 1536.0
    game_processes: tuple[str, ...] = ("helldivers2.exe", "cs2.exe", "valorant-win64-shipping.exe")
    local_voice: str = "dataset_voice"
    tts_fallback: bool = True
    speech_quirk_enabled: bool = True
    speech_quirk_probability: float = 0.8
    voice_name: str = "nova"
    voice_tone: str = "original"
    user_expertise: str = "unknown"

    @classmethod
    def parse(cls, value):
        defaults = asdict(cls())
        ranges = dict(mic_sensitivity=(0.5, 4), start_threshold=(0.001, 0.08),
                      stop_threshold=(0.0005, 0.06), silence_timeout=(0.3, 1.5),
                      min_speech_seconds=(0.12, 0.8), voice_speed=(0.75, 1.35), voice_volume=(0, 1),
                      speech_quirk_probability=(0, 1), gpu_enter_percent=(50, 95), gpu_resume_percent=(10, 80),
                      gpu_high_seconds=(1, 15), gpu_recovery_seconds=(3, 60), gpu_vram_percent=(70, 95),
                      gpu_reserve_mb=(512, 4096))
        enums = dict(tts_mode={"auto", "local", "gaming", "openai", ""}, voice_backend={"openai", "local"}, voice_name={"nova", "coral", "shimmer", "sage", "marin"},
                     voice_tone={"original", "light", "soft"},
                     user_expertise={"unknown", "beginner", "experienced", "expert"})
        if not isinstance(value, dict):
            return cls()
        for key, item in value.items():
            if key in ranges and type(item) in (int, float) and math.isfinite(item):
                defaults[key] = max(ranges[key][0], min(ranges[key][1], float(item)))
            elif key in enums and isinstance(item, str) and item in enums[key]:
                defaults[key] = item
            elif key == "local_voice" and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item):
                defaults[key] = item
            elif key == "game_processes" and isinstance(item, (str, list, tuple)):
                names = re.split(r"[,;，；\n]+", item) if isinstance(item, str) else item
                defaults[key] = tuple(dict.fromkeys(n.strip().lower() for n in names if isinstance(n, str)
                    and re.fullmatch(r"[\w .-]{1,120}\.exe", n.strip(), re.IGNORECASE)))[:64]
            elif key in ("mic_enabled", "system_audio_enabled", "echo_guard", "tts_fallback", "speech_quirk_enabled") and type(item) is bool:
                defaults[key] = item
        defaults["stop_threshold"] = min(defaults["stop_threshold"], defaults["start_threshold"] * 0.8)
        defaults["gpu_resume_percent"] = min(defaults["gpu_resume_percent"], defaults["gpu_enter_percent"] - 10)
        if "tts_mode" not in value:
            defaults["tts_mode"] = "auto" if defaults["voice_backend"] == "local" else "openai"
        if defaults["tts_mode"]:
            defaults["voice_backend"] = "openai" if defaults["tts_mode"] == "openai" else "local"
        return cls(**defaults)


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Ativia" / "settings.json"

    def load(self):
        try:
            return RuntimeSettings.parse(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return RuntimeSettings()

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(asdict(value), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
