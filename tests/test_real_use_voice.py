"""Reported use cases with reproducible acoustic fixtures and blocked requests.

Acoustic fixtures are synthetic: these contracts are not real microphone WER.
"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from PySide6.QtTest import QTest
from PySide6.QtCore import QTimer

import audio
from runtime_settings import RuntimeSettings, SettingsStore
from speech_activity import SpeechActivity, Utterance
from companion_policy import conversation_opportunity
from character import CharacterMemory, Observation
from voice_pipeline import VoiceWorker, PREVIEW_TEXT
from behavior_probes import AppCase
from character_trace import TraceCollector, TraceStore, capture_model
from tts_fixtures import audio_file


def syllables(amplitude=.025, seconds=.8):
    at = np.arange(round(16000 * seconds)) / 16000
    envelope = .65 + .35 * np.sin(2 * np.pi * 4 * at)
    return (amplitude * envelope * (np.sin(2 * np.pi * 440 * at) + .3 * np.sin(2 * np.pi * 880 * at))).astype(np.float32)


def detect(samples, settings=RuntimeSettings()):
    vad = SpeechActivity(settings)
    signal = np.concatenate((np.zeros(3200, dtype=np.float32), samples, np.zeros(24000, dtype=np.float32)))
    results = []
    starts = 0
    for offset in range(0, len(signal), 320):
        results.extend(vad.feed(signal[offset:offset + 320], (offset + 320) / 16000))
        starts += int(vad.started)
    return results, starts


class AcousticTests(unittest.TestCase):
    def test_normal_speech_hangover_preserves_start_and_full_utterance(self):
        rows, starts = detect(syllables())
        self.assertEqual(len(rows), 1)
        self.assertEqual(starts, 1)
        self.assertGreaterEqual(len(rows[0].samples), len(syllables()))
        self.assertAlmostEqual(rows[0].detected_at - rows[0].speech_end, .6, places=2)

    def test_quiet_mic_is_user_adjustable_not_globally_lowered(self):
        quiet = syllables(.004)
        self.assertEqual(len(detect(quiet)[0]), 0)
        self.assertEqual(len(detect(quiet, replace(RuntimeSettings(), mic_sensitivity=4))[0]), 1)

    def test_broadband_noise_hum_and_short_click_do_not_submit(self):
        rng = np.random.default_rng(12)
        hum = .04 * np.sin(np.arange(16000) / 16000 * 2 * np.pi * 60)
        click = np.zeros(16000); click[800] = .4
        for signal in (rng.normal(0, .018, 16000), hum, click, syllables(.04, .1)):
            self.assertEqual(detect(signal)[0], [])
            self.assertEqual(detect(signal)[1], 0)

    def test_short_pause_does_not_split_a_sentence(self):
        signal = np.concatenate((syllables(seconds=.5), np.zeros(4800), syllables(seconds=.6)))
        rows, _ = detect(signal)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(len(rows[0].samples), len(signal))

    def test_different_silence_timeout_changes_measured_end_detection(self):
        for timeout in (.3, 1.2):
            rows, _ = detect(syllables(), replace(RuntimeSettings(), silence_timeout=timeout))
            self.assertAlmostEqual(rows[0].detected_at - rows[0].speech_end, timeout, places=2)

    def test_live_buffer_read_is_consuming_and_bounded(self):
        mic = audio.MicRecorder()
        mic._accepting = True  # Simulate an opened input stream; paused callbacks must be dropped.
        for _ in range(105):
            mic._callback(syllables(seconds=.02).reshape(-1, 1), 320, None, None)
        self.assertEqual(len(mic.read_pending()), 100)
        self.assertEqual(mic.dropped_chunks, 5)
        self.assertEqual(mic.read_pending(), [])

    def test_long_speech_is_bounded_and_explicitly_split(self):
        vad = SpeechActivity()
        rows = []
        for index in range(1550):
            rows.extend(vad.feed(syllables(seconds=.02), (index + 1) * .02))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].forced_split)
        self.assertLessEqual(len(rows[0].samples), 30 * 16000)


class SettingsAndSpeechTests(unittest.TestCase):
    def test_restart_round_trip_and_corrupt_file_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            settings = replace(RuntimeSettings(), voice_speed=.85, voice_tone='light', user_expertise='expert',
                               mic_sensitivity=3, silence_timeout=.9, echo_guard=False)
            SettingsStore(path).save(settings)
            self.assertEqual(SettingsStore(path).load(), settings)
            path.write_text('bad json')
            self.assertEqual(SettingsStore(path).load(), RuntimeSettings())

    def test_invalid_settings_are_bounded_without_nan_or_fake_pitch(self):
        settings = RuntimeSettings.parse(dict(voice_speed=9, mic_sensitivity=float('nan'), voice_name=[],
                                               start_threshold=.002, stop_threshold=.06, pitch=30))
        self.assertEqual(settings.voice_speed, 1.35)
        self.assertEqual(settings.mic_sensitivity, 1)
        self.assertLess(settings.stop_threshold, settings.start_threshold)
        self.assertNotIn('pitch', asdict(settings))

    def test_tts_speed_and_tone_use_actual_supported_api_parameters(self):
        with patch('audio.run_backend', side_effect=lambda *a, **kw: audio_file(self)) as transport:
            for speed, tone in ((.75, 'original'), (1.35, 'light'), (1.0, 'soft')):
                path = audio.synthesize_speech(PREVIEW_TEXT, replace(RuntimeSettings(), voice_speed=speed,
                                                                    voice_tone=tone, speech_quirk_enabled=False))
                try:
                    request = transport.call_args.kwargs
                    self.assertEqual(request['text'], PREVIEW_TEXT)
                    self.assertEqual(request['speed'], speed)
                    self.assertNotIn('pitch', request)
                    if tone == 'light':
                        self.assertIn('youthful adult', request['instructions'])
                finally:
                    Path(path).unlink()

    def test_stt_does_not_force_chinese_on_english_or_korean(self):
        with patch('audio._client') as client:
            client.return_value.audio.transcriptions.create.return_value.text = 'What happened?'
            self.assertEqual(audio.transcribe_speech(syllables()), 'What happened?')
            self.assertNotIn('language', client.return_value.audio.transcriptions.create.call_args.kwargs)

    def test_stream_first_content_timing_and_cancellation_close_transport(self):
        import vision
        def chunk(text):
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)
        with patch.dict('os.environ', OPENAI_API_KEY='test-only'), patch('vision.OpenAI') as client:
            stream = client.return_value.__enter__.return_value.chat.completions.create.return_value
            stream.__enter__.return_value.__iter__.return_value = iter([chunk(''), chunk('嗯'), chunk('，在。')])
            metrics, trace = {}, {}
            with capture_model(trace), patch('vision.monotonic', side_effect=[10, 10.02, 10.1]):
                reply = vision.respond_to_voice('在吗', '', '[]', RuntimeSettings(), metrics, lambda: False)
            self.assertEqual(reply, '嗯，在。')
            self.assertAlmostEqual(metrics['llm_first_token_ms'], 20)
            self.assertEqual(trace['raw_response'], reply)
            stream.__exit__.assert_called_once()
            stream.__enter__.return_value.__iter__.return_value = iter([chunk('不要展示')])
            result = vision.respond_to_voice('在吗', '', '[]', RuntimeSettings(), {}, lambda: True)
            self.assertEqual(result, '')
            self.assertEqual(stream.__exit__.call_count, 2)


class VoiceForegroundTests(AppCase):
    def setUp(self):
        super().setUp()
        # Voice metrics and utterance time share the same clock in these timing probes.
        self.clock.side_effect = time.monotonic
        self.stt = self.mock('voice_pipeline.transcribe_speech', return_value='这个怎么打？')
        self.reply = self.mock('voice_pipeline.respond_to_voice', side_effect=self.respond)
        self.speech = self.mock('overlay.synthesize_speech', side_effect=lambda *a, **kw: audio_file(self))
        self.app._settings = replace(self.app._settings, speech_quirk_enabled=False)
        def play(path, reply_id):
            self.app._speech._started(reply_id)
            QTimer.singleShot(1, lambda: self.app._speech._finished(reply_id))
        self.player.play_file.side_effect = play

    @staticmethod
    def respond(spoken, recent, observations, settings, metrics, cancelled, mode, **kwargs):
        metrics['llm_first_token_ms'] = 4.0
        metrics['llm_total_ms'] = 8.0
        return '先看这一波的节奏。'

    def utterance(self):
        now = time.monotonic()
        return Utterance(syllables(), now - .6, now)

    def settle(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_voice_preempts_inflight_screen_without_waiting_or_leaking_result(self):
        entered, release = threading.Event(), threading.Event()
        self.ask.side_effect = lambda *a, **k: (entered.set(), release.wait(4), '迟到的屏幕回复')[2]
        self.app._auto = True
        self.app._start_auto_observe()
        try:
            self.settle(entered.is_set)
            began = time.monotonic()
            self.app._enqueue_voice(self.utterance())
            self.settle(lambda: self.app._latest_voice_metrics is not None)
            self.assertFalse(release.is_set())
            self.assertEqual(self.app.control.companion_text.text(), '先看这一波的节奏。')
            self.assertLess(time.monotonic() - began, .8)
            self.assertEqual(self.capture.call_count, 1)  # Voice did not request another screen.
            self.assertEqual(self.stt.call_count, 1)
        finally:
            release.set()
            self.settle(lambda: not self.app._retired_workers)
        self.assertEqual(self.app.control.companion_text.text(), '先看这一波的节奏。')
        self.assertEqual(len(self.app._recent_turns), 1)

    def test_voice_without_auto_mode_responds_and_records_each_stage(self):
        self.assertFalse(self.app._auto)
        self.app._enqueue_voice(self.utterance())
        self.settle(lambda: self.app._latest_voice_metrics is not None)
        metrics = self.app._latest_voice_metrics
        self.assertAlmostEqual(metrics['vad_end_detection_ms'], 600, places=0)
        for name in ('queue_wait_ms', 'stt_ms', 'context_assembly_ms', 'llm_first_token_ms', 'llm_total_ms', 'speech_to_text_ms'):
            self.assertIsNotNone(metrics[name], name)
        self.assertIsNone(metrics['tts_ms'])  # Sound is off in AppCase.
        self.assertIsNone(metrics['speech_to_audio_ms'])
        self.capture.assert_not_called()

    def test_text_arrives_before_slow_tts_finishes(self):
        entered, release = threading.Event(), threading.Event()
        self.app.control._on_speak_clicked()
        self.speech.side_effect = lambda *a, **kw: (entered.set(), release.wait(3), audio_file(self))[2]
        try:
            self.app._enqueue_voice(self.utterance())
            self.settle(entered.is_set)
            self.settle(lambda: self.app.control.companion_text.text() == '先看这一波的节奏。')
            self.assertFalse(release.is_set())
        finally:
            release.set()
            self.settle(lambda: self.app._voice_worker is None)
            self.settle(lambda: not self.app._speech.is_busy())

    def test_typing_can_preempt_background_from_actual_composer(self):
        entered, release = threading.Event(), threading.Event()
        self.ask.side_effect = lambda *a, **k: (entered.set(), release.wait(3), '屏幕输出')[2]
        self.app._auto = True
        self.app._start_auto_observe()
        try:
            self.settle(entered.is_set)
            self.assertTrue(self.app.control.chat_input.isEnabled())
            self.app.control.chat_input.setText('请解释这个问题')
            self.app.control.chat_send_button.click()
            self.assertEqual(len(self.app._retired_workers), 1)
            self.assertEqual(self.app._voice_worker.text_input, '请解释这个问题')
            self.assertIsNone(self.app._pending_command)  # Plain chat no longer schedules another screenshot.
            self.assertEqual(self.capture.call_count, 1)
        finally:
            release.set()
            self.wait_idle()
            self.settle(lambda: not self.app._retired_workers)

    def test_new_voice_cancels_old_speech_but_screen_poll_does_not(self):
        entered, release = threading.Event(), threading.Event()
        self.app.control._on_speak_clicked()
        self.speech.side_effect = lambda *a, **kw: (entered.set(), release.wait(4), audio_file(self))[2]
        try:
            self.app._enqueue_voice(self.utterance())
            self.settle(entered.is_set)
            self.settle(lambda: self.app._voice_worker is None)
            first = self.app._speech._active
            self.app._enqueue_voice(self.utterance())
            self.settle(lambda: self.app._voice_worker is None and len(self.app._speech._queue) == 1)
            self.app._auto = True
            self.app._start_auto_observe()
            self.assertTrue(first.cancelled.is_set())
            self.assertIs(self.app._speech._active, first)
            self.player.stop.assert_not_called()
            self.capture.assert_not_called()
        finally:
            release.set()
            self.settle(lambda: self.app._voice_worker is None and not self.app._speech.is_busy())
        self.assertEqual(self.player.play_file.call_count, 1)  # Superseded audio is never played.
        ids = [call.args[1] for call in self.player.play_file.call_args_list]
        self.assertEqual(len(set(ids)), 1)

    def test_real_mic_poll_completes_utterance_without_screen_timer(self):
        self.mic.segmented_input = True
        self.mic.dropped_chunks = 0
        self.app._settings = replace(self.app._settings, echo_guard=False)
        audio_data = np.concatenate((syllables(), np.zeros(16000, dtype=np.float32)))
        now = time.monotonic()
        self.mic.read_pending.return_value = [(audio_data, now)]
        self.app._poll_microphone()
        self.mic.read_pending.return_value = []
        self.settle(lambda: self.app._latest_voice_metrics is not None)
        self.stt.assert_called_once()
        self.capture.assert_not_called()

    def test_microphone_test_shows_levels_but_never_sends_requests(self):
        self.app._open_voice_settings()
        dialog = self.app._voice_dialog
        dialog.test_button.setChecked(True)
        self.mic.segmented_input = True
        self.mic.dropped_chunks = 0
        self.mic.read_pending.return_value = [(syllables(), time.monotonic())]
        self.app._poll_microphone()
        self.mic.read_pending.return_value = []
        self.assertIn('当前 RMS', dialog.mic_status.text())
        self.assertGreater(dialog.meter.value(), 0)
        self.stt.assert_not_called()

    def test_voice_preview_uses_unsaved_speed_tone_and_no_fake_pitch(self):
        self.app._open_voice_settings()
        dialog = self.app._voice_dialog
        dialog.values['voice_speed'].setValue(.8)
        dialog.tone.setCurrentIndex(dialog.tone.findData('light'))
        dialog.preview_button.click()
        self.settle(lambda: not self.app._speech.is_busy())
        self.assertEqual(self.speech.call_args.args[0], PREVIEW_TEXT)
        self.assertEqual(self.speech.call_args.args[1].voice_speed, .8)
        self.assertEqual(self.speech.call_args.args[1].voice_tone, 'light')
        self.assertEqual(self.app._settings.voice_speed, 1.2)
        dialog.save_button.click()
        self.assertEqual(self.app._settings.voice_speed, .8)
        self.assertEqual(self.app._settings.user_expertise, 'unknown')

    def test_closing_microphone_test_discards_test_audio(self):
        self.app._open_voice_settings()
        dialog = self.app._voice_dialog
        dialog.test_button.setChecked(True)
        self.app._vad.feed(syllables(), time.monotonic())
        self.assertTrue(self.app._vad.active)
        dialog.close()
        self.assertFalse(self.app._vad.active)
        self.assertFalse(dialog.test_button.isChecked())
        self.stt.assert_not_called()

    def test_voice_queue_is_bounded_without_silent_eviction(self):
        # Four deliberately blocked retired transports exercise bounded admission.
        self.app._retired_voice = [SimpleNamespace() for _ in range(4)]
        first = self.utterance()
        try:
            for item in (first, self.utterance(), self.utterance()):
                self.app._enqueue_voice(item)
            self.assertEqual(len(self.app._voice_queue), 2)
            self.assertIs(self.app._voice_queue[0], first)
            self.assertIn('队列已满', self.show_text.call_args.args[0])
        finally:
            self.app._retired_voice = []
            self.app._voice_queue.clear()

    def test_trace_saves_voice_stages_and_does_not_invent_playback_latency(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app._trace = TraceCollector(TraceStore(Path(folder) / 'trace.sqlite3'))
            self.app._trace.set_enabled(True)
            self.app._enqueue_voice(self.utterance())
            self.settle(lambda: bool(self.app._trace.store.recent()))
            row = self.app._trace.store.recent()[0]
            trace = self.app._trace.store.get(row['id'])['trace']
            self.assertEqual(trace['source'], 'user_speech')
            self.assertEqual(trace['context']['user_spoken'], '这个怎么打？')
            self.assertEqual(trace['delivery']['response'], '先看这一波的节奏。')
            self.assertIsNotNone(trace['latency']['stt_ms'])
            self.assertIsNone(trace['latency']['speech_to_audio_ms'])

    def test_unsolicited_helldivers_coaching_is_not_delivered(self):
        self.ask.return_value = Observation('game', 'surprise', 'HELLDIVERS 2 遭遇敌群', '左侧敌人靠近',
                                            'high', False, 'REACT', '你应该先解决左边的敌人，然后撤退。')
        self.automatic(time.monotonic())
        self.show_text.assert_not_called()
        self.assertEqual(self.app._character_memory.entries[-1].action, 'WAIT')
        self.reply.side_effect = None
        self.reply.return_value = '可以先处理阻挡撤离路线的敌人。'
        self.app._on_chat_submitted('这个怎么打？')
        self.wait_idle()
        self.assertIn('撤离路线', self.app.control.companion_text.text())


class OpportunityTests(unittest.TestCase):
    def event(self, **changes):
        values = dict(scene='game', event='surprise', subject='HELLDIVERS 2 队友登船', evidence='队友撞在舱门上',
                      confidence='high', focused=False, intent='REACT', reply='门：不许走。')
        values.update(changes)
        return Observation(**values)

    def test_long_silence_interesting_event_is_opportunity(self):
        result = conversation_opportunity(self.event(), CharacterMemory(), 300, 0)
        self.assertGreaterEqual(result.score, .75)

    def test_long_silence_alone_focus_noise_and_repetition_do_not_force_speech(self):
        memory = CharacterMemory()
        observation = self.event()
        for event in (self.event(event='none'), self.event(focused=True), self.event(confidence='low')):
            self.assertEqual(conversation_opportunity(event, memory, 3600, 0).score, 0)
        memory.accept(observation, 0)
        self.assertEqual(conversation_opportunity(observation, memory, 3600, 0).score, 0)
        self.assertEqual(conversation_opportunity(observation, CharacterMemory(), 3600, 3).score, 0)

    def test_normal_game_is_not_help_but_explicit_help_is_allowed(self):
        import vision
        from test_character_trace import model_reply
        with patch.dict('os.environ', OPENAI_API_KEY='test-only'), patch('vision.OpenAI') as client:
            create = client.return_value.chat.completions.create
            create.return_value = model_reply('这下热闹了。')
            vision.comment_on_screenshot(b'HELLDIVERS 2 fixture', automatic=True, user_expertise='expert')
            request = create.call_args.kwargs
            prompt = request['messages'][0]['content'] + request['messages'][1]['content'][0]['text']
            self.assertIn('expert', prompt)
            self.assertIn('不教操作', prompt)
            self.assertIn('本轮无人求助', prompt)
            vision.comment_on_screenshot(b'HELLDIVERS 2 fixture', user_command='这个怎么打？', user_expertise='expert')
            prompt = create.call_args.kwargs['messages'][1]['content'][0]['text']
            self.assertIn('确有求助就认真回答', prompt)
            self.assertIn('这个怎么打', prompt)
