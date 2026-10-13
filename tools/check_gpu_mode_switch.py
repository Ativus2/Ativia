"""Exercise saved Gaming Mode, cache release, late admission and real PCM playback."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
out = ROOT / 'local_tts/logs/gpu-protection/mode-switch'
out.mkdir(parents=True, exist_ok=True)
os.environ['ATIVIA_TTS_EVENT_LOG'] = str(out / 'events.jsonl')
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from runtime_settings import SettingsStore, RuntimeSettings
from gpu_policy import resource_settings, Route
from audio import synthesize_speech
from tts_audio import delete_audio
from speech_playback import SpeechPlayer
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

store = SettingsStore()
raw = json.loads(store.path.read_text(encoding='utf-8'))
(out / 'settings-before.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
final = dict(raw)
for key, value in resource_settings(RuntimeSettings()).items():
    final.setdefault(key, value)
final['tts_mode'] = 'auto'

def save(data):
    temporary = store.path.with_name('settings.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(store.path)
    finally:
        temporary.unlink(missing_ok=True)

opener = build_opener(ProxyHandler({}))
def health():
    with opener.open('http://127.0.0.1:9881/health', timeout=5) as response:
        return json.load(response)

result = dict(before=health(), playback=dict(started=False, finished=False, error=None))
path = None
try:
    save(dict(final,tts_mode='gaming'))
    time.sleep(5)  # Allow the idle maintenance pass to release unused cache once.
    result['protected'] = health()
    settings = replace(store.load(), tts_fallback=False, speech_quirk_enabled=False)
    path = synthesize_speech('游戏模式已开启，我们继续。', settings, reply_id='gpu-mode-switch-direct')
    result['direct_backend'] = path.backend
    assert path.backend == 'openai'
    delete_audio(path); path = None
    # Model the client/server timing race; use the actual HTTP worker, GPU guard,
    # original OpenAI fallback and real output device, not mock audio.
    with patch('audio.choose_route',return_value=Route('local','race_before_saved_gaming')):
        path = synthesize_speech('准备好了，我们继续。', replace(settings,tts_mode='local'), reply_id='gpu-mode-switch-race')
    result['race_backend'] = path.backend
    assert path.backend == 'openai'
    app = QApplication.instance() or QApplication([])
    player = SpeechPlayer(); player.set_volume(.12)
    def started(_): result['playback']['started'] = True
    def finished(_): result['playback']['finished'] = True; app.quit()
    def failed(_, reason): result['playback']['error'] = reason; app.quit()
    player.playback_started.connect(started)
    player.playback_finished.connect(finished)
    player.playback_failed.connect(failed)
    QTimer.singleShot(30000, app.quit)
    player.play_file(path,'gpu-mode-switch-race')
    app.exec()
    result['playback']['evidence'] = SpeechPlayer.EVIDENCE
    result['after'] = health()
    assert result['playback']['started'] and result['playback']['finished'] and not result['playback']['error']
    assert result['after']['stats']['inference_count'] == result['before']['stats']['inference_count']
    assert result['after']['stats']['model_load_count'] == result['before']['stats']['model_load_count']
    result['passed'] = True
finally:
    delete_audio(path)
    save(final)
    result['saved_mode'] = store.load().tts_mode
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
