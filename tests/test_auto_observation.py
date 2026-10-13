import threading
from PySide6.QtTest import QTest

import behavior_probes as probes
import vision


class AutoObservationTests(probes.AppCase):
    test_manual_same_screen = probes.BehaviorProbes.test_manual_same_screen
    test_rapid_switch_cooldown = probes.BehaviorProbes.test_rapid_switch_cooldown
    test_video_audio_same_screen = probes.BehaviorProbes.test_video_audio_same_screen
    test_learning_auto_same_screen = probes.BehaviorProbes.test_learning_auto_same_screen
    test_pause_cancels_pending_reply = probes.BehaviorProbes.test_pause_cancels_pending_reply
    test_idle_repeated_screen_no_call = probes.BehaviorProbes.test_idle_repeated_screen_no_call

    def test_failed_same_screen_retries_after_cooldown(self):
        self.ask.side_effect = [vision.ScreenshotAnalysisError("暂时失败"), "现在成功"]
        self.automatic(100)
        self.assertIsNone(self.app._last_hash)
        self.automatic(112)
        self.assertEqual(self.ask.call_count, 1)
        self.automatic(170)
        self.assertEqual(self.ask.call_count, 2)
        self.assertEqual(self.app._last_hash, 42)

    def test_manual_question_bypasses_auto_cooldown(self):
        self.automatic(100)
        self.clock.return_value = 112
        with __import__('unittest').mock.patch('voice_pipeline.respond_to_voice',return_value='解释内容') as reply:
            self.app._on_chat_submitted("请解释这个报错");self.wait_idle()
        self.assertEqual(self.ask.call_count,1);reply.assert_called_once()

    def test_pause_during_api_drops_reply_and_tts_even_if_resumed(self):
        release = threading.Event()
        entered = threading.Event()

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return "这条已被用户取消"

        self.ask.side_effect = delayed
        self.app.control._on_speak_clicked()
        self.app._auto = True
        self.app._start_auto_observe()
        try:
            import time
            deadline=time.monotonic()+2
            while not entered.is_set() and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.005)
            self.assertTrue(entered.is_set())
            self.app._on_auto_toggled(False)
            self.app._on_auto_toggled(True)
        finally:
            release.set()
        self.wait_idle()
        self.show_text.assert_not_called()
        self.tts.assert_not_called()

    def test_pausing_auto_does_not_cancel_manual(self):
        self.app._start_observe()
        self.app._on_auto_toggled(False)
        self.wait_idle()
        self.assertEqual(self.ask.call_count, 1)
        self.assertEqual(self.show_text.call_count, 1)
