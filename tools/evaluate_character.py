"""Fixed character-direction proxy rubric; unmeasured points are never redistributed."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from character_probes import CharacterProbes
from behavior_probes import BehaviorProbes
from test_learning import LearningFlowTests

RUBRIC = {
    "角色生命感": (25, [(CharacterProbes, "silent_observation_is_remembered", 5), (CharacterProbes, "silence_can_track_a_transition", 5)]),
    "人格一致性": (20, []),
    "主动互动自然程度": (15, [(CharacterProbes, "focused_scenes_do_not_force_a_comment", 4), (CharacterProbes, "same_event_different_words_stays_quiet", 4), (BehaviorProbes, "rapid_switch_cooldown", 4)]),
    "上下文连续性": (15, [(CharacterProbes, "error_then_resolution_can_react", 5), (CharacterProbes, "memory_is_bounded_expires_and_survives_mode_switch", 5), (BehaviorProbes, "recent_context_in_request", 5)]),
    "娱乐性/节目效果": (10, [(CharacterProbes, "grounded_non_helpful_reaction_is_allowed", 2)]),
    "屏幕理解": (10, [(CharacterProbes, "uncertain_and_empty_events_are_not_announced", 3), (BehaviorProbes, "frozen_error_region", 2)]),
    "工具实用性": (5, [(CharacterProbes, "manual_help_uses_original_channel", 2), (LearningFlowTests, "region_summary_and_explanation", 3)]),
}


def main():
    report = {"kind": "scripted behavior proxy, not subjective life/personality or visual accuracy",
              "unmeasured_points": 51, "real_api_calls": 0, "categories": {}, "checks": {}}
    for category, (weight, entries) in RUBRIC.items():
        score = 0
        for cls, name, points in entries:
            cls.setUpClass()
            result = unittest.TestResult()
            cls("test_" + name).run(result)
            passed = result.wasSuccessful()
            check = {"passed": passed, "points": points}
            if not passed:
                check["failure"] = (result.errors + result.failures)[0][1].strip().splitlines()[-1]
            report["checks"][name] = check
            score += points if passed else 0
        report["categories"][category] = {"weight": weight, "verified": score,
                                           "unmeasured": weight - sum(entry[2] for entry in entries)}
    report["score_out_of_100"] = sum(c["verified"] for c in report["categories"].values())
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
