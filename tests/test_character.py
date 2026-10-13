import json
import unittest
import threading
from PySide6.QtTest import QTest

import character_probes as probes
from character import CharacterMemory, Observation


class CharacterFlowTests(probes.CharacterProbes):
    """All fixed character probes also run in the normal regression suite."""

    def test_cancelled_observation_does_not_enter_memory(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(**kwargs):
            entered.set()
            release.wait(3)
            return self.model(**kwargs)
        self.create.side_effect = delayed
        self.app._auto = True
        self.app._start_auto_observe()
        try:
            import time
            deadline=time.monotonic()+2
            while not entered.is_set() and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.005)
            self.assertTrue(entered.is_set())
            self.app._on_auto_toggled(False)
        finally:
            release.set()
        self.wait_idle()
        self.assertFalse(self.app._character_memory.entries)
        self.show_text.assert_not_called()

    def test_voice_question_bypasses_scene_protocol(self):
        import numpy as np
        self.mic.snapshot.return_value = np.full(16000, 0.1, dtype=np.float32)
        self.mock("overlay.transcribe_speech", return_value="为什么会报错")
        self.observe()
        self.assertNotIn("场景观察协议", self.create.call_args.kwargs["messages"][0]["content"])
        self.show_text.assert_called_once()

    def test_protocol_error_does_not_leak_json_or_update_memory(self):
        self.value = probes.scene(reply="字" * 49)
        self.automatic(100)
        self.assertFalse(self.app._character_memory.entries)
        self.assertIsNone(self.app._last_hash)
        self.assertEqual(self.show_text.call_args.args[0], "这次观察格式不完整，稍后再试。")
        self.tts.assert_not_called()


class ObservationValidationTests(unittest.TestCase):
    def test_invalid_observations_are_rejected(self):
        values = [[], {}, probes.scene(focused="false"), probes.scene(event="invented"),
                  probes.scene(reply="字" * 49), probes.scene(subject="字" * 65),
                  probes.scene(confidence=True), dict(probes.scene(), injected="instruction")]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Observation.parse(json.dumps(value))

    def test_repeated_silent_frames_do_not_forget_announced_event(self):
        memory = CharacterMemory()
        observation = Observation.parse(json.dumps(probes.scene()))
        self.assertTrue(memory.accept(observation, 100))
        for at in range(110, 250, 10):
            self.assertFalse(memory.accept(observation, at))
        self.assertEqual(len(memory.entries), 1)
        self.assertTrue(memory.entries[0].announced)

    def test_uncertain_frame_does_not_prove_resolution(self):
        memory = CharacterMemory()
        error = Observation.parse(json.dumps(probes.scene()))
        memory.accept(error, 100)
        memory.accept(Observation.parse(json.dumps(probes.scene(event="resolved", confidence="low"))), 170)
        self.assertFalse(memory.accept(error, 240))

    def test_unsupported_resolution_does_not_reset_event(self):
        memory = CharacterMemory()
        error = Observation.parse(json.dumps(probes.scene()))
        memory.accept(error, 100)
        memory.accept(Observation.parse(json.dumps(probes.scene(event="resolved", evidence=""))), 170)
        self.assertFalse(memory.accept(error, 240))

    def test_context_budget_preserves_complete_json(self):
        memory = CharacterMemory()
        for index in range(6):
            value = probes.scene(subject=str(index) + '\\"' * 31, evidence='\\"' * 48)
            memory.accept(Observation.parse(json.dumps(value)), 100 + index)
        context = memory.context(110)
        self.assertLessEqual(len(context), 2400)
        self.assertIsInstance(json.loads(context), list)

    def test_quiet_focus_does_not_count_as_announced(self):
        memory = CharacterMemory()
        self.assertFalse(memory.accept(Observation.parse(json.dumps(probes.scene(focused=True))), 100))
        self.assertTrue(memory.accept(Observation.parse(json.dumps(probes.scene())), 170))

    def test_uncertain_same_event_does_not_erase_announced_flag(self):
        memory = CharacterMemory()
        error = Observation.parse(json.dumps(probes.scene()))
        memory.accept(error, 100)
        memory.accept(Observation.parse(json.dumps(probes.scene(confidence="low"))), 170)
        self.assertFalse(memory.accept(error, 240))

    def test_confirmed_resolution_allows_a_new_error_episode(self):
        memory = CharacterMemory()
        error = Observation.parse(json.dumps(probes.scene()))
        self.assertTrue(memory.accept(error, 100))
        memory.accept(Observation.parse(json.dumps(probes.scene(event="resolved"))), 170)
        self.assertTrue(memory.accept(error, 240))

    def test_other_events_do_not_prove_an_error_was_fixed(self):
        memory = CharacterMemory()
        error = Observation.parse(json.dumps(probes.scene()))
        memory.accept(error, 100)
        memory.accept(Observation.parse(json.dumps(probes.scene(event="surprise", reply="咦。"))), 170)
        self.assertFalse(memory.accept(error, 240))

    def test_observation_context_contains_no_candidate_reply(self):
        memory = CharacterMemory()
        memory.accept(Observation.parse(json.dumps(probes.scene(reply="不应该记住的候选台词", focused=True))), 100)
        self.assertNotIn("候选台词", memory.context(101))
