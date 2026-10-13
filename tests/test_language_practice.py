"""Offline practice contracts: actual speech, silence of hints, cancellation and isolation."""
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from assistant_pause import PauseGate
from controls import ControlBar
from language_practice.core import PracticeSettings, PracticeState, PracticeStore, PracticeTurn, connection
from language_practice.controller import PracticeController
from language_practice.worker import PracticeWorker

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
FIRST = PracticeTurn('어떤 음악을 좋아해요?', '喜欢什么音乐？', '저는 조용한 음악을 좋아해요.', '我喜欢安静的音乐。')
NEXT = PracticeTurn('왜 신나는 노래를 좋아해요?', '为什么喜欢欢快的歌？', '들으면 힘이 나요.', '听了会有精神。', '表达清楚。')


def until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(.01)
    assert predicate(), 'worker did not settle'


class CoreTests(unittest.TestCase):
    def test_reference_is_not_claimed_as_learner_speech(self):
        state = PracticeState()
        state.accept(FIRST, 'start')
        prompt = state.messages(PracticeSettings(), 'answer', '저는 신나는 노래를 좋아해요.')
        data = json.loads(prompt[-1]['content'])
        self.assertEqual(data['actual_learner_answer'], '저는 신나는 노래를 좋아해요.')
        self.assertNotIn(FIRST.suggested_answer, prompt[-1]['content'])
        self.assertEqual(state.history, [])
        state.accept(NEXT, 'answer', data['actual_learner_answer'])
        self.assertEqual(state.history[0]['actual_answer'], data['actual_learner_answer'])
        self.assertEqual(state.answer_count, 1)

    def test_simplification_does_not_change_question_or_commit_turn(self):
        state = PracticeState()
        state.accept(FIRST, 'start')
        state.accept(NEXT, 'simplify')
        self.assertEqual(state.current.question, FIRST.question)
        self.assertEqual(state.current.suggested_answer, NEXT.suggested_answer)
        self.assertFalse(state.history)
        self.assertEqual(state.answer_count, 0)

    def test_history_is_bounded_without_limiting_session_length(self):
        state = PracticeState()
        state.accept(FIRST, 'start')
        for i in range(30):
            state.accept(NEXT, 'answer', str(i))
        self.assertEqual(len(state.history), 16)
        self.assertEqual(state.answer_count, 30)

    def test_invalid_or_partial_generation_cannot_be_displayed(self):
        for raw in ('null', '{}', 'not json', '{"question":123}', '[]'):
            with self.assertRaises(ValueError):
                PracticeTurn.parse(raw)
        self.assertEqual(PracticeTurn.parse(json.dumps(asdict(FIRST))), FIRST)

    def test_settings_are_independent_and_keys_not_in_repr(self):
        with tempfile.TemporaryDirectory() as path:
            store = PracticeStore(Path(path) / 'practice.json')
            settings = replace(PracticeSettings(), audio_base_url='https://audio.example/v1', audio_api_key='secret-fixture')
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertNotIn('secret-fixture', repr(settings))
            store.path.write_text('{"show_chinese":"no", "topic":3}')
            self.assertEqual(store.load(), PracticeSettings())

    def test_custom_audio_host_requires_its_own_key(self):
        with self.assertRaises(ValueError):
            replace(PracticeSettings(), audio_base_url='https://audio.example/v1').validate()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'main-key', 'OPENAI_BASE_URL': 'https://main.example/v1'}):
            settings = replace(PracticeSettings(), audio_base_url='https://audio.example/v1', audio_api_key='audio-key')
            self.assertEqual(connection(settings)['api_key'], 'main-key')
            self.assertEqual(connection(settings, audio=True)['api_key'], 'audio-key')


