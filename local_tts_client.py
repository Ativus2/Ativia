"""Loopback-only WAV client. Reference recordings never leave the TTS process."""
import json
from pathlib import Path
import re
from tts_audio import InvalidAudio
from tts_events import TTSCancelled, GPUProtected
from tts_process import run_backend, operation_context
from gpu_policy import resource_settings
from local_tts_service import LocalServiceStarting

ROOT = Path(__file__).resolve().parent / "local_tts"
ENDPOINT = "http://127.0.0.1:9881"

class LocalTTSError(RuntimeError):
    pass


class LocalTTSInvalidAudio(InvalidAudio, LocalTTSError):
    pass

def voice_choices():
    try:
        data = json.loads((ROOT / "configs" / "voices.json").read_text(encoding="utf-8"))
        return [(key, value.get("label", key)) for key, value in data.items()
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) and isinstance(value, dict)]
    except (OSError, ValueError, AttributeError):
        return [("dataset_voice", "数据集音色（需启动本地服务）")]

def synthesize_local(text, settings):
    if not text.strip():
        raise LocalTTSError("No text supplied")
    try:
        from local_tts_service import ensure_service
        context = operation_context.get() or {}
        state = ensure_service(context.get('cancelled', lambda: False), context.get('reply_id', 'local'),
                               wait=not context.get('allow_cloud',True))
        from tts_events import event
        event(context.get('reply_id','local'), 'LOCAL_MODEL_READY' if state.get('model_loaded') else 'LOCAL_MODEL_COLD',
              voice=settings.local_voice, model=state.get('model'), device=state.get('device'))
        return run_backend("local", endpoint=ENDPOINT, text=text, voice=settings.local_voice,
                           speed=settings.voice_speed, resource_policy=resource_settings(settings))
    except (TTSCancelled, GPUProtected, LocalServiceStarting):
        raise
    except InvalidAudio as exc:
        raise LocalTTSInvalidAudio(str(exc)) from exc
    except Exception as exc:
        raise LocalTTSError(str(exc)) from exc
