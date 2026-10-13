"""Resource scheduling contracts: no GPU stress or cloud calls in unit tests."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from runtime_settings import RuntimeSettings, SettingsStore
from gpu_monitor import Sample, GpuMonitor
from gpu_policy import GpuPolicy, choose_route, resource_settings
from tts_fixtures import audio_file
import audio


def samples(values, *, used=2000, free=6192, games=(), first=0):
    return [Sample(at=first+i, utilization=value, used_mb=used, total_mb=8192, free_mb=free,
                   processes=frozenset(games)) for i, value in enumerate(values)]


class GpuAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(RuntimeSettings(), tts_mode='auto', voice_backend='local')

    def test_high_pending_then_sustained_and_hysteresis(self):
        policy = GpuPolicy()
        self.assertEqual(policy.decide(self.settings, samples([71]), 0).reason, 'gpu_high_pending')
        history = samples([72]*4)
        self.assertEqual(policy.decide(self.settings, history, 3).reason, 'gpu_high_sustained')
        history += samples([60,69,46,44,49,44], first=4)
        self.assertEqual(policy.decide(self.settings, history, 9).reason, 'gpu_recovery_hold')
        history += samples([40]*9, first=10)
        self.assertEqual(policy.decide(self.settings, history, 18).backend, 'local')

    def test_memory_uses_capacity_and_free_headroom_not_bandwidth(self):
        for used, free in ((7000,1192), (5000,1200)):
            self.assertEqual(GpuPolicy().decide(self.settings, samples([4], used=used, free=free), 0).reason,
                             'vram_headroom_low')

    def test_configured_games_override_low_gpu_and_local_preference(self):
        for mode in ('auto', 'local'):
            settings = replace(self.settings, tts_mode=mode, game_processes=('test-game.exe',))
            route = GpuPolicy().decide(settings, samples([2], games=('test-game.exe',)), 0)
            self.assertEqual(route.backend, 'openai')
            self.assertEqual(route.game, 'test-game.exe')
        self.assertEqual(GpuPolicy().decide(self.settings, samples([2], games=('steam.exe',)), 0).backend, 'local')

    def test_missing_stale_broken_or_process_unknown_telemetry_is_protected(self):
        cases = [([],0), ([Sample(at=0,error='missing')],0), (samples([0]),3),
                 ([replace(samples([2])[0],process_error=True)],0)]
        for history, now in cases:
            self.assertEqual(GpuPolicy().decide(self.settings, history, now).backend, 'openai')

    def test_recovery_requires_continuous_fresh_samples(self):
        policy = GpuPolicy(); policy.protected = True
        history = samples([20,20], first=0) + samples([20,20], first=10)
        self.assertEqual(policy.decide(self.settings, history,11).backend,'openai')

    def test_manual_modes_always_cloud_and_fallback_switch_does_not_disable_protection(self):
        for mode in ('gaming','openai'):
            settings = replace(self.settings,tts_mode=mode,tts_fallback=False)
            self.assertEqual(GpuPolicy().decide(settings,samples([0]),0).backend,'openai')

    def test_own_serial_inference_is_not_misidentified_as_a_new_game(self):
        self.assertEqual(GpuPolicy().decide(self.settings,samples([90]*4),3,own_inference=True).backend,'local')
        self.assertEqual(GpuPolicy().decide(self.settings,samples([90],games=('cs2.exe',)),0,own_inference=True).backend,'openai')

    def test_protection_selects_cloud_without_a_local_call_or_local_start_event(self):
        for reason_history in (samples([80]*4,first=time.monotonic()-3),
                               samples([3],used=7500,free=692,first=time.monotonic()),
                               samples([3],games=('cs2.exe',),first=time.monotonic())):
            policy = GpuPolicy()
            route = policy.decide(self.settings,reason_history)
            with patch('audio.choose_route',return_value=route), patch('local_tts_client.synthesize_local') as local, \
                    patch('audio._synthesize_openai_speech',return_value=audio_file(self)), patch('audio.event') as events:
                result = audio.synthesize_speech('准备好了。',replace(self.settings,tts_fallback=False))
                self.assertEqual(result.backend,'openai')
                local.assert_not_called()
                names=[call.args[1] for call in events.call_args_list]
                self.assertIn('LOCAL_TTS_SKIPPED',names)
                self.assertNotIn('LOCAL_TTS_STARTED',names)

    def test_idle_and_failing_local_keep_existing_fallback(self):
        route = GpuPolicy().decide(self.settings,samples([4]),0)
        with patch('audio.choose_route',return_value=route), \
                patch('local_tts_client.synthesize_local',side_effect=ConnectionError) as local, \
                patch('audio._synthesize_openai_speech',return_value=audio_file(self)) as cloud:
            audio.synthesize_speech('你好。',self.settings)
            local.assert_called_once(); cloud.assert_called_once()

    def test_server_protection_race_overrides_disabled_error_fallback(self):
        from tts_events import GPUProtected
        route = GpuPolicy().decide(self.settings,samples([4]),0)
        with patch('audio.choose_route',return_value=route), \
                patch('local_tts_client.run_backend',side_effect=GPUProtected('server_gpu_protected')), \
                patch('local_tts_service.ensure_service',return_value={'model_loaded':True}), \
                patch('audio._synthesize_openai_speech',return_value=audio_file(self)) as cloud:
            result = audio.synthesize_speech('你好。',replace(self.settings,tts_fallback=False))
            self.assertEqual(result.backend,'openai')
            cloud.assert_called_once()

    def test_maximum_recovery_window_fits_monitor_history(self):
        monitor = GpuMonitor()
        monitor._history.extend(samples([20]*61))
        policy = GpuPolicy(); policy.protected = True
        self.assertEqual(policy.decide(replace(self.settings,gpu_recovery_seconds=60),
                                       monitor.history(),60).backend,'local')


class GpuSettingsTests(unittest.TestCase):
    def test_queued_replies_obey_new_gaming_mode_before_inference(self):
        from tts_pipeline import SpeechPipeline, SpeechJob
        from test_tts_reliability import RecordingPlayer
        app = QApplication.instance() or QApplication([])
        player=RecordingPlayer(); pipeline=SpeechPipeline(player)
        settings=replace(RuntimeSettings(),tts_mode='local',voice_backend='local')
        seen=[]
        def synthesize(text, settings, **kwargs):
            seen.append(settings.tts_mode)
            return audio_file(self)
        with patch('tts_pipeline.synthesize_speech',side_effect=synthesize):
            pipeline.submit(SpeechJob('第一句。',settings))
            pipeline.submit(SpeechJob('第二句。',settings))
            pipeline.update_resource_settings(replace(settings,tts_mode='gaming'))
            deadline=time.monotonic()+3
            while pipeline.is_busy() and time.monotonic()<deadline:
                app.processEvents(); time.sleep(.002)
        self.assertFalse(pipeline.is_busy())
        self.assertEqual(seen,['local','gaming'])

    def test_old_settings_migrate_and_thresholds_remain_ordered(self):
        self.assertEqual(RuntimeSettings.parse({'voice_backend':'local'}).tts_mode,'auto')
        self.assertEqual(RuntimeSettings.parse({'voice_backend':'openai'}).tts_mode,'openai')
        value = RuntimeSettings.parse(dict(tts_mode='auto',gpu_enter_percent=60,gpu_resume_percent=80,
            gpu_high_seconds=float('nan'),game_processes='CS2.EXE\ncs2.exe;C:/bad.exe;steam.exe'))
        self.assertEqual(value.gpu_resume_percent,50)
        self.assertEqual(value.game_processes,('cs2.exe','steam.exe'))
        self.assertEqual(value.gpu_high_seconds,3)
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder)/'settings.json'); store.save(value)
            self.assertEqual(store.load(),value)

    def test_four_modes_and_collapsed_parameters_round_trip(self):
        from voice_settings import VoiceSettingsDialog
        app = QApplication.instance() or QApplication([])
        dialog=VoiceSettingsDialog(RuntimeSettings.parse({'voice_backend':'local'}))
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.backend.count(),4)
        for mode in ('auto','local','gaming','openai'):
            dialog.backend.setCurrentIndex(dialog.backend.findData(mode))
            self.assertEqual(dialog.current().tts_mode,mode)
        dialog.games.setPlainText('CS2.exe\nmy-game.exe')
        self.assertEqual(dialog.current().game_processes,('cs2.exe','my-game.exe'))

    def test_monitor_lifecycle_retains_thread_and_stops(self):
        class Reader:
            def read(self): return dict(utilization=4,used_mb=2000,total_mb=8192,free_mb=6192)
            def close(self): self.closed=True
        reader=Reader(); monitor=GpuMonitor(.01,lambda:reader,lambda:frozenset({'test.exe'}))
        monitor.start(); first=monitor._thread; monitor.start()
        self.assertIs(monitor._thread,first)
        deadline=time.monotonic()+1
        while not monitor.history() and time.monotonic()<deadline: time.sleep(.01)
        monitor.stop()
        self.assertFalse(first.is_alive()); self.assertTrue(reader.closed)
        self.assertIn('test.exe',monitor.history()[0].processes)


if __name__=='__main__': unittest.main()
