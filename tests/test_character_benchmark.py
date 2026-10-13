"""Offline contracts for benchmark integrity, not a character quality score."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.character_benchmark import evaluator as ev
from tools.character_benchmark import scenarios


class BenchmarkContracts(unittest.TestCase):
    def test_coverage_and_sequence_lengths(self):
        scenarios.validate()
        self.assertEqual(len(scenarios.SEQUENCES), 10)
        ids = {s["id"] for seq in scenarios.SEQUENCES for s in seq["steps"]}
        self.assertGreaterEqual(len(ids), 30)
        self.assertTrue({"death_3", "boss_win", "error_returns", "study_help", "reject_speech", "screen_empty"} <= ids)

    def test_weighted_score_exactly_user_weights(self):
        self.assertEqual(sum(ev.WEIGHTS.values()), 100)
        self.assertEqual(ev.weighted(dict.fromkeys(ev.WEIGHTS, 5)), 100)
        self.assertEqual(ev.weighted(dict.fromkeys(ev.WEIGHTS, 0)), 0)

    def test_blinding_allowlist_excludes_provenance_and_internal_prompt(self):
        rows = [dict(response="嗯。", version="new-secret", model="generator-secret", prompt="prompt-secret",
                     internal_state={"mood": "state-secret"}) for _ in range(4)]
        data = ev.blind_payload(scenarios.SEQUENCES[0], rows, rows)
        text = json.dumps(data)
        for secret in ("new-secret", "generator-secret", "prompt-secret", "state-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(data["candidates"]["A"][0]), {"turn", "response", "action"})

    def test_all_six_assistant_patterns(self):
        for text in ("看起来你正在写代码。", "我注意到你报错了。", "如果你愿意，我可以讲解。",
                     "你可以尝试重新启动。", "有什么需要帮助的吗？", "这可能是因为依赖缺失。"):
            self.assertTrue(ev.text_flags(text)["patterns"], text)

    def test_requested_help_is_exempt_from_surface_penalties(self):
        row = dict(response="你可以尝试" + "步骤" * 60, context=dict(command="请详细解释"))
        scores = dict.fromkeys(ev.WEIGHTS, 4)
        self.assertEqual(ev.adjusted_scores(scores, [row]), scores)
        self.assertEqual(ev.text_flags(row["response"], False)["patterns"], [])

    def test_no_speech_does_not_prove_presence(self):
        result = ev.adjusted_scores(dict.fromkeys(ev.WEIGHTS, 5), [dict(response="", context=dict(command=None))])
        self.assertEqual(result["presence"], 2)
        self.assertEqual(result["continuity"], 2)
        self.assertEqual(result["restraint"], 5)

    def test_surface_penalty_is_fixed_and_bounded(self):
        row = dict(response="如果你愿意，我可以帮你。", context=dict(command=None))
        scores = dict.fromkeys(ev.WEIGHTS, 1)
        self.assertEqual(ev.adjusted_scores(scores, [row])["non_assistant"], 0)
        self.assertEqual(scores["non_assistant"], 1)

    def test_question_is_flag_not_automatic_bad_joke(self):
        self.assertEqual(ev.text_flags("又是你？"), dict(patterns=[], long=False, question=True))

    def test_judge_schema_rejects_missing_evidence_and_invalid_scores(self):
        candidate = dict(scores=dict.fromkeys(ev.WEIGHTS, 3), reasons=dict.fromkeys(ev.WEIGHTS, "turn 1: 嗯"),
                         flags=[], evidence_limited=False)
        good = dict(A=copy.deepcopy(candidate), B=copy.deepcopy(candidate), winner="tie", comparison="基本一致")
        ev.validate_judgment(good, {"1"})
        bad = copy.deepcopy(good)
        bad["A"]["scores"]["presence"] = True
        with self.assertRaises(ValueError):
            ev.validate_judgment(bad, {"1"})
        bad = copy.deepcopy(good)
        bad["A"]["reasons"]["presence"] = ""
        with self.assertRaises(ValueError):
            ev.validate_judgment(bad, {"1"})
        bad = copy.deepcopy(good)
        bad["A"]["flags"] = [dict(turn="8", kind="invented", quote="不存在")]
        with self.assertRaises(ValueError):
            ev.validate_judgment(bad, {"1"})

    def test_evaluator_does_not_import_generator(self):
        source = Path(ev.__file__).read_text(encoding="utf-8")
        for module in ("vision", "overlay", "persona", "character"):
            self.assertNotIn(f"import {module}", source)

    def test_real_worker_delivery_and_resume_without_recalling_api(self):
        from tools.character_benchmark import run
        fixture = copy.deepcopy(scenarios.SEQUENCES[0])
        fixture["steps"] = fixture["steps"][:1]
        obs = dict(scene="game", event="error", subject="Ash Knight", evidence="YOU DIED",
                   confidence="high", focused=False, intent="REACT", reply="差一点。")
        result = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(obs)))],
                                 model="offline-fixture", usage=None)
        with tempfile.TemporaryDirectory() as directory, patch.object(run, "SEQUENCES", [fixture]), \
             patch.object(run, "OpenAI") as client, patch.dict("os.environ", {"OPENAI_API_KEY": "test-placeholder"}):
            out = Path(directory)
            run.prepare(out, "independent-judge")
            client.return_value.chat.completions.create.return_value = result
            run.generate(out, max_sequences=1)
            rows = run.read(out / "private/outputs/game_deaths-0.json")
            self.assertEqual(rows[0]["response"], "差一点。")
            self.assertEqual(rows[0]["decision"], "REACT")
            self.assertEqual(rows[0]["event"], obs)
            self.assertTrue(rows[0]["state_after"]["memory"][0]["announced"])
            self.assertEqual(client.return_value.chat.completions.create.call_count, 1)
            # Complete three trials; a subsequent resume must not regenerate anything.
            run.generate(out)
            run.generate(out)
            self.assertEqual(client.return_value.chat.completions.create.call_count, 3)


if __name__ == "__main__":
    unittest.main()
