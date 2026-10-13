"""Run the frozen offline proxy rubric. No external API or real screen capture."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from behavior_probes import BehaviorProbes

RUBRIC = {
    "屏幕理解": [("static_region", 4), ("frozen_error_region", 6)],
    "主动交互时机": [("manual_same_screen", 5), ("rapid_switch_cooldown", 5), ("video_audio_same_screen", 5), ("learning_auto_same_screen", 5)],
    "自然度与人格": [("focus_can_stay_silent", 5), ("ignored_duplicate_not_repeated", 5)],
    "上下文连续性": [("explanation_keeps_original", 5), ("recent_context_in_request", 5), ("context_bounds", 5)],
    "打扰程度": [("pause_cancels_pending_reply", 5), ("busy_blocks_duplicate", 5)],
    "性能与API成本": [("idle_repeated_screen_no_call", 2.5), ("silent_no_tts", 2.5)],
    "稳定性": [("snip_start_failure_recovers", 2.5), ("capture_failure_recovers", 2.5)],
}


def main():
    report = {"kind": "offline verified lower bound; NOT model accuracy", "unmeasured_points": 25, "categories": {}, "checks": {}}
    BehaviorProbes.setUpClass()
    for category, entries in RUBRIC.items():
        points = 0
        for name, weight in entries:
            case = BehaviorProbes("test_" + name)
            result = unittest.TestResult()
            case.run(result)
            passed = result.wasSuccessful()
            report["checks"][name] = {"passed": passed, "weight": weight}
            if not passed:
                details = result.failures + result.errors
                report["checks"][name]["failure"] = details[0][1].strip().splitlines()[-1] if details else "unknown"
            points += weight if passed else 0
        report["categories"][category] = points
    report["score_out_of_100"] = sum(report["categories"].values())
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
