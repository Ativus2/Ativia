"""Fixed evidence-transition cases; all inputs are scripted observations."""
from dataclasses import replace
import json
import unittest

from character import CharacterMemory, Observation
import continuity_probes as probes


ERROR = Observation("coding", "error", "terminal:numpy", "ModuleNotFoundError: numpy",
                    "high", False, "REACT", "哦，炸了。")
RESOLVED = replace(ERROR, event="resolved", evidence="同一任务测试通过", reply="这回过了。")


class EvidenceMemoryTests(unittest.TestCase):
    def test_confirmed_resolution_survives_low_confidence_frame(self):
        memory = CharacterMemory()
        for item, at in ((ERROR, 100), (RESOLVED, 170), (replace(RESOLVED, confidence="low"), 240)):
            memory.accept(item, at)
        self.assertEqual(memory.accept(replace(ERROR, reply="怎么又是它。"), 310), "怎么又是它。")

    def test_confirmed_resolution_survives_missing_evidence(self):
        memory = CharacterMemory()
        for item, at in ((ERROR, 100), (RESOLVED, 170), (replace(RESOLVED, evidence=""), 240)):
            memory.accept(item, at)
        self.assertTrue(memory.accept(ERROR, 310))

    def test_context_retains_confirmed_and_uncertain_observation_ages(self):
        memory = CharacterMemory()
        memory.accept(RESOLVED, 100)
        memory.accept(replace(RESOLVED, confidence="low", evidence="当前画面模糊"), 170)
        entries = json.loads(memory.context(240))
        self.assertEqual([(item["confidence"], item["age_s"]) for item in entries],
                         [("high", 140), ("low", 70)])
        self.assertEqual(entries[0]["evidence"], "同一任务测试通过")
        self.assertEqual(entries[1]["action"], "OBSERVE")

    def test_weak_frames_do_not_extend_confirmed_evidence_lifetime(self):
        memory = CharacterMemory()
        memory.accept(RESOLVED, 100)
        for at in (170, 240, 310, 380, 450, 520, 590, 660):
            memory.accept(replace(RESOLVED, confidence="low"), at)
        self.assertEqual([row["confidence"] for row in json.loads(memory.context(699))], ["high", "low"])
        self.assertEqual([row["confidence"] for row in json.loads(memory.context(701))], ["low"])
        self.assertEqual(len(memory.entries), 2)

    def test_uncertain_resolution_still_cannot_reset_a_known_error(self):
        memory = CharacterMemory()
        memory.accept(ERROR, 100)
        memory.accept(replace(RESOLVED, confidence="low"), 170)
        memory.accept(replace(RESOLVED, evidence=""), 240)
        self.assertFalse(memory.accept(ERROR, 310))

    def test_repeated_credible_event_stays_coalesced_and_quiet(self):
        memory = CharacterMemory()
        self.assertTrue(memory.accept(ERROR, 100))
        for at in range(170, 700, 70):
            self.assertFalse(memory.accept(replace(ERROR, evidence="numpy 依然未找到"), at))
        self.assertEqual(len(memory.entries), 1)

    def test_alternating_confidence_stays_bounded_without_repeated_speech(self):
        memory = CharacterMemory()
        outputs = [memory.accept(ERROR, 100)]
        for index in range(1, 30):
            outputs.append(memory.accept(replace(ERROR, confidence="low" if index % 2 else "high"), 100 + index * 10))
        self.assertEqual(sum(bool(text) for text in outputs), 1)
        self.assertLessEqual(len(memory.entries), 6)
        self.assertLessEqual(len(memory.context(400)), 2400)


class EvidenceFlowTests(probes.ObservationCase):
    def test_new_error_after_blur_can_reach_subtitle_and_tts(self):
        self.app.control._on_speak_clicked()
        self.observe()
        self.observe(event="resolved", evidence="同一任务测试通过", reply="这回过了。")
        self.observe(event="resolved", confidence="low", evidence="当前画面模糊", reply="好像过了？")
        self.observe(reply="怎么又是它。")
        self.assertEqual(self.show_text.call_count, 3)
        self.assertEqual(self.show_text.call_args.args[0], "怎么又是它。")
        self.assertEqual(self.tts.call_count, 3)
        self.assertEqual(self.create.call_count, 4)
