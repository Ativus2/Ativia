"""Exercise Ativia's real TTS call and PCM playback without playing aloud."""
import json
import os
from pathlib import Path
import shutil
import sys
from dataclasses import replace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
os.environ["QT_QPA_PLATFORM"]="offscreen"
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from audio import synthesize_speech, SpeechPlayer
from runtime_settings import RuntimeSettings
from voice_settings import VoiceSettingsDialog
app=QApplication([])
settings=replace(RuntimeSettings(),voice_backend="local",local_voice="dataset_voice_2",tts_fallback=False)
dialog=VoiceSettingsDialog(settings)
assert dialog.current().voice_backend=="local"
assert dialog.current().local_voice=="dataset_voice_2"
with patch("audio._synthesize_openai_speech",side_effect=AssertionError("Cloud TTS must not run")):
    path=synthesize_speech("这是一段本地音色测试，我会继续陪你学习。",settings)
shutil.copy2(path,ROOT/"samples/ativia-integration.wav")
player=SpeechPlayer()
player.set_volume(0)
result=dict(local_call=True,wav=path.endswith(".wav"),started=False,finished=False,errors=[])
player.started.connect(lambda:result.update(started=True))
def done():
    result["finished"]=True
    app.quit()
player.finished.connect(done)
player.playback_failed.connect(lambda reply_id,message:result["errors"].append(message))
QTimer.singleShot(25000,app.quit)
player.play_file(path)
app.exec()
player.stop()
(ROOT/"logs/integration-check.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
assert result["started"] and result["finished"] and not result["errors"],result
print("REAL_LOCAL_CALL_AND_QT_WAV_PLAYBACK_PASSED",flush=True)
