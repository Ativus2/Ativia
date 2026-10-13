import sys,os,time,json,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from runtime_settings import SettingsStore
from tts_pipeline import SpeechPipeline,SpeechJob
from speech_playback import SpeechPlayer
from tts_events import subscribe
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
events=[];result={'kind':'real TTS and hardware playback; cancellation is a timed button-equivalent, not human barge-in'}
player=SpeechPlayer();pipeline=SpeechPipeline(player)
settings=SettingsStore().load()
result['saved_backend']=settings.voice_backend;result['saved_mode']=settings.tts_mode
began=time.monotonic();job=SpeechJob('这是 Ativia 的语音测试。',settings,source='preview')
def collect(row):
    if row.get('reply_id')==job.reply_id:events.append(row)
unsubscribe=subscribe(collect)
def started(identity):
    result['actual_playback_started']=True;result['first_audio_ms']=(time.monotonic()-began)*1000
    QTimer.singleShot(400,cancel)
def cancel():
    at=time.monotonic();pipeline.cancel('benchmark_user_interrupt')
    result['cancel_return_ms']=(time.monotonic()-at)*1000
    result['player_stopped']=not player.is_busy()
    QTimer.singleShot(100,finish)
def failed(identity,reason):
    result['failed']=True;result['reason']=reason[:150];QTimer.singleShot(10,finish)
def finish():
    if pipeline.has_workers() or player.has_cleanup():QTimer.singleShot(50,finish);return
    result['native_cleanup_complete']=not player.has_cleanup()
    result['events']=events
    path=ROOT/'docs/iteration4/benchmark/real-tts-playback.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    unsubscribe();pipeline.close();app.quit()
pipeline.reply_started.connect(started);pipeline.reply_failed.connect(failed)
QTimer.singleShot(0,lambda:pipeline.submit(job));QTimer.singleShot(90000,cancel)
app.exec()
print(json.dumps({k:v for k,v in result.items() if k!='events'},ensure_ascii=False))


