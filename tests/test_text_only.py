"""Offline text preset/stream/cancel regressions. No real provider or audio calls."""
import asyncio,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QRect,Qt
from PySide6.QtGui import QImage,QColor,QPixmap
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from device_profile import DeviceProfileStore
from text_session import TextSession
from runtime_settings import RuntimeSettings,SettingsStore

app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)


def until(condition,seconds=5):
    deadline=time.monotonic()+seconds
    # QTest.qWait holds the GIL on this PySide build; let the Python IO worker run too.
    while not condition() and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
    assert condition(),'condition timed out'


class DeviceTests(unittest.TestCase):
    def test_modes_are_device_local_and_never_rewrite_voice_preferences(self):
        with tempfile.TemporaryDirectory() as root:
            voice=SettingsStore(Path(root)/'settings.json');voice.save(RuntimeSettings(local_voice='dataset_voice_2',tts_mode='auto',voice_backend='local'))
            original=voice.path.read_bytes();laptop=DeviceProfileStore(Path(root)/'laptop');rtx=DeviceProfileStore(Path(root)/'rtx')
            laptop.save('text')
            self.assertEqual(laptop.load(),'text');self.assertEqual(rtx.load(),'voice')
            self.assertEqual(voice.path.read_bytes(),original)

    def test_fresh_process_cannot_import_any_audio_backend_on_start_pause_or_settings(self):
        code='''
import importlib.abc,sys,tempfile
from pathlib import Path
blocked={'audio','soundcard','sounddevice','speech_activity','voice_pipeline','tts_pipeline','tts_process','speech_playback','local_tts_client','local_tts_service','gpu_monitor','gpu_policy','torch','numpy'}
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,*args):
  if fullname.split('.')[0] in blocked:raise AssertionError('forbidden import '+fullname)
sys.meta_path.insert(0,Guard())
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from device_profile import DeviceProfileStore
from text_session import TextSession
from daily_desktop import DailyDesktop
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
with tempfile.TemporaryDirectory() as t:
 session=TextSession(DeviceProfileStore(t));daily=DailyDesktop(session);QTest.qWait(40)
 for _ in range(20):
  daily.open_chat();session.control.close();session._pause_all();session._resume_all()
 daily.local_test();daily.cloud_test();daily.command('mic',{'enabled':True})
 assert session._engine._thread is None
 assert not session.snapshot()['imported_audio_modules']
 assert 'openai' not in sys.modules
 assert session._pause_gate.state=='RUNNING'
 session._hotkey.stop();daily.cleanup();session.control.close_to_hide=False;session.control.close()
 print('fresh-process audio import guard passed')
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class StreamTests(unittest.TestCase):
    def setUp(self):
        hotkeys=patch.object(TextSession,"_setup_hotkey");hotkeys.start();self.addCleanup(hotkeys.stop)
        import httpx2
        from openai import AsyncOpenAI
        self.temp=tempfile.TemporaryDirectory()
        isolation=patch('companion_runtime.data_directory',return_value=Path(self.temp.name))
        isolation.start();self.addCleanup(isolation.stop)
        self.session=TextSession(DeviceProfileStore(self.temp.name))
        self.session.control.show();self.requests=[];self.streams=[];owner=self
        class Stream(httpx2.AsyncByteStream):
            closed=False
            async def __aiter__(self):
                for content in ('第一段，','第二段，','完成。'):
                    data={'id':'offline','object':'chat.completion.chunk','model':'offline-fixture',
                          'choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]}
                    yield ('data: '+json.dumps(data)+'\n\n').encode()
                    await asyncio.sleep(.15)
                yield b'data: [DONE]\n\n'
            async def aclose(self):self.closed=True
        def handler(request):
            self.assertEqual(request.url.path,'/v1/chat/completions')
            self.requests.append(json.loads(request.content));stream=Stream();self.streams.append(stream)
            return httpx2.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
        self.session._engine._client=AsyncOpenAI(api_key='offline-test-placeholder',base_url='https://offline.invalid/v1',
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),max_retries=0)

    def tearDown(self):
        self.session.companion.close()
        self.session.companion.memory.close_future.result(3)
        self.session._pause_all();self.session._engine.close()
        until(lambda:not self.session._engine._thread or not self.session._engine._thread.is_alive())
        if self.session._hotkey:self.session._hotkey.stop()
        if self.session._select:self.session._select.close()
        self.session.control.close();self.session.deleteLater();QTest.qWait(20);self.temp.cleanup()

    def test_incremental_text_appears_before_completion_and_uses_same_prompt(self):
        from vision import build_text_request
        expected=build_text_request('测试问题','','[]',self.session._settings)
        self.session._on_chat_submitted('测试问题')
        until(lambda:'第一段' in self.session.control.history.toPlainText())
        self.assertTrue(self.session._busy());self.assertNotIn('完成。',self.session.control.history.toPlainText())
        until(lambda:not self.session._busy())
        self.assertEqual(self.session.control._last_reply,'第一段，第二段，完成。')
        for key in ('model','max_completion_tokens','messages'):self.assertEqual(self.requests[0][key],expected[key])
        self.assertTrue(self.requests[0]['stream']);self.assertEqual(len(self.requests),1)

    def test_cancel_closes_real_sdk_stream_and_next_question_still_works(self):
        self.session._on_chat_submitted('中止测试');until(lambda:bool(self.streams))
        until(lambda:'第一段' in self.session.control.history.toPlainText())
        old=self.session._active;self.session._stop_current_reply()
        until(lambda:self.streams[0].closed)
        self.assertEqual(self.session._pause_gate.state,'RUNNING');self.assertFalse(self.session._busy())
        before=self.session.control.history.toPlainText()
        self.session._delta(old,'不能出现');self.session._completed(old,'不能记住',{})
        self.assertEqual(before,self.session.control.history.toPlainText());self.assertFalse(self.session._recent_turns)
        self.session._on_chat_submitted('新问题');until(lambda:not self.session._busy())
        self.assertEqual(len(self.requests),2)

    def test_pause_blocks_pending_capture_and_late_results_but_preserves_completed_history(self):
        self.session.control.append_message('Ativia','之前已完成的历史')
        self.session._on_chat_submitted('在途问题');old=self.session._active
        with patch('screenshot.capture_selected_jpeg') as capture:
            self.session._later_capture(lambda:self.session._capture(QRect(10,10,50,50)))
            self.session._pause_all();self.session._resume_all();QTest.qWait(220)
            capture.assert_not_called()
        self.session._completed(old,'迟到结果',{});self.session._delta(old,'迟到增量')
        self.assertFalse(self.session._recent_turns)
        self.assertIn('之前已完成的历史',self.session.control.history.toPlainText())
        self.assertNotIn('迟到',self.session.control.history.toPlainText())
        self.session._pause_all();count=len(self.requests);self.session._on_chat_submitted('禁止问题');QTest.qWait(80)
        self.assertEqual(count,len(self.requests))

    def test_explicit_image_request_uses_only_supplied_crop_and_no_audio_parameters(self):
        image=QImage(400,240,QImage.Format_RGB32);image.fill(QColor('white'))
        from screenshot import qimage_to_jpeg
        data=qimage_to_jpeg(image,max_width=None,quality=90)
        with patch('screenshot.capture_selected_jpeg',return_value=data) as capture:
            self.session._selection_command='解释选区';self.session._capture(QRect(20,30,100,80))
            capture.assert_called_once_with(QRect(20,30,100,80))
            until(lambda:not self.session._busy())
        import base64
        payload=self.requests[0];parts=payload['messages'][1]['content']
        self.assertEqual(base64.b64decode(parts[1]['image_url']['url'].split(',')[1]),data)
        self.assertNotIn('audio',payload);self.assertIsNone(self.session._active_image)

    def test_repeated_cancel_and_resume_reuses_one_worker_and_keeps_no_queue(self):
        for n in range(20):
            self.session._on_chat_submitted('重复测试');QTest.qWait(20)
            self.session._stop_current_reply();self.session._pause_all();self.session._resume_all()
        until(lambda:self.session._engine.snapshot()['pending_requests']==0)
        import threading
        self.assertEqual(sum(t.name=='ativia-text-io' for t in threading.enumerate()),1)
        self.assertFalse(self.session._pending);self.assertFalse(self.session._flush.isActive())
        self.assertIsNone(self.session._active_image)


class CaptureTests(unittest.TestCase):
    def test_crop_capture_requests_only_selected_screen_coordinates(self):
        from unittest.mock import Mock
        from screenshot import capture_selected_jpeg
        screen=Mock();screen.geometry.return_value=QRect(-1280,0,1280,720);screen.devicePixelRatio.return_value=2
        pixmap=QPixmap(200,160);pixmap.fill(QColor('white'));screen.grabWindow.return_value=pixmap
        with patch.object(QApplication,'screens',return_value=[screen]):
            data=capture_selected_jpeg(QRect(-1200,100,100,80))
        screen.grabWindow.assert_called_once_with(0,80,100,100,80)
        decoded=QImage.fromData(data);self.assertEqual((decoded.width(),decoded.height()),(200,160))

    def test_live_selection_never_freezes_whole_desktop_and_cancel_releases_image(self):
        from region_select import RegionSelectOverlay
        window=RegionSelectOverlay()
        with patch('region_select.capture_virtual_pixmap') as capture:
            window.begin(live=True);capture.assert_not_called();self.assertTrue(window._bg.isNull())
            window._bg=QPixmap(100,100);window._cancel();self.assertTrue(window._bg.isNull())
        window.close()