class FakeWorker(QObject):
    result = Signal(str, object)
    failed = Signal(str, str)
    status = Signal(str, str)
    finished = Signal(str)

    def __init__(self, token, kind, settings, payload, parent):
        super().__init__(parent)
        self.token, self.kind, self.payload = token, kind, payload
        self.thread = None
        self.stopped = self.record_done = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def stop_recording(self):
        self.record_done = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-placeholder'})
        self.env.start()
        self.control = ControlBar(text_only=True)
        self.owner = SimpleNamespace(control=self.control, _pause_gate=PauseGate(),
                                     _stop_current_reply=MagicMock(), translation=SimpleNamespace(stop=MagicMock(), worker=None))
        self.controller = PracticeController(self.owner, PracticeStore(Path(self.temp.name) / 'practice.json'), FakeWorker)
        self.controller.open()

    def tearDown(self):
        self.controller.stop()
        for token in tuple(self.controller.workers):
            self.controller._finished(token)
        self.controller.window.close()
        self.controller.window.deleteLater()
        self.control.close()
        self.control.deleteLater()
        APP.processEvents()
        self.env.stop()
        self.temp.cleanup()

    def pending(self):
        return self.controller.workers[self.controller.token]

    def finish(self, value):
        worker = self.pending()
        worker.result.emit(worker.token, value)
        worker.finished.emit(worker.token)
        return worker

    def start_question(self):
        self.controller.start()
        self.finish(FIRST)

    def test_opening_is_idle_and_first_reference_is_silent(self):
        self.assertFalse(self.controller.active)
        self.assertFalse(self.controller.workers)
        self.start_question()
        self.assertFalse(self.controller.workers)
        self.assertIn(FIRST.suggested_answer, self.controller.window.captions.toPlainText())
        self.assertFalse(self.controller.state.history)
        self.assertFalse(self.control.chat_send_button.isEnabled())
        self.assertFalse(self.control.tts_allowed())

    def test_only_question_can_be_sent_to_speech_output(self):
        self.start_question()
        self.controller.read_question()
        worker = self.pending()
        self.assertEqual(worker.kind, 'question_audio')
        self.assertEqual(worker.payload, FIRST.question)
        self.assertNotEqual(worker.payload, FIRST.suggested_answer)

    def test_recording_needs_review_and_does_not_advance(self):
        self.start_question()
        self.controller.record()
        worker = self.pending()
        self.assertTrue(self.controller.recording)
        self.controller.record()
        self.assertTrue(worker.record_done)
        self.finish('실제로 말한 답변')
        self.assertEqual(self.controller.window.answer.toPlainText(), '실제로 말한 답변')
        self.assertEqual(self.controller.state.answer_count, 0)
        self.controller.submit()
        self.assertEqual(self.controller.state.answer_count, 0)
        self.finish(NEXT)
        self.assertEqual(self.controller.state.history[0]['actual_answer'], '실제로 말한 답변')
        self.assertEqual(self.controller.state.answer_count, 1)

    def test_failed_followup_keeps_answer_for_retry_without_duplicate_history(self):
        self.start_question()
        self.controller.window.answer.setPlainText('实际回答')
        self.controller.submit()
        worker = self.pending()
        worker.failed.emit(worker.token, 'offline failure')
        worker.finished.emit(worker.token)
        self.assertEqual(self.controller.window.answer.toPlainText(), '实际回答')
        self.assertFalse(self.controller.state.history)
        self.controller.submit()
        self.finish(NEXT)
        self.assertEqual(len(self.controller.state.history), 1)

    def test_stop_drops_late_result_and_waits_before_restart(self):
        self.controller.start()
        worker = self.pending()
        self.controller.stop()
        self.assertTrue(worker.stopped)
        self.assertFalse(self.controller.window.start_button.isEnabled())
        worker.result.emit(worker.token, FIRST)
        self.assertIsNone(self.controller.state.current)
        worker.finished.emit(worker.token)
        self.assertTrue(self.controller.quiescent())
        self.assertTrue(self.controller.window.start_button.isEnabled())
        self.assertTrue(self.control.chat_send_button.isEnabled())

    def test_pause_stops_recording_without_resuming_on_unpause(self):
        self.start_question()
        self.controller.record()
        worker = self.pending()
        self.owner._pause_gate.transition('PAUSING')
        self.control.set_lifecycle('PAUSING')
        self.assertTrue(worker.stopped)
        worker.result.emit(worker.token, 'late transcript')
        worker.finished.emit(worker.token)
        self.owner._pause_gate.transition('RUNNING')
        self.control.set_lifecycle('RUNNING')
        self.assertFalse(self.controller.active)
        self.assertFalse(self.controller.recording)
        self.assertEqual(self.controller.window.answer.toPlainText(), '')

    def test_simplify_and_chinese_toggle_leave_learner_input_intact(self):
        self.start_question()
        self.controller.window.answer.setPlainText('my draft')
        self.controller.simplify()
        self.finish(NEXT)
        self.controller.window.chinese.setChecked(False)
        self.assertEqual(self.controller.window.answer.toPlainText(), 'my draft')
        self.assertEqual(self.controller.state.current.question, FIRST.question)
        self.assertNotIn(FIRST.question_zh, self.controller.window.captions.toPlainText())

    def test_empty_answer_does_not_submit_hint(self):
        self.start_question()
        self.controller.submit()
        self.assertFalse(self.controller.workers)
        self.assertFalse(self.controller.state.history)

    def test_initial_failure_can_be_retried(self):
        self.controller.start()
        worker = self.pending()
        worker.failed.emit(worker.token, 'failed')
        worker.finished.emit(worker.token)
        self.assertTrue(self.controller.window.start_button.isEnabled())
        self.assertFalse(self.controller.active)


