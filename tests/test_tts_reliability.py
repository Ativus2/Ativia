"""Fault injection and owned-queue tests; no private data or cloud calls."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import sounddevice as sd
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

import audio
import local_tts_client
from runtime_settings import RuntimeSettings
from speech_playback import SpeechPlayer, device_pcm, default_output_device
from tts_audio import InvalidAudio, SpeechAudio, validate_wav_bytes, validate_audio_file, finalize_openai_streaming_wav
from tts_events import TTSCancelled, TTSFailure
from tts_pipeline import SpeechJob, SpeechPipeline
from tts_process import operation_context, run_backend
from tts_transport import download_openai
from tts_fixtures import wav_bytes, audio_file


class AudioValidationTests(unittest.TestCase):
    def test_openai_transport_requests_wav_and_consumes_all_chunks(self):
        with patch('openai.OpenAI') as client:
            create = client.return_value.__enter__.return_value.audio.speech.with_streaming_response.create
            response = create.return_value.__enter__.return_value
            response.iter_bytes.return_value = [b'part1', b'part2']
            result = download_openai(dict(timeout=10, model='gpt-4o-mini-tts', voice='nova',
                                          text='test', instructions='same voice', speed=1.2))
            self.assertEqual(result, b'part1part2')
            self.assertEqual(create.call_args.kwargs['response_format'], 'wav')
            create.return_value.__exit__.assert_called_once()

    def test_only_explicit_streaming_sentinel_is_finalized(self):
        valid = wav_bytes()
        streaming = bytearray(valid)
        struct.pack_into('<I', streaming, 4, 0xffffffff)
        struct.pack_into('<I', streaming, 40, 0xffffffff)
        self.assertEqual(finalize_openai_streaming_wav(bytes(streaming)), valid)
        with self.assertRaises(InvalidAudio):
            validate_wav_bytes(bytes(streaming))
        with self.assertRaises(InvalidAudio):
            validate_wav_bytes(finalize_openai_streaming_wav(valid[:-2]))
    def test_bad_200_payloads_are_not_playable(self):
        valid = wav_bytes()
        zero_frames = bytearray(valid)
        struct.pack_into('<I', zero_frames, 40, 0)
        cases = [None, b'', b'RIFF', b'{}', b'x' * 10000, valid[:44], valid[:-2],
                 bytes(zero_frames), wav_bytes(seconds=.001), wav_bytes(amplitude=0)]
        for data in cases:
            with self.subTest(size=None if data is None else len(data)):
                with self.assertRaises(InvalidAudio):
                    validate_wav_bytes(data)
        self.assertGreater(validate_wav_bytes(valid).duration, 0)

    def test_empty_missing_unreadable_or_corrupt_files_fail(self):
        path = audio_file(self)
        for value in (None, '', path + '.missing', 42):
            with self.subTest(value=value), self.assertRaises(InvalidAudio):
                validate_audio_file(value)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError), self.assertRaises(InvalidAudio):
            validate_audio_file(path)
        Path(path).write_bytes(b'')
        with self.assertRaises(InvalidAudio):
            validate_audio_file(path)

    def test_all_invalid_local_results_fallback_even_without_exception(self):
        settings = replace(RuntimeSettings(), voice_backend='local', speech_quirk_enabled=False)
        for local in (None, '', 'missing.wav', b'', 0):
            fallback = audio_file(self)
            with self.subTest(local=local), patch('local_tts_client.synthesize_local', return_value=local), \
                    patch('audio._synthesize_openai_speech', return_value=fallback) as old, patch('audio.event') as log:
                result = audio.synthesize_speech('测试。', settings, reply_id='test-invalid')
                self.assertEqual(result.backend, 'openai')
                old.assert_called_once()
                self.assertIn('LOCAL_TTS_INVALID_AUDIO', [c.args[1] for c in log.call_args_list])

    def test_connection_reset_timeout_and_arbitrary_exception_fallback(self):
        settings = replace(RuntimeSettings(), voice_backend='local')
        for exc in (TimeoutError(), ConnectionResetError(), OSError(), RuntimeError('failed')):
            fallback = audio_file(self)
            with self.subTest(error=type(exc)), patch('local_tts_client.synthesize_local', side_effect=exc), \
                    patch('audio._synthesize_openai_speech', return_value=fallback):
                self.assertEqual(audio.synthesize_speech('测试。', settings).backend, 'openai')

    def test_invalid_fallback_has_explicit_error(self):
        settings = replace(RuntimeSettings(), voice_backend='local')
        with patch('local_tts_client.synthesize_local', return_value=None), \
                patch('audio._synthesize_openai_speech', return_value=None), patch('audio.event') as log:
            with self.assertRaises(TTSFailure):
                audio.synthesize_speech('测试。', settings)
            names = [c.args[1] for c in log.call_args_list]
            self.assertIn('FALLBACK_TTS_FAILED', names)
            self.assertIn('ERROR', names)

    def test_cancel_is_not_failure_and_does_not_start_fallback(self):
        settings = replace(RuntimeSettings(), voice_backend='local')
        with patch('local_tts_client.synthesize_local', side_effect=TTSCancelled('muted')), \
                patch('audio._synthesize_openai_speech') as old, patch('audio.event') as log:
            with self.assertRaises(TTSCancelled):
                audio.synthesize_speech('测试。', settings)
            old.assert_not_called()
            self.assertIn('TTS_CANCELLED', [c.args[1] for c in log.call_args_list])


class LocalTransportTests(unittest.TestCase):
    def setUp(self):
        case = self
        self.content = wav_bytes()
        self.status = 200
        self.kind = 'audio/wav'
        self.delay = 0
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                time.sleep(case.delay)
                try:
                    self.send_response(case.status)
                    self.send_header('Content-Type', case.kind)
                    self.end_headers()
                    self.wfile.write(case.content)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'
        p = patch.object(local_tts_client, 'ENDPOINT', self.endpoint)
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_http_200_empty_corrupt_tiny_zero_silence_and_wrong_type_fallback(self):
        settings = replace(RuntimeSettings(), voice_backend='local', speech_quirk_enabled=False)
        valid = wav_bytes()
        for content, kind in ((b'', 'audio/wav'), (b'{}', 'audio/wav'), (valid[:44], 'audio/wav'),
                              (valid[:-10], 'audio/wav'), (wav_bytes(seconds=.001), 'audio/wav'),
                              (wav_bytes(amplitude=0), 'audio/wav'), (valid, 'application/json')):
            self.content, self.kind = content, kind
            fallback = audio_file(self)
            with self.subTest(bytes=len(content), kind=kind), patch('audio._synthesize_openai_speech', return_value=fallback) as old:
                result = audio.synthesize_speech('测试。', settings)
                self.assertEqual(result.backend, 'openai')
                old.assert_called_once()

    def test_hung_request_has_absolute_deadline_then_fallback(self):
        self.delay = 5
        started = time.monotonic()
        with patch.dict(os.environ, ATIVIA_LOCAL_TTS_DEADLINE_SECONDS='.6'), \
                patch('audio._synthesize_openai_speech', return_value=audio_file(self)):
            result = audio.synthesize_speech('测试。', replace(RuntimeSettings(), voice_backend='local'))
        self.assertEqual(result.backend, 'openai')
        self.assertLess(time.monotonic() - started, 4)

    def test_cancellation_reaps_blocked_process(self):
        self.delay = 5
        cancelled = threading.Event()
        token = operation_context.set(dict(cancelled=cancelled.is_set, reply_id='cancel-test'))
        timer = threading.Timer(.3, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TTSCancelled):
                run_backend('local', endpoint=self.endpoint, text='测试', voice='dataset_voice', speed=1.2)
        finally:
            operation_context.reset(token)
            timer.join()
        self.assertLess(time.monotonic() - started, 3)


class RecordingPlayer(QObject):
    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)
    playback_cancelled = Signal(str, str)
    def __init__(self):
        super().__init__()
        self.active = None
        self.played = []
        self.fail_first = False
    def set_volume(self, volume):
        pass
    def is_busy(self):
        return self.active is not None
    def play_file(self, path, reply_id):
        assert self.active is None, 'overlapping playback'
        validate_audio_file(path)
        self.active = reply_id
        if self.fail_first:
            self.fail_first = False
            self.active = None
            self.playback_failed.emit(reply_id, 'injected_decoder_failure')
            return
        self.playback_started.emit(reply_id)
        QTimer.singleShot(2, lambda: self.finish(reply_id))
    def finish(self, reply_id):
        if self.active != reply_id:
            return
        self.played.append(reply_id)
        self.active = None
        self.playback_finished.emit(reply_id)
    def stop(self, reason='explicit_stop'):
        reply_id, self.active = self.active, None
        if reply_id:
            self.playback_cancelled.emit(reply_id, reason)


class QueueAndPlaybackTests(unittest.TestCase):
    def test_default_output_prefers_matching_wasapi_device(self):
        devices = [dict(name='speaker', hostapi=0, max_output_channels=2),
                   dict(name='speaker', hostapi=1, max_output_channels=2)]
        with patch('speech_playback.sd.query_devices', return_value=devices), \
                patch('speech_playback.sd.default.device', [0, 0]), \
                patch('speech_playback.sd.query_hostapis', side_effect=lambda i: dict(name=['MME','WASAPI'][i])):
            self.assertEqual(default_output_device(), (1, devices[1]))

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def wait(self, predicate, seconds=6):
        until = time.monotonic() + seconds
        while time.monotonic() < until and not predicate():
            self.qt.processEvents()
            time.sleep(.002)
        self.assertTrue(predicate(), 'async operation did not settle')

    def test_twenty_fast_replies_are_not_replaced_and_all_fallback(self):
        player = RecordingPlayer()
        pipeline = SpeechPipeline(player)
        settings = replace(RuntimeSettings(), voice_backend='local')
        jobs = [SpeechJob(f'第{i}条。', settings) for i in range(20)]
        paths = [audio_file(self) for _ in jobs]
        failures = []
        pipeline.reply_failed.connect(lambda *args: failures.append(args))
        with patch('local_tts_client.synthesize_local', side_effect=ConnectionRefusedError), \
                patch('audio._synthesize_openai_speech', side_effect=paths) as old:
            for job in jobs:
                pipeline.submit(job)
            self.wait(lambda: not pipeline.is_busy())
            self.assertEqual(old.call_count, 20)
        self.assertEqual(player.played, [j.reply_id for j in jobs])
        self.assertFalse(failures)

    def test_playback_failure_retries_openai_once_with_same_id_and_text(self):
        player = RecordingPlayer(); player.fail_first = True
        pipeline = SpeechPipeline(player)
        job = SpeechJob('你好。', replace(RuntimeSettings(), voice_backend='local', speech_quirk_probability=1))
        with patch('local_tts_client.synthesize_local', return_value=audio_file(self)), \
                patch('audio._synthesize_openai_speech', return_value=audio_file(self)) as old:
            pipeline.submit(job)
            self.wait(lambda: not pipeline.is_busy())
            old.assert_called_once()
            self.assertEqual(old.call_args.args[0], '你好喵。')
        self.assertEqual(player.played, [job.reply_id])

    def test_explicit_cancel_keeps_thread_owned_until_it_exits(self):
        player = RecordingPlayer(); pipeline = SpeechPipeline(player)
        began, release = threading.Event(), threading.Event()
        job = SpeechJob('你好。', RuntimeSettings())
        def blocked(*args, **kwargs):
            began.set()
            release.wait(4)
            raise TTSCancelled('explicit_test_cancel')
        with patch('tts_pipeline.synthesize_speech', side_effect=blocked):
            pipeline.submit(job)
            self.wait(began.is_set)
            pipeline.cancel('voice_output_disabled')
            self.assertTrue(pipeline.has_workers())
            release.set()
            self.wait(lambda: not pipeline.has_workers())
        self.assertFalse(player.played)

    def test_duplicate_or_late_playback_event_cannot_finish_next_reply(self):
        player = RecordingPlayer(); pipeline = SpeechPipeline(player)
        first, second = SpeechJob('第一条。', RuntimeSettings()), SpeechJob('第二条。', RuntimeSettings())
        with patch('audio._synthesize_openai_speech', return_value=audio_file(self)):
            pipeline.submit(first)
            self.assertFalse(pipeline.submit(first))
            pipeline.submit(second)
            pipeline._finished('stale-id')
            self.wait(lambda: not pipeline.is_busy())
        self.assertEqual(player.played, [first.reply_id, second.reply_id])

    def test_never_started_and_decoder_errors_are_failures_not_finished(self):
        player = SpeechPlayer()
        failures, finished = [], []
        player.playback_failed.connect(lambda *args: failures.append(args))
        player.playback_finished.connect(finished.append)
        player._reply_id = 'no-start'; player._expecting_end = True
        player._on_timeout()
        self.assertEqual(failures[-1][1], 'playback_never_started')
        player._reply_id = 'decode'; player._expecting_end = True
        player._fail('pcm_decode_failure')
        self.assertIn('decode_failure', failures[-1][1])
        self.assertFalse(finished)

    def test_missing_file_fails_with_reply_id(self):
        player = SpeechPlayer(); failed = []
        player.playback_failed.connect(lambda *args: failed.append(args))
        player.play_file('missing.wav', 'missing-file-id')
        self.assertEqual(failed[0][0], 'missing-file-id')

    def test_pcm_finish_requires_all_frames_and_callback_returns_none(self):
        for partial in (False, True):
            player = SpeechPlayer(); done, failed = [], []
            player.playback_finished.connect(done.append)
            player.playback_failed.connect(lambda *args: failed.append(args))
            with patch('speech_playback.default_output_device', return_value=(0, dict(name='test', default_samplerate=24000, max_output_channels=1))), \
                    patch('speech_playback.sd.check_output_settings'), patch('speech_playback.sd.RawOutputStream') as stream:
                stream.return_value.time = 101
                player.play_file(audio_file(self), 'pcm-completion')
                callbacks = stream.call_args.kwargs
                frames = 100 if partial else 4800
                args = (bytearray(frames*4), frames, SimpleNamespace(outputBufferDacTime=100), False)
                if partial:
                    callbacks['callback'](*args)
                else:
                    with self.assertRaises(sd.CallbackStop):
                        callbacks['callback'](*args)
                self.assertIsNone(callbacks['finished_callback']())
                self.wait(lambda: not player.is_busy())
                stream.return_value.close.assert_called_once()
            self.assertEqual(bool(done), not partial)
            self.assertEqual(bool(failed), partial)

    def test_pcm_callback_exception_is_forwarded_and_stale_drain_ignored(self):
        player = SpeechPlayer(); failed = []
        player.playback_failed.connect(lambda *args: failed.append(args))
        with patch('speech_playback.default_output_device', return_value=(0, dict(name='test', default_samplerate=24000, max_output_channels=1))), \
                patch('speech_playback.sd.check_output_settings'), patch('speech_playback.sd.RawOutputStream') as stream:
            player.play_file(audio_file(self), 'callback-error')
            player._on_drained(player._generation - 1)
            self.assertTrue(player.is_busy())
            with self.assertRaises(sd.CallbackAbort):
                stream.call_args.kwargs['callback'](bytearray(3), 100, None, False)
            player._check_progress()
        self.assertEqual(failed, [('callback-error', 'audio_callback_ValueError')])

    def test_playback_handoff_exception_triggers_fallback(self):
        player = RecordingPlayer(); pipeline = SpeechPipeline(player)
        original = player.play_file
        attempts = []
        def broken_once(path, reply_id):
            attempts.append(reply_id)
            if len(attempts) == 1:
                raise OSError('injected_file_read_error')
            original(path, reply_id)
        player.play_file = broken_once
        job = SpeechJob('你好。', replace(RuntimeSettings(), voice_backend='local'))
        with patch('local_tts_client.synthesize_local', return_value=audio_file(self)), \
                patch('audio._synthesize_openai_speech', return_value=audio_file(self)) as fallback:
            pipeline.submit(job)
            self.wait(lambda: not pipeline.is_busy())
            fallback.assert_called_once()
        self.assertEqual(player.played, [job.reply_id])

    def test_both_playback_attempts_fail_explicitly_and_queue_continues(self):
        player = RecordingPlayer(); pipeline = SpeechPipeline(player)
        player.play_file = lambda path, reply_id: player.playback_failed.emit(reply_id, 'no_device')
        failed = []
        pipeline.reply_failed.connect(lambda *args: failed.append(args))
        job = SpeechJob('你好。', replace(RuntimeSettings(), voice_backend='local'))
        with patch('local_tts_client.synthesize_local', return_value=audio_file(self)), \
                patch('audio._synthesize_openai_speech', return_value=audio_file(self)) as fallback, \
                patch('tts_pipeline.event') as log:
            pipeline.submit(job)
            self.wait(lambda: not pipeline.is_busy())
            fallback.assert_called_once()
            self.assertIn('ERROR', [call.args[1] for call in log.call_args_list])
        self.assertEqual(failed, [(job.reply_id, 'no_device')])
        self.assertFalse(player.played)

    def test_pcm_device_conversion_preserves_duration_and_stall_is_failure(self):
        converted = device_pcm(wav_bytes()[44:], 24000, 1, 48000, 2)
        self.assertEqual(converted.shape, (9600, 2))
        self.assertAlmostEqual(len(converted) / 48000, validate_wav_bytes(wav_bytes()).duration, places=4)
        player = SpeechPlayer(); failed = []
        player.playback_failed.connect(lambda *args: failed.append(args))
        player._reply_id = 'stall'; player._expecting_end = True; player._started = True
        player._on_timeout()
        self.assertEqual(failed, [('stall', 'playback_stalled')])



if __name__ == '__main__':
    unittest.main()
