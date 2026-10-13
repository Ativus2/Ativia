import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from evaluate_live_character import scene_contract
from character import Observation


class RecordedLiveCasesTests(unittest.TestCase):
    def test_recorded_baseline_exposes_the_two_missing_reactions(self):
        report = Path(__file__).with_name("fixtures") / "live-character-baseline.json"
        rows = json.loads(report.read_text(encoding="utf-8"))
        checks = [scene_contract(row["case"], Observation(**row["observation"]), row["delivered_reply"])
                  for row in rows]
        self.assertEqual(checks, [True, True, False, False])

    def test_invented_identity_does_not_get_credit_for_a_cat_keyword(self):
        observation = Observation("game", "surprise", "ending", "The detective was a cat", "high", False, "REACT", "原来凶手是猫。")
        for reply in ("原来凶手是猫。", "原来侦探和嫌犯，都是猫。", "猫才是终极嫌疑人。"):
            with self.subTest(reply=reply):
                self.assertFalse(scene_contract("twist", observation, reply))

    def test_focus_gate_cannot_be_ignored_to_get_reaction_credit(self):
        observation = Observation("coding", "error", "numpy", "numpy missing", "high", True, "REACT", "numpy 不在。")
        self.assertFalse(scene_contract("error", observation, observation.reply))