class WorkerTests(unittest.TestCase):
    def test_recording_closes_input_before_returning_wav(self):
        from array import array
        import io, wave
        worker = PracticeWorker('r', 'record', PracticeSettings(), None)
        closed = []
        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def __enter__(self):
                self.callback(array('h', [1500] * 8000).tobytes(), 8000, None, False)
                worker.stop_recording()
                return self
            def __exit__(self, *args):
                closed.append(True)
        fake = SimpleNamespace(query_devices=lambda **kwargs: {'default_samplerate':16000}, RawInputStream=Stream)
        with patch.dict(sys.modules, {'sounddevice':fake}):
            data = worker._record()
        self.assertTrue(closed)
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnframes(), 8000)

    def test_cancel_during_recording_releases_input_and_discards_audio(self):
        worker = PracticeWorker('r', 'record', PracticeSettings(), None)
        closed = []
        class Stream:
            def __init__(self, **kwargs):pass
            def __enter__(self):worker.stop();return self
            def __exit__(self, *args):closed.append(True)
        fake = SimpleNamespace(query_devices=lambda **kwargs: {'default_samplerate':16000}, RawInputStream=Stream)
        with patch.dict(sys.modules, {'sounddevice':fake}):
            self.assertEqual(worker._record(), b'')
        self.assertTrue(closed)

    def test_cancelled_playback_does_not_open_audio_device(self):
        worker = PracticeWorker('p', 'question_audio', PracticeSettings(), None)
        worker.stop()
        fake = MagicMock()
        with patch.dict(sys.modules, {'sounddevice':fake}):
            worker._play(b'\x00\x01')
        fake.RawOutputStream.assert_not_called()

    def test_fake_provider_requests_and_speech_boundary(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(asdict(FIRST))))]))
        client.audio.transcriptions.create = AsyncMock(return_value=SimpleNamespace(text='actual speech'))
        client.audio.speech.create = AsyncMock(return_value=SimpleNamespace(content=b'\x00\x01'))
        settings = PracticeSettings()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'offline'}), patch('openai.AsyncOpenAI', return_value=client):
            worker = PracticeWorker('a', 'generate', settings, [])
            self.assertEqual(asyncio.run(worker._request([])), FIRST)
            client.audio.speech.create.assert_not_called()
            worker = PracticeWorker('b', 'record', settings, None)
            self.assertEqual(asyncio.run(worker._request(b'fake wav')), 'actual speech')
            self.assertEqual(client.audio.transcriptions.create.call_args.kwargs['language'], 'ko')
            client.audio.speech.create.assert_not_called()
            worker = PracticeWorker('c', 'question_audio', settings, FIRST.question)
            self.assertEqual(asyncio.run(worker._request(FIRST.question)), b'\x00\x01')
            self.assertEqual(client.audio.speech.create.call_args.kwargs['input'], FIRST.question)

    def test_cancelled_worker_never_opens_client(self):
        worker = PracticeWorker('x', 'generate', PracticeSettings(), [])
        worker.stop()
        with patch('openai.AsyncOpenAI') as client:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(worker._request([]))
            client.assert_not_called()


class VoiceIntegrationTests(unittest.TestCase):
    def test_main_capture_is_suspended_and_only_prior_inputs_are_restored(self):
        from overlay import OverlayApp
        owner = SimpleNamespace(_mic=MagicMock(),_system=MagicMock(),_system_ok=False,
                                _voice_timer=MagicMock(),_voice_queue=[],_vad=MagicMock(),
                                _pause_gate=PauseGate(),_settings=SimpleNamespace(mic_enabled=True),
                                _system_enabled=True,_quitting=False)
        owner._mic._stream = object()
        owner._voice_timer.isActive.return_value = True
        OverlayApp._hold_for_practice(owner,True)
        owner._mic.stop.assert_called_once()
        owner._system.stop.assert_called_once()
        OverlayApp._hold_for_practice(owner,False)
        owner._mic.start.assert_called_once_with(allow_fallback=False)
        owner._system.start.assert_not_called()
        owner._voice_timer.start.assert_called_once()

    def test_leaving_practice_during_full_pause_does_not_restart_main_microphone(self):
        from overlay import OverlayApp
        owner = SimpleNamespace(_practice_inputs=(True,True,True),_pause_gate=PauseGate(True),_quitting=False,
                                _mic=MagicMock(),_system=MagicMock(),_voice_timer=MagicMock())
        OverlayApp._hold_for_practice(owner,False)
        owner._mic.start.assert_not_called()
        owner._system.start.assert_not_called()

    def test_voice_controls_return_after_ending_practice(self):
        control=ControlBar(text_only=False)
        control._practice_active=True;control._refresh_status()
        self.assertFalse(control.mic_button.isEnabled())
        self.assertFalse(control.system_button.isEnabled())
        control._practice_active=False;control._refresh_status()
        self.assertTrue(control.mic_button.isEnabled())
        self.assertTrue(control.system_button.isEnabled())
        control.close();control.deleteLater();APP.processEvents()


if __name__ == '__main__':
    unittest.main()
