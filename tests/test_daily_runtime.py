"""Offline regressions; not evidence of cloud latency or human listening."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from dataclasses import replace
import threading
from pathlib import Path
from PySide6.QtTest import QTest
from behavior_probes import AppCase
from runtime_settings import RuntimeSettings
from voice_pipeline import VoiceWorker
from tts_events import TTSCancelled
import local_tts_service as service

class DailyChatTests(AppCase):
    def test_paused_preferences_save_without_acquiring_devices(self):
        self.app._pause_all()
        until=__import__('time').monotonic()+3
        while self.app._resource_change and __import__('time').monotonic()<until:QTest.qWait(10)
        self.mic.start.reset_mock();self.system.start.reset_mock()
        self.app._apply_settings(replace(self.app._settings,mic_enabled=False))
        self.app._set_system_enabled(False)
        self.mic.start.assert_not_called();self.system.start.assert_not_called()
        self.assertEqual(self.app._pause_gate.state,'PAUSED')
        self.assertIn('已停止',self.app.control.status_label.text())

    def test_plain_chat_uses_existing_voice_llm_without_capture_or_asr(self):
        with patch('voice_pipeline.respond_to_voice',return_value='这里是回答。') as llm, patch('voice_pipeline.transcribe_speech') as asr:
            self.app._on_chat_submitted('现在聊一下。')
            self.wait_idle()
            llm.assert_called_once();asr.assert_not_called();self.capture.assert_not_called();self.ask.assert_not_called()
            self.assertEqual(llm.call_args.args[0],'现在聊一下。')

    def test_pause_invalidates_pending_text(self):
        release=threading.Event()
        with patch('voice_pipeline.respond_to_voice',side_effect=lambda *a,**k:release.wait(2) and '迟到的回答'):
            self.app._on_chat_submitted('问题');QTest.qWait(20)
            self.app._pause_all();release.set()
            until=__import__('time').monotonic()+3
            while (self.app._voice_worker or self.app._resource_change) and __import__('time').monotonic()<until:QTest.qWait(10)
            self.show_text.assert_not_called();self.tts.assert_not_called()

class LocalStartupTests(unittest.TestCase):
    def test_local_only_test_never_pays_when_gpu_protects(self):
        from audio import synthesize_speech
        from gpu_policy import Route
        with patch('audio.choose_route',return_value=Route('openai','gpu_telemetry_unavailable')),patch('audio._synthesize_openai_speech') as cloud:
            with self.assertRaisesRegex(RuntimeError,'local_only_test_no_cloud'):
                synthesize_speech('本地测试',RuntimeSettings(voice_backend='local'),allow_cloud=False)
            cloud.assert_not_called()

    def test_ready_service_reused_without_process_launch(self):
        with patch.object(service,'health',return_value={'status':'ready'}) as health:
            self.assertEqual(service.ensure_service(),{'status':'ready'});health.assert_called_once()

    def test_cancel_does_not_start_or_fallback(self):
        with patch.object(service,'health') as health:
            with self.assertRaises(TTSCancelled):service.ensure_service(lambda:True)
            health.assert_not_called()

    def test_foreign_port_identity_not_adopted(self):
        with patch.object(service,'health',side_effect=RuntimeError('wrong_project')):
            with self.assertRaisesRegex(RuntimeError,'wrong_project'):service.ensure_service()

if __name__=='__main__':unittest.main()
