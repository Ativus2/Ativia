"""Offline bounds and local decision timings; not API billing or model latency."""
import ast
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from character import CharacterMemory, Observation, MEMORY_LIMIT, MEMORY_SECONDS, CONTEXT_LIMIT
import vision


def main():
    before = ast.parse((ROOT / "docs/character-iterations/before-1/vision.py").read_text(encoding="utf-8"))
    prompts = {node.targets[0].id: ast.literal_eval(node.value) for node in before.body
               if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and isinstance(node.value.value, str)}
    value = dict(scene="coding", event="none", subject="editor:parser", evidence="正在编辑解析器",
                 confidence="high", focused=True, intent="WAIT", reply="")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    memory = CharacterMemory()
    started = perf_counter()
    for index in range(2000):
        memory.accept(Observation.parse(encoded), float(index))
        memory.context(float(index))
    elapsed = (perf_counter() - started) * 1000 / 2000
    report = {
        "real_api_calls": 0, "actual_tokens_and_billing_measured": False,
        "automatic_entertainment_system_chars_before": len(prompts["ENTERTAINMENT_PROMPT"] + "\n" + prompts["AUTO_OBSERVE_PROMPT"]),
        "automatic_entertainment_system_chars_after": len(vision.ENTERTAINMENT_PROMPT + "\n" + vision.SCENE_OBSERVATION_PROMPT),
        "automatic_output_token_cap_before": 220, "automatic_output_token_cap_after": 480,
        "manual_and_learning_budgets_changed": False,
        "screen_model_calls_per_analyzed_frame_before": 1, "screen_model_calls_per_analyzed_frame_after": 1,
        "automatic_cooldown_seconds_unchanged": 60,
        "old_silent_response_chars": len(vision.SILENT_RESPONSE), "sample_silent_observation_chars": len(encoded),
        "additional_observation_context_max_chars": CONTEXT_LIMIT,
        "memory_limit": MEMORY_LIMIT, "retrieval_ttl_seconds": MEMORY_SECONDS,
        "mean_parse_decide_context_ms_2000_runs": round(elapsed, 4),
    }
    output = json.dumps(report, ensure_ascii=False, indent=2)
    (ROOT / "docs/character-iterations/cost-observations.json").write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
