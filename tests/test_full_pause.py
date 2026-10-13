"""Pause privacy/reliability invariants. Synthetic inputs and delayed mocks only."""
import threading,time
from dataclasses import replace
from unittest.mock import Mock,patch
import unittest
import numpy as np
from PySide6.QtTest import QTest
from behavior_probes import AppCase
from assistant_pause import PauseGate
from audio import MicRecorder,SystemAudioRecorder,synthesize_speech
from runtime_settings import RuntimeSettings
from speech_activity import Utterance
from voice_pipeline import VoiceWorker
from tts_events import TTSCancelled
from tts_pipeline import SpeechJob


class PauseAppTests(AppCase):
    def settle(self):
        until=time.monotonic()+3
        while self.app._resource_change is not None and time.monotonic()<until:QTest.qWait(10)
        self.assertIsNone(self.app._resource_change)

    def test_admission_closes_before_cleanup_and_no_input_or_capture(self):
        release=threading.Event();self.mic.stop.side_effect=lambda:release.wait(2)
        self.app._pause_all()
        self.assertEqual(self.app._pause_gate.state,'PAUSING')
        self.mic.read_pending.reset_mock();self.capture.reset_mock();self.ask.reset_mock()
        self.app._poll_microphone();self.app._start_auto_observe();self.app._start_observe()
        self.app._on_chat_submitted('暂停期间的问题');self.app._on_snip()
        self.mic.read_pending.assert_not_called();self.capture.assert_not_called();self.ask.assert_not_called()
        self.assertFalse(self.app._timer.isActive());self.assertFalse(self.app._voice_timer.isActive())
        release.set();self.settle();self.assertEqual(self.app._pause_gate.state,'PAUSED')

    def test_late_screen_result_cannot_update_subtitle_memory_or_tts(self):
        self.app._pause_all();self.settle()
        before=list(self.app._recent_turns);memory=self.app._character_memory
        self.app._on_comment('迟到回复','');self.app._on_analysis_failed('迟到失败')
        self.show_text.assert_not_called();self.tts.assert_not_called()
        self.assertEqual(before,list(self.app._recent_turns));self.assertIs(memory,self.app._character_memory)

    def test_resume_restores_switches_and_does_not_duplicate_or_replay(self):
        self.app._apply_settings(replace(self.app._settings,mic_enabled=False))
        self.app._set_system_enabled(False);self.app._auto=False
        self.mic.start.reset_mock();self.system.start.reset_mock()
        for _ in range(3):
            self.app._pause_all();self.settle();self.app._pause_all()
            self.app._resume_all();self.app._resume_all();self.settle()
            self.assertEqual(self.app._pause_gate.state,'RUNNING')
            self.assertFalse(self.app._timer.isActive());self.assertTrue(self.app.control.chat_input.isEnabled())
            self.assertFalse(self.app._voice_queue)
        self.mic.start.assert_not_called();self.system.start.assert_not_called();self.ask.assert_not_called()

    def test_close_failure_is_not_reported_paused_and_resume_not_allowed(self):
        self.mic.stop.side_effect=RuntimeError('injected handle failure')
        self.app._pause_all();self.settle()
        self.assertEqual(self.app._pause_gate.state,'FAILED');self.app._resume_all()
        self.assertEqual(self.app._pause_gate.state,'FAILED');self.system.stop.assert_called()
        self.mic.stop.side_effect=None;self.app._pause_all();self.settle()
        self.assertEqual(self.app._pause_gate.state,'PAUSED')

    def test_resume_failure_rolls_back_all_inputs(self):
        self.app._pause_all();self.settle();self.system.start.side_effect=RuntimeError('device missing')
        self.app._resume_all();self.settle()
        self.assertEqual(self.app._pause_gate.state,'FAILED')
        self.mic.quiesce.assert_called();self.mic.stop.assert_called()
        self.assertFalse(self.app._voice_timer.isActive());self.assertTrue(self.app._speech._paused)

    def test_auto_screen_is_independent_and_stop_reply_keeps_input_enabled(self):
        self.app._on_auto_toggled(False)
        self.mic.stop.assert_not_called();self.system.stop.assert_not_called()
        self.assertTrue(self.app._voice_timer.isActive())
        self.app._stop_current_reply();self.assertTrue(self.app._pause_gate.allows())
        self.assertTrue(self.app._voice_timer.isActive())

    def test_pending_capture_does_not_run_after_pause_resume(self):
        self.app._start_observe();self.app._pause_all();self.settle();self.app._resume_all();self.settle()
        QTest.qWait(240);self.capture.assert_not_called();self.ask.assert_not_called()

    def test_paused_pipeline_admission_is_cancel_not_error(self):
        self.app._pause_all();self.settle()
        cancelled=[];failed=[]
        self.app._speech.reply_cancelled.connect(lambda *args:cancelled.append(args))
        self.app._speech.reply_failed.connect(lambda *args:failed.append(args))
        job=SpeechJob('不得合成',RuntimeSettings())
        self.assertFalse(self.app._speech.submit(job));self.assertTrue(job.cancelled.is_set())
        self.assertEqual(cancelled[0][1],'USER_PAUSED');self.assertFalse(failed);self.tts.assert_not_called()

class CaptureAndWorkerPauseTests(unittest.TestCase):
    def test_quiesced_callbacks_cannot_refill_buffers(self):
        mic=MicRecorder();mic._accepting=True
        frame=np.ones((320,1),np.float32)*.1
        mic._callback(frame,320,None,None);self.assertTrue(mic.read_pending())
        mic.quiesce();mic.clear()
        for _ in range(20):mic._callback(frame,320,None,None)
        self.assertFalse(mic.read_pending());self.assertFalse(mic.snapshot().any())
        system=SystemAudioRecorder();system._running=True;system._write(frame.ravel());system.quiesce();system.clear()
        system._write(frame.ravel());self.assertFalse(system.snapshot().any())

    def test_late_stt_does_not_start_llm_or_deliver(self):
        gate=PauseGate();epoch=gate.epoch
        utterance=Mock(samples=np.zeros(16000),speech_end=0,detected_at=0)
        worker=VoiceWorker(utterance,RuntimeSettings(),'','',True);worker.valid=lambda:gate.allows(epoch)
        def transcribe(*args):gate.transition('PAUSING');return '旧转写'
        output=[];worker.text_ready.connect(output.append)
        with patch('voice_pipeline.transcribe_speech',side_effect=transcribe),patch('voice_pipeline.respond_to_voice') as llm:
            worker.run();llm.assert_not_called();self.assertFalse(output)

    def test_pause_while_local_fails_never_starts_cloud(self):
        cancelled=threading.Event()
        def local(*args):cancelled.set();raise RuntimeError('local late error')
        with patch('audio.choose_route',return_value=Mock(backend='local',reason='idle')),patch('local_tts_client.synthesize_local',side_effect=local),patch('audio._synthesize_openai_speech') as cloud:
            # dataclass route logging uses asdict, so use the real route type below.
            with patch('audio.asdict',return_value={}):
                with self.assertRaises(TTSCancelled):synthesize_speech('回答',replace(RuntimeSettings(),voice_backend='local'),cancelled=cancelled.is_set)
            cloud.assert_not_called()

    def test_epoch_does_not_revalidate_old_work_after_resume(self):
        gate=PauseGate();old=gate.epoch;gate.transition('PAUSING');gate.transition('PAUSED')
        gate.transition('RESUMING');gate.transition('RUNNING')
        self.assertFalse(gate.allows(old));self.assertTrue(gate.allows(gate.epoch))
