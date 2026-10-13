"""Instrument real Qt widgets and existing text transport using an isolated test profile."""
import json,os,sys,time,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/iteration4/benchmark'
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from unittest.mock import patch
from device_profile import DeviceProfileStore
from experiments.desktop_pet.window import PetWindow
from tools.windows_resources import sample_tree,summarize
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
for font_file in ('msyh.ttc','segoeui.ttf','malgun.ttf'):
    font_path=Path('C:/Windows/Fonts')/font_file
    if font_path.exists():QFontDatabase.addApplicationFont(str(font_path))
app.setFont(QFont('Microsoft YaHei',10))
directory=tempfile.TemporaryDirectory()
os.environ['ATIVIA_DATA_DIR']=directory.name
from text_session import TextSession
from daily_desktop import DailyDesktop
session=TextSession(DeviceProfileStore(Path(directory.name)/'device'))
# Only the desktop preference path is replaced; widgets/network/lifecycle are real.
with patch('daily_desktop.PetWindow',side_effect=lambda **kw:PetWindow(Path(directory.name)/'pet.json',**kw)):
    daily=DailyDesktop(session)
results={'scope':'Real Qt / text-only isolated profile; no real microphone or gameplay','events':[]}
samples=[];start=time.monotonic();phase='idle';last_sample=0;request_at=None;timeout_at=start+150
OUT.mkdir(parents=True,exist_ok=True)
def save(): (OUT/'runtime-ui.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
def tick():
    global phase,last_sample,request_at
    now=time.monotonic()
    try:
        if now>timeout_at:raise TimeoutError('runtime_probe_deadline')
        if phase=='idle':
            if now-last_sample>=2:
                samples.append(sample_tree(os.getpid(),include_launcher=False));last_sample=now
            if now-start<60:return
            results['idle_resources']=summarize(samples);results['default_panel_hidden']=not session.control.isVisible()
            results['pet_visible']=daily.pet.isVisible();results['hotkey']=session._hotkey.snapshot() if session._hotkey else None
            daily.pet.bar.show();daily.pet.position_popups()
            daily.pet.grab().save(str(OUT/'pet.png'));daily.pet.bar.grab().save(str(OUT/'hover.png'))
            menu=daily.pet.make_menu();results['menu_groups']=[a.text() for a in menu.actions()]
            menu.show();menu.grab().save(str(OUT/'menu.png'));menu.hide()
            session.companion.remember_input('以后我玩熟悉的游戏时，不要主动教我怎么玩。')
            daily.open_chat();session.control.chat_input.setText('今天终于忙完了，简短打个招呼。')
            session.control.chat_send_button.click();request_at=now;phase='chat'
        elif phase=='chat':
            if session._active:return
            if not session.control._last_reply:raise RuntimeError('no_text_reply')
            results['text_reply']=session.control._last_reply
            results['text_latency_ms']=(now-request_at)*1000
            results['text_metrics']=session._engine.snapshot()['recent_requests']
            daily.refresh();session.control.grab().save(str(OUT/'conversation.png'))
            session.control.close();results['panel_close_hides']=not session.control.isVisible() and daily.pet.isVisible()
            daily.command('memory',{'tab':0});daily.memory_dialog.grab().save(str(OUT/'memory.png'))
            results['memory_visible_rows']=daily.memory_dialog.lists[0].count()
            daily.memory_dialog.hide()
            daily.open_performance();phase='performance';request_at=now
        elif phase=='performance':
            if now-request_at<2:return
            daily.performance_dialog.refresh();daily.performance_dialog.grab().save(str(OUT/'performance.png'))
            results['performance_text']=daily.performance_dialog.view.toPlainText()
            daily.performance_dialog.hide()
            daily.command('mode',{'mode':'CLASS'});results['class_tts_disabled']=not session.control.tts_allowed()
            # Explanation display contract uses known fixture text, not a screen-capture claim.
            state=dict(daily.pet.state,learning=True,caption='작업을 마쳤어요.\n中文翻译：工作完成了。',thinking=False)
            daily.pet.update_state(state);app.processEvents()
            daily.pet.bubble.grab().save(str(OUT/'explanation.png'))
            QTest.keyClick(daily.pet.bubble,Qt.Key_Escape);results['explanation_esc']=not daily.pet.bubble.isVisible()
            daily.pet.update_state(dict(state,caption=state['caption']+'\n下一条。'))
            daily.pet.bubble.close_button.click();results['explanation_close']=not daily.pet.bubble.isVisible()
            session._pause_all();phase='pause';request_at=now
        elif phase=='pause':
            if session._pause_gate.state!='PAUSED':return
            results['paused']=True;results['pause_ms']=(now-request_at)*1000
            results['paused_inputs_disabled']=not session.control.chat_input.isEnabled()
            session._resume_all();results['resumed']=session._pause_gate.state=='RUNNING'
            results['audio_modules']=session.snapshot()['imported_audio_modules']
            session.companion.memory.submit(lambda:None).result(3)
            results['memory_rows']=len(session.companion.memory.snapshot)
            phase='closing';save();timer.stop();session._quit()
    except Exception as exc:
        results['error_type']=type(exc).__name__;save();timer.stop();session._quit()
timer=QTimer();timer.setInterval(30);timer.timeout.connect(tick);timer.start()
app.exec()
# Actual new Runtime/DB connection after the original session has closed.
from companion_runtime import CompanionRuntime
again=CompanionRuntime(Path(directory.name)/'companion.json');again.memory.ready.wait(3)
results['restart_memory_retained']=any('游戏' in r['content'] for r in again.memory.snapshot)
again.close();again.memory.close_future.result(3)
results['shutdown_complete']=not session._engine.snapshot()['worker_threads']
save()
daily.cleanup()
directory.cleanup()
print(json.dumps({k:v for k,v in results.items() if k not in ('performance_text','text_reply','text_metrics','events')},ensure_ascii=False))

