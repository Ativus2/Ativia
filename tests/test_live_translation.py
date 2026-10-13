"""Offline microphone translation, isolation, lifecycle and Alt+Q regressions."""
import asyncio
from array import array
import ctypes
from ctypes import wintypes
import io
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtWidgets import QApplication
from assistant_pause import PauseGate
from controls import ControlBar
from live_translation import (TranslationSettings, TranslationStore, SpeechSegments,
    TranslationController, TranslationWorker, translate_audio, friendly_error)
from hotkey import _Filter

app=QApplication.instance() or QApplication([])
app.setQuitOnLastWindowClosed(False)

class PipelineTests(unittest.TestCase):
    def test_silence_is_never_sent_and_speech_has_bounded_segments(self):
        segmenter=SpeechSegments(1000)
        quiet=array('h',[0]*100).tobytes()
        loud=array('h',[10000]*100).tobytes()
        for _ in range(100):self.assertIsNone(segmenter.push(quiet))
        results=[]
        for _ in range(140):
            value=segmenter.push(loud)
            if value:results.append(value)
        self.assertEqual(len(results),2)
        self.assertLessEqual(max(map(len,results)),12600)
        for _ in range(7):
            value=segmenter.push(quiet)
            if value:results.append(value)
        self.assertEqual(len(results),3)

    def test_short_noise_is_discarded(self):
        s=SpeechSegments(1000)
        s.push(array('h',[8000]*100).tobytes())
        for _ in range(7):self.assertIsNone(s.push(bytes(200)))

    def test_api_uses_own_models_language_and_audio_and_no_persona(self):
        client=Mock()
        client.audio.transcriptions.create=AsyncMock(return_value=SimpleNamespace(text='Hello class'))
        client.chat.completions.create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='同学们好'))]))
        settings=TranslationSettings(api_key='translation-only',recognition_model='my-asr',translation_model='my-translate',source_language='en')
        pcm=array('h',[1000]*1000).tobytes()
        self.assertEqual(asyncio.run(translate_audio(client,settings,pcm,1000)),('Hello class','同学们好'))
        params=client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(params['model'],'my-asr');self.assertEqual(params['language'],'en')
        with wave.open(io.BytesIO(params['file'][1]),'rb') as wav:
            self.assertEqual(wav.getframerate(),1000);self.assertEqual(wav.readframes(1000),pcm)
        params=client.chat.completions.create.call_args.kwargs
        self.assertEqual(params['model'],'my-translate')
        self.assertEqual(params['messages'][1]['content'],'Hello class')
        self.assertNotIn('Ativia',str(params));self.assertNotIn('image_url',str(params))

    def test_empty_transcript_does_not_translate(self):
        client=Mock();client.audio.transcriptions.create=AsyncMock(return_value=SimpleNamespace(text=' '))
        self.assertEqual(asyncio.run(translate_audio(client,TranslationSettings(),b'\0\0',16000)),('',''))
        client.chat.completions.create.assert_not_called()

    def test_store_does_not_inherit_conversation_key_or_write_other_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'OPENAI_API_KEY':'conversation-secret'}):
            store=TranslationStore(Path(directory)/'translation.json')
            self.assertEqual(store.load().api_key,'')
            settings=TranslationSettings(api_key='translation-secret')
            store.save(settings);self.assertEqual(store.load(),settings)
            self.assertNotIn('translation-secret',repr(settings))
            self.assertEqual(len(list(Path(directory).iterdir())),1)

    def test_provider_errors_do_not_leak_private_response(self):
        exc=RuntimeError('secret-key private classroom transcript')
        self.assertNotIn('secret',friendly_error(exc))

    def test_stop_cancels_inflight_request_and_releases_microphone(self):
        entered=threading.Event();closed=threading.Event();request=threading.Event();cancelled=threading.Event()
        owner=self
        class Stream:
            def __init__(self,**kwargs):self.callback=kwargs['callback']
            def __enter__(self):
                entered.set()
                for _ in range(6):self.callback(array('h',[6000]*100).tobytes(),100,None,None)
                for _ in range(7):self.callback(bytes(200),100,None,None)
                return self
            def __exit__(self,*args):closed.set()
        client=Mock()
        async def transcribe(**kwargs):
            request.set()
            try:await asyncio.sleep(60)
            finally:cancelled.set()
        client.audio.transcriptions.create=transcribe
        client.__aenter__=AsyncMock(return_value=client);client.__aexit__=AsyncMock(return_value=False)
        with patch('sounddevice.query_devices',return_value={'default_samplerate':1000}),patch('sounddevice.RawInputStream',Stream),patch('openai.AsyncOpenAI',return_value=client) as provider:
            worker=TranslationWorker(1,TranslationSettings(backend='legacy_mic',api_key='independent',base_url='https://translation.invalid/v1'))
            worker.start()
            self.assertTrue(request.wait(4));worker.stop();worker.thread.join(4)
            self.assertFalse(worker.thread.is_alive());self.assertTrue(entered.is_set());self.assertTrue(closed.is_set());self.assertTrue(cancelled.is_set())
            self.assertEqual(provider.call_args.kwargs['api_key'],'independent')
            self.assertEqual(provider.call_args.kwargs['base_url'],'https://translation.invalid/v1')
            client.chat.completions.create.assert_not_called()

class UiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.control=ControlBar(text_only=True)
        self.gate=PauseGate();self.store=TranslationStore(Path(self.temp.name)/'translation.json')
        self.store.save(TranslationSettings(api_key='offline'))
        self.controller=TranslationController(self.control,self.gate,store=self.store)
    def tearDown(self):
        self.controller.stop();self.controller.window.hide()
        if self.controller.dialog:self.controller.dialog.close()
        self.controller.window.deleteLater();self.control.close();self.control.deleteLater()
        app.processEvents();self.temp.cleanup()
    def start_mock(self):
        self.control._set_mode('learning')
        with patch('live_translation.TranslationWorker') as constructor:
            self.controller.toggle();return constructor.return_value
    def test_start_requires_learning_and_running_and_is_explicit(self):
        with patch('live_translation.TranslationWorker') as worker:
            self.controller.toggle();worker.assert_not_called()
            self.control._set_mode('learning');worker.assert_not_called()
            self.gate.transition('PAUSED');self.controller.toggle();worker.assert_not_called()
    def test_pause_rejects_late_caption_and_resume_does_not_restart(self):
        worker=self.start_mock();generation=self.controller.generation
        self.controller._caption(generation,'original','译文')
        self.assertIn('译文',self.controller.window.text.toPlainText())
        self.assertEqual(self.control._last_reply,'')
        self.gate.transition('PAUSED');self.control.set_lifecycle('PAUSED')
        worker.stop.assert_called()
        self.controller._caption(generation,'late','迟到内容')
        self.assertNotIn('迟到',self.controller.window.text.toPlainText())
        self.controller._finished(generation);self.gate.transition('RUNNING');self.control.set_lifecycle('RUNNING')
        self.assertFalse(self.controller.active);self.assertIsNone(self.controller.worker)
    def test_switch_mode_and_close_subtitle_stop_mic(self):
        worker=self.start_mock();self.control._set_mode('entertainment');worker.stop.assert_called()
        self.controller._finished(self.controller.generation)
        worker=self.start_mock();self.controller.window.close();worker.stop.assert_called()
    def test_unconfigured_translation_opens_settings_without_microphone(self):
        self.controller.settings=TranslationSettings();self.control._set_mode('learning')
        with patch('live_translation.TranslationWorker') as worker:
            self.controller.toggle();worker.assert_not_called();self.assertIsNotNone(self.controller.dialog)

class HotkeyTests(unittest.TestCase):
    def test_windows_event_types_are_deferred_and_other_messages_are_ignored(self):
        class Owner(QObject):activated=Signal()
        owner=Owner();owner.available=True;owner._id=0xA771
        calls=[];owner.activated.connect(lambda:calls.append(True));f=_Filter(owner)
        msg=wintypes.MSG();msg.message=0x312;msg.wParam=owner._id
        for kind in ('windows_generic_MSG',b'windows_dispatcher_MSG',QByteArray(b'windows_generic_MSG')):
            old=len(calls);self.assertEqual(f.nativeEventFilter(kind,ctypes.addressof(msg)),(True,0))
            self.assertEqual(len(calls),old);app.processEvents();self.assertEqual(len(calls),old+1)
        msg.wParam=999;self.assertEqual(f.nativeEventFilter(b'windows_generic_MSG',ctypes.addressof(msg)),(False,0))

class AdditionalHotkeyTests(unittest.TestCase):
    def test_registration_conflict_recovers_and_stop_disables_retry(self):
        from hotkey import AltQWatcher
        user=Mock();user.RegisterHotKey.side_effect=[False,True]
        with patch('hotkey.C.WinDLL',return_value=user),patch('hotkey.C.get_last_error',return_value=1409),patch('hotkey._OverrideHook') as fallback:
            fallback.return_value.start.return_value=False;fallback.return_value.error=5
            watcher=AltQWatcher();self.assertFalse(watcher.available);self.assertIn('1409',watcher.error)
            self.assertTrue(watcher._retry.isActive());watcher._register()
            self.assertTrue(watcher.available);self.assertFalse(watcher._retry.isActive())
            watcher.stop();self.assertFalse(watcher.available);user.UnregisterHotKey.assert_called_once()

    def test_paused_hotkey_opens_chat_and_explains_without_capture_or_api(self):
        from text_session import TextSession
        from device_profile import DeviceProfileStore
        with tempfile.TemporaryDirectory() as directory,patch.object(TextSession,'_setup_hotkey'):
            session=TextSession(DeviceProfileStore(directory),start_paused=True)
            session.control.hide();session._on_snip()
            self.assertTrue(session.control.isVisible());self.assertIn('暂停',session.control._failure)
            self.assertIsNone(session._select);self.assertIsNone(session._engine._thread)
            session.translation.window.close();session.control.close();session.deleteLater();app.processEvents()

    def test_learning_bubble_keeps_full_explanation_without_timeout(self):
        from experiments.desktop_pet.window import PetWindow
        with tempfile.TemporaryDirectory() as directory:
            pet=PetWindow(settings_path=Path(directory)/'pet.json',daily=True);pet.show()
            explanation='完整学习解释。'*70
            pet.update_state(dict(state='RUNNING',mic_active=False,learning=True,caption=explanation,instance='offline'))
            self.assertEqual(pet.bubble.toPlainText(),explanation)
            self.assertFalse(pet.bubble_timer.isActive());pet.dispose()

if __name__=='__main__':unittest.main()

