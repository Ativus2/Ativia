import io
import json
import os
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
import wave

import audio
import local_tts_client as client
from runtime_settings import RuntimeSettings
from tts_events import TTSFailure
from tts_fixtures import wav_bytes, audio_file
from gpu_policy import resource_settings

class LocalTTSContractTests(unittest.TestCase):
    def setUp(self):
        # This suite exercises an ephemeral HTTP fixture, not the real 9881 launcher.
        ready=patch('local_tts_service.ensure_service',return_value={'model_loaded':True})
        ready.start();self.addCleanup(ready.stop)
        self.content = wav_bytes(rate=32000)
        self.status = 200
        self.received = None
        case = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                case.received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(case.status)
                self.send_header("Content-Type", "audio/wav")
                self.end_headers(); self.wfile.write(case.content)
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = patch.object(client, "ENDPOINT", f"http://127.0.0.1:{self.server.server_port}")
        self.endpoint.start()
        self.settings = replace(RuntimeSettings(), voice_backend="local", voice_speed=1.2,
                                speech_quirk_enabled=False)

    def tearDown(self):
        self.endpoint.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join()

    def test_local_wav_and_request_preserve_speed_without_cloud_or_reference_upload(self):
        with patch("audio._client", side_effect=AssertionError("Cloud must not be called")):
            path = audio.synthesize_speech("你好，测试本地声音。", self.settings)
        try:
            self.assertTrue(path.endswith(".wav"))
            with wave.open(path) as w: self.assertEqual(w.getframerate(), 32000)
            self.assertEqual(self.received, dict(text="你好，测试本地声音。", voice="dataset_voice", speed=1.2,
                                               language="auto", resource_policy=json.loads(json.dumps(resource_settings(self.settings)))))
        finally: os.remove(path)

    def test_invalid_audio_is_rejected_instead_of_played(self):
        for content in (b"not a wav", self.content[:-12]):
            self.content = content
            with self.assertRaises(client.LocalTTSError): client.synthesize_local("test", self.settings)

    def test_http_failure_falls_back_only_when_enabled(self):
        self.status = 503
        fallback = audio_file(self)
        with patch("audio._synthesize_openai_speech", return_value=fallback) as old:
            self.assertEqual(audio.synthesize_speech("test", self.settings), fallback)
            old.assert_called_once()
        with patch("audio._synthesize_openai_speech") as old:
            with self.assertRaises(TTSFailure):
                audio.synthesize_speech("test", replace(self.settings, tts_fallback=False))
            old.assert_not_called()

    def test_new_settings_round_trip_and_invalid_values(self):
        from dataclasses import asdict
        self.assertEqual(RuntimeSettings.parse(asdict(self.settings)), self.settings)
        bad = RuntimeSettings.parse(dict(voice_backend="remote", local_voice="../escape", tts_fallback="false"))
        self.assertEqual(bad.voice_backend, "openai")
        self.assertEqual(bad.local_voice, "dataset_voice")
        self.assertIs(bad.tts_fallback, True)
