"""Iteration 2 refinement; user weights and total measurable points stay fixed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "tools")]
import evaluate_character as original
from continuity_probes import ContinuityProbes

# Freeze BEFORE the production fix. Split existing acceptance weights instead
# of taking credit from unmeasured subjective personality / entertainment.
natural = original.RUBRIC["主动互动自然程度"][1]
natural[1] = (natural[1][0], natural[1][1], 2)
natural.append((ContinuityProbes, "memory_keeps_announced_fact_after_waiting", 2))
continuity = original.RUBRIC["上下文连续性"][1]
continuity[2] = (continuity[2][0], continuity[2][1], 1)
continuity.extend([
    (ContinuityProbes, "typed_question_can_recall_silent_observation", 2),
    (ContinuityProbes, "voice_question_can_recall_silent_observation", 2),
])

if __name__ == "__main__":
    original.main()
