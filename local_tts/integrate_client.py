from pathlib import Path
root=Path.cwd()
def write(name,text):
 p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
def edit(name,old,new):
 p=root/name;s=p.read_text(encoding='utf-8');assert old in s,(name,old);p.write_text(s.replace(old,new,1),encoding='utf-8')
write('local_tts_client.py','''"""Loopback-only WAV client. Reference recordings never leave the TTS process."""
import io
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import wave

ROOT = Path(__file__).resolve().parent / "local_tts"
ENDPOINT = "http://127.0.0.1:9881"

class LocalTTSError(RuntimeError):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LocalTTSError("Local TTS redirect rejected")

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
    payload = json.dumps(dict(text=text, voice=settings.local_voice, speed=settings.voice_speed,
                             language="auto"), ensure_ascii=False).encode("utf-8")
    request = Request(ENDPOINT + "/tts", payload, {"Content-Type": "application/json"}, method="POST")
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=120) as response:
            if response.status != 200 or "audio/wav" not in response.headers.get("Content-Type", ""):
                raise LocalTTSError("Local TTS returned no WAV")
            content = response.read(32 * 1024 * 1024 + 1)
        if len(content) > 32 * 1024 * 1024:
            raise LocalTTSError("WAV exceeds size limit")
        with wave.open(io.BytesIO(content), "rb") as wav:
            expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
            if wav.getnframes() == 0 or wav.getsampwidth() != 2 or len(wav.readframes(wav.getnframes())) != expected:
                raise LocalTTSError("Empty or incomplete PCM16 WAV")
    except LocalTTSError:
        raise
    except Exception as exc:
        raise LocalTTSError("Local TTS unavailable or invalid audio: " + type(exc).__name__) from exc
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ativia-local-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
    except Exception:
        os.unlink(path)
        raise
    return path
''')
edit('audio.py','import io\n','import io\nimport logging\n')
edit('audio.py','def synthesize_speech(text: str, settings: RuntimeSettings | None = None) -> str:\n    settings = settings or RuntimeSettings()\n    client = _client()', '''def synthesize_speech(text: str, settings: RuntimeSettings | None = None) -> str:
    settings = settings or RuntimeSettings()
    if settings.voice_backend == "local":
        from local_tts_client import synthesize_local, LocalTTSError
        try:
            return synthesize_local(text, settings)
        except LocalTTSError:
            if not settings.tts_fallback:
                raise
            logging.getLogger(__name__).warning("Local TTS failed; using configured OpenAI fallback (text only).")
    return _synthesize_openai_speech(text, settings)


def _synthesize_openai_speech(text: str, settings: RuntimeSettings) -> str:
    client = _client()''')
edit('runtime_settings.py','import os\n','import os\nimport re\n')
edit('runtime_settings.py','    voice_name: str = "nova"','    voice_backend: str = "openai"\n    local_voice: str = "dataset_voice"\n    tts_fallback: bool = True\n    voice_name: str = "nova"')
edit('runtime_settings.py','enums = dict(voice_name=', 'enums = dict(voice_backend={"openai", "local"}, voice_name=')
edit('runtime_settings.py','elif key in ("mic_enabled", "echo_guard")', 'elif key == "local_voice" and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item):\n                defaults[key] = item\n            elif key in ("mic_enabled", "echo_guard", "tts_fallback")')
edit('voice_settings.py','from runtime_settings import RuntimeSettings','from runtime_settings import RuntimeSettings\nfrom local_tts_client import voice_choices')
edit('voice_settings.py','        self.voice = QComboBox()', '''        self.backend = QComboBox()
        self.backend.addItem("原有声音（OpenAI）", "openai")
        self.backend.addItem("本地数据集音色", "local")
        self.backend.setCurrentIndex(self.backend.findData(settings.voice_backend))
        form.addRow("语音来源", self.backend)
        self.local_voice = QComboBox()
        for key, label in voice_choices():
            self.local_voice.addItem(label, key)
        selected = self.local_voice.findData(settings.local_voice)
        if selected < 0:
            self.local_voice.addItem(settings.local_voice, settings.local_voice)
            selected = self.local_voice.count() - 1
        self.local_voice.setCurrentIndex(selected)
        form.addRow("本地音色", self.local_voice)
        self.fallback = QCheckBox("本地失败时使用原有声音（仅回复文字发送到 OpenAI）")
        self.fallback.setChecked(settings.tts_fallback)
        form.addRow(self.fallback)
        self.voice = QComboBox()''')
edit('voice_settings.py','form.addRow("音色", self.voice)','form.addRow("原有／备用音色", self.voice)')
edit('voice_settings.py','        form.addRow("声音风格 / Tone", self.tone)','''        form.addRow("声音风格 / Tone", self.tone)
        self.backend.currentIndexChanged.connect(self._update_backend_controls)
        self._update_backend_controls()''')
edit('voice_settings.py','当前 TTS 没有独立音高 Pitch 参数。风格会传给语音模型，但不保证精确音高；未使用变调后处理。','本地音色由参考录音决定；需先启动 start-local-tts.bat。声音风格仅作用于原有 TTS；语速和音量均可调。')
edit('voice_settings.py','    def current(self):','''    def _update_backend_controls(self):
        local = self.backend.currentData() == "local"
        self.local_voice.setEnabled(local)
        self.fallback.setEnabled(local)
        self.tone.setEnabled(not local or self.fallback.isChecked())

    def current(self):''')
edit('voice_settings.py','voice_name=self.voice.currentData(), voice_tone=self.tone.currentData(),','voice_name=self.voice.currentData(), voice_tone=self.tone.currentData(),\n                    voice_backend=self.backend.currentData(), local_voice=self.local_voice.currentData(),\n                    tts_fallback=self.fallback.isChecked(),')
with (root/'.gitignore').open('a',encoding='utf-8') as f:
 f.write('\n# Private voice data and local ML runtime\nlocal_tts/data/\nlocal_tts/backups/\nlocal_tts/models/\nlocal_tts/vendor/\nlocal_tts/.venv/\nlocal_tts/samples/\nlocal_tts/logs/\nlocal_tts/configs/voices.json\n')
print('Integration written; existing default remains OpenAI until local validation succeeds.')
