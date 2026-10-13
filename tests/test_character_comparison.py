"""The new cross-version transport must preserve the frozen judge contract."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from tools import compare_character_versions as compare

BASELINE = Path(__file__).resolve().parents[1] / "docs/character-benchmark/baseline-2026-09-08"


class ComparisonContracts(unittest.TestCase):
    def fixture(self, destination):
        destination.mkdir(parents=True)
        from tools.character_benchmark import evaluator as ev
        import hashlib
        root=Path(__file__).resolve().parents[1]
        source="tools/character_benchmark/evaluator.py"
        protocol=dict(benchmark_sha256={source:hashlib.sha256((root/source).read_bytes()).hexdigest()},
            weights=dict(ev.WEIGHTS),judge_model="unit-fixture",generation_model="unit-fixture",trials=3,
            aggregation="unit",penalty="unit")
        compare.save(destination/"protocol.json",protocol)
        compare.save(destination/"scenarios.json",[dict(id="unit-"+str(i),steps=[]) for i in range(10)])
        (destination/"rubric.txt").write_text(ev.RUBRIC,encoding="utf-8")

    def test_all_trials_are_paired_and_identity_is_only_in_private_map(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root / "old")
            self.fixture(root / "new")
            plan = compare.prepare(root / "old", root / "new", root / "out")
            formal = [p for p in plan["pairs"] if not p["audit"]]
            self.assertEqual(len(formal), 30)
            self.assertEqual(len({(p["sequence"], p["trial"]) for p in formal}), 30)
            self.assertTrue(all({p["A"], p["B"]} == {"old", "new"} for p in formal))
            self.assertEqual(compare.prepare(root / "old", root / "new", root / "out"), plan)

    def test_changed_scoring_rules_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root / "old")
            self.fixture(root / "new")
            file = root / "new/protocol.json"
            value = compare.read(file)
            value["weights"]["personality"] += 1
            compare.save(file, value)
            with self.assertRaises(ValueError):
                compare.verify(root / "old", root / "new")

    def test_changed_scenario_text_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root / "old")
            self.fixture(root / "new")
            (root / "new/scenarios.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                compare.verify(root / "old", root / "new")
