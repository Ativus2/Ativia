import numpy as np
import behavior_probes as probes


class VoicePriorityTests(probes.AppCase):
    def voice_question(self):
        self.mic.snapshot.return_value = np.full(16000, 0.1, dtype=np.float32)
        self.mock("overlay.transcribe_speech", return_value="刚才的答案能再说一遍吗")

    def test_new_voice_during_cooldown_is_processed(self):
        self.automatic(100)
        self.voice_question()
        self.automatic(112)
        self.assertEqual(self.ask.call_count, 2)
        self.assertIn("刚才的答案能再说一遍吗", self.ask.call_args.args[1])

    def test_voice_repeat_is_not_filtered(self):
        self.automatic(100)
        self.voice_question()
        self.automatic(170)
        self.assertEqual(self.ask.call_count, 2)
        self.assertEqual(self.show_text.call_count, 2)
