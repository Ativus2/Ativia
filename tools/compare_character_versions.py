"""Cross-version orchestration only. Imports the frozen evaluator unchanged."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import random
import statistics
import sys
from threading import Lock
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from openai import OpenAI
from tools.character_benchmark import evaluator as ev

LOCK = Lock()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def verify(old, new):
    before, after = read(old / "protocol.json"), read(new / "protocol.json")
    for key in ("benchmark_sha256", "weights", "judge_model", "generation_model", "trials", "aggregation", "penalty"):
        if before[key] != after[key]:
            raise ValueError(f"Frozen benchmark contract changed: {key}")
    for file in ("scenarios.json", "rubric.txt"):
        if (old / file).read_bytes() != (new / file).read_bytes():
            raise ValueError("Frozen scenarios/rubric changed")
    for file, digest in before["benchmark_sha256"].items():
        if sha256((ROOT / file).read_bytes()).hexdigest() != digest:
            raise ValueError("Evaluator source changed")
    return before


def prepare(old, new, out):
    protocol = verify(old, new)
    if (out / "private/plan.json").exists():
        saved = read(out / "private/plan.json")
        if saved["old"] != str(old.resolve()) or saved["new"] != str(new.resolve()):
            raise ValueError("Cannot change comparison sources")
        return saved
    rng = random.Random(20260909)
    pairs = []
    for seq in read(old / "scenarios.json"):
        for trial in range(3):
            sides = ["old", "new"]
            rng.shuffle(sides)
            pairs.append(dict(id=f"comparison-{len(pairs)+1:02}", sequence=seq["id"], trial=trial,
                              A=sides[0], B=sides[1], audit=False))
    for original in (pairs[0], pairs[6]):
        pairs.append(dict(**{k: v for k, v in original.items() if k not in {"id", "A", "B", "audit"}},
                          id=f"comparison-{len(pairs)+1:02}", A=original["B"], B=original["A"], audit=True,
                          original=original["id"]))
    plan = dict(old=str(old.resolve()), new=str(new.resolve()), pairs=pairs, judge=protocol["judge_model"],
                rubric_sha256=sha256(ev.RUBRIC.encode()).hexdigest(), max_calls=36,
                keep_gate="Full benchmark gain >=1, paired mean gain >=1, New wins > Old wins, initiative>=78/non_assistant>=85/restraint>=88, no guard dimension drop >5.")
    save(out / "private/plan.json", plan)
    save(out / "ledger.json", dict(calls=0, known_tokens=0, unknown_usage_calls=0))
    return plan


def judge_pair(pair, plan, out):
    target = out / "judgments" / (pair["id"] + ".json")
    if target.exists() and read(target).get("valid"):
        return
    sequence = next(s for s in read(Path(plan["old"]) / "scenarios.json") if s["id"] == pair["sequence"])
    rows = [read(Path(plan[pair[label]]) / "private/outputs" / f"{pair['sequence']}-{pair['trial']}.json") for label in ("A", "B")]
    if any(len(r) != len(sequence["steps"]) for r in rows):
        raise ValueError("Incomplete sequence")
    payload = ev.blind_payload(sequence, *rows)
    save(out / "blind-inputs" / (pair["id"] + ".json"), payload)
    with LOCK:
        ledger = read(out / "ledger.json")
        if ledger["calls"] >= plan["max_calls"] or ledger["known_tokens"] >= 150000:
            raise RuntimeError("Comparison budget exhausted")
        ledger["calls"] += 1
        attempt = ledger["calls"]
        save(out / "ledger.json", ledger)
    try:
        raw, usage, model = ev.judge(OpenAI(timeout=90, max_retries=0), plan["judge"], payload)
    except Exception as exc:
        with LOCK:
            ledger = read(out / "ledger.json")
            ledger["unknown_usage_calls"] += 1
            save(out / "ledger.json", ledger)
        save(out / "errors" / f"{pair['id']}-{attempt}.json", dict(error_type=type(exc).__name__, usage=None))
        raise
    record = dict(raw=raw, usage=usage, model=model, valid=False)
    save(out / "attempts" / f"{pair['id']}-{attempt}.json", record)
    with LOCK:
        ledger = read(out / "ledger.json")
        ledger["known_tokens"] += usage.get("total_tokens", 0)
        save(out / "ledger.json", ledger)
    judgment = ev.validate_judgment(json.loads(raw), {str(i+1) for i in range(len(sequence["steps"]))})
    record.update(valid=True, judgment=judgment)
    save(target, record)
    print(json.dumps(dict(pair=pair["id"], winner=judgment["winner"], audit=pair["audit"])), flush=True)


def aggregate(plan, out):
    values = {"old": [], "new": []}
    wins = dict(old=0, new=0, tie=0)
    results = []
    for pair in plan["pairs"]:
        j = read(out / "judgments" / (pair["id"] + ".json"))["judgment"]
        if pair["audit"]:
            continue
        winner = "tie" if j["winner"] == "tie" else pair[j["winner"]]
        wins[winner] += 1
        entry = dict(sequence=pair["sequence"], trial=pair["trial"], preference=winner)
        for label in ("A", "B"):
            side = pair[label]
            rows = read(Path(plan[side]) / "private/outputs" / f"{pair['sequence']}-{pair['trial']}.json")
            scores = ev.adjusted_scores(j[label]["scores"], rows)
            values[side].append(scores)
            entry[side] = ev.weighted(scores)
        entry["delta"] = entry["new"] - entry["old"]
        results.append(entry)
    means = {side: {k: statistics.mean(row[k] for row in values[side]) for k in ev.WEIGHTS} for side in values}
    before, after = [read(Path(plan[side]) / "summary.json") for side in ("old", "new")]
    full_gain = after["character_score"] - before["character_score"]
    paired_gain = ev.weighted(means["new"]) - ev.weighted(means["old"])
    floors = dict(initiative=78, non_assistant=85, restraint=88)
    regressions = [k for k, floor in floors.items() if after["components"][k] < floor
                   or after["components"][k] < before["components"][k] - 5]
    keep = full_gain >= 1 and paired_gain >= 1 and wins["new"] > wins["old"] and not regressions
    summary = dict(full_before=before["character_score"], full_after=after["character_score"],
                   full_gain=round(full_gain, 2), paired_scores={s:round(ev.weighted(means[s]),2) for s in means},
                   paired_gain=round(paired_gain, 2), preference=wins, regression=regressions,
                   decision="KEEP" if keep else "REVERT", pairs=results, costs=read(out / "ledger.json"),
                   note="Full scores use unchanged original benchmark. Paired scores rejudge both versions together; they do not replace the recorded baseline.")
    save(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    plan = prepare(args.old, args.new, args.out)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(judge_pair, pair, plan, args.out) for pair in plan["pairs"]]
        failures = []
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                failures.append(type(exc).__name__)
        if failures:
            raise RuntimeError(f"Unfinished comparisons: {failures}; resume without replacing successful reviews")
    aggregate(plan, args.out)


if __name__ == "__main__":
    main()
