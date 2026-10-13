"""Explicit live benchmark; never imported by ordinary test discovery."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtTest import QTest
import behavior_probes
from character import CharacterMemory, Impression, Observation
import overlay
import vision
from tools.character_benchmark.scenarios import SEQUENCES
from tools.character_benchmark import evaluator

REAL_HASH = overlay.screenshot_hash
PRODUCTION = sorted([*ROOT.glob("*.py"), ROOT / "requirements.txt", ROOT / "README.md", ROOT / ".env.example"])
MAX_GENERATION_CALLS = 126
MAX_JUDGE_CALLS = 24
MAX_TOKENS = 450000


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fingerprints(paths):
    return {str(p.resolve().relative_to(ROOT)).replace("\\", "/"): sha256(p.read_bytes()).hexdigest() for p in paths}


def prepare(out, judge_model):
    target = out / "protocol.json"
    if target.exists():
        protocol = read(target)
        if protocol["production_sha256"] != fingerprints(PRODUCTION):
            raise RuntimeError("Production changed: baseline cannot mix versions")
        if protocol["benchmark_sha256"] != fingerprints(sorted(Path(__file__).parent.glob("*.py"))):
            raise RuntimeError("Benchmark changed: use a new run directory")
        if protocol["judge_model"] != judge_model:
            raise RuntimeError("Judge model changed")
        return protocol
    protocol = dict(version="character-benchmark-1.0", created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    generation_model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"), judge_model=judge_model,
                    trials=3, scenarios=sum(len(s["steps"]) for s in SEQUENCES), sequences=len(SEQUENCES),
                    weights=evaluator.WEIGHTS, engineering_score=49, production_sha256=fingerprints(PRODUCTION),
                    benchmark_sha256=fingerprints(sorted(Path(__file__).parent.glob("*.py"))),
                    max_generation_calls=MAX_GENERATION_CALLS, max_judge_calls=MAX_JUDGE_CALLS,
                    max_total_tokens=MAX_TOKENS, api_timeout_seconds=60, sdk_retries=0,
                    aggregation="Mean reviews per trial, then equal trial means, then equal sequence means; 0..5 scaled to 100.",
                    penalty="Proactive spoken turns only: non_assistant minus 2*phrase-hit fraction; restraint minus over-48-char fraction; floor 0. Silence evidence caps: presence/personality/continuity/entertainment <=2.",
                    scope="Synthetic screenshots, real vision -> ObserveWorker -> memory/rhythm -> OverlayApp delivery. Observe path injects obtained screenshot after capture gate; poll path also runs real cooldown/hash. No real capture, microphone, TTS or Live2D evaluation.",
                    blinding="Only public persona, scene facts, chronological visible speech/silence enter separate judge. Current-version trials compared A/B; these are NOT old/new versions. Two swapped-pair audits excluded from score.")
    save(target, protocol)
    save(out / "scenarios.json", SEQUENCES)
    (out / "rubric.txt").write_text(evaluator.RUBRIC, encoding="utf-8")
    plan = []
    rng = random.Random(20260908)
    for sequence in SEQUENCES:
        for first, second in ((0, 1), (1, 2)):
            pair = [first, second]
            rng.shuffle(pair)
            plan.append(dict(id=f"pair-{len(plan) + 1:02}", sequence=sequence["id"], A=pair[0], B=pair[1], audit=False))
    for original in (plan[0], plan[min(4, len(plan) - 1)]):
        plan.append(dict(id=f"pair-{len(plan) + 1:02}", sequence=original["sequence"],
                         A=original["B"], B=original["A"], audit=True, original=original["id"]))
    save(out / "private" / "blind-map.json", plan)
    save(out / "ledger.json", dict(generation_calls=0, judge_calls=0, total_tokens=0))
    return protocol


def budget(out, kind, usage=None):
    ledger = read(out / "ledger.json")
    if usage is not None:
        ledger["total_tokens"] += usage.get("total_tokens", 0)
    else:
        limit = MAX_GENERATION_CALLS if kind == "generation" else MAX_JUDGE_CALLS
        if ledger[kind + "_calls"] >= limit or ledger["total_tokens"] >= MAX_TOKENS:
            raise RuntimeError("Pre-registered API budget exhausted")
        ledger[kind + "_calls"] += 1
    save(out / "ledger.json", ledger)


def render(item, out):
    picture = Image.new("RGB", (1100, 720), "#19232d")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 23)
    draw.rectangle((0, 0, 1100, 65), fill="#314457")
    draw.text((26, 20), item["title"], font=font, fill="#ffffff")
    for index, line in enumerate(item["lines"]):
        draw.text((32, 100 + index * 48), line, font=font, fill="#e3eaf0")
    path = out / "screens" / (item["id"] + ".jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(path, format="JPEG", quality=85)
    return path.read_bytes()


def state(app):
    return dict(memory=[asdict(entry) for entry in app._character_memory.entries],
                reactions=list(app._conversation._reactions), recent_turns=list(app._recent_turns),
                next_auto_at=app._next_auto_at, last_hash=app._last_hash,
                unsupported_states=["mood", "relationship_closeness", "running_jokes", "long_term_memory"])


def restore(app, value):
    app._character_memory = CharacterMemory(Impression(observation=Observation(**entry["observation"]),
                                                      **{k: v for k, v in entry.items() if k != "observation"})
                                           for entry in value["memory"])
    app._conversation._reactions.extend(value["reactions"])
    app._recent_turns.extend(tuple(turn) for turn in value["recent_turns"])
    app._next_auto_at = value["next_auto_at"]
    app._last_hash = value["last_hash"]


class Harness(behavior_probes.AppCase):
    """Hardware fixtures only; production worker, decisions and UI result path run unchanged."""
    def wait_idle(self):
        deadline = time.monotonic() + 90
        while self.app._busy() and time.monotonic() < deadline:
            self.qt.processEvents()
            # QTest.qWait can monopolize the Python GIL during real HTTP work.
            # Yield to the production QThread as the normal Qt event loop does.
            time.sleep(0.01)
        if self.app._busy():
            self.app._worker.requestInterruption()
            self.app._worker.wait(70000)
            raise RuntimeError("Worker deadline exceeded")
        QTest.qWait(20)


def generate(out, max_sequences=None):
    Harness.setUpClass()
    finished = 0
    for sequence in SEQUENCES:
        for trial in range(3):
            path = out / "private" / "outputs" / f"{sequence['id']}-{trial}.json"
            rows = read(path) if path.exists() else []
            if len(rows) == len(sequence["steps"]):
                continue
            h = Harness()
            h.setUp()
            h.app._timer.stop()
            h.app._auto = True
            h.image_hash.side_effect = REAL_HASH
            if rows:
                restore(h.app, rows[-1]["state_after"])
            try:
                for item in sequence["steps"][len(rows):]:
                    h.clock.return_value = item["at"]
                    if item["mode"] != h.app.control.current_mode():
                        h.app.control._on_mode_clicked()
                    image = render(item, out)
                    if item["same_as"]:
                        image = (out / "screens" / (item["same_as"] + ".jpg")).read_bytes()
                        (out / "screens" / (item["id"] + ".jpg")).write_bytes(image)
                    h.capture.return_value = image
                    h.show_text.reset_mock()
                    h.ask.reset_mock()
                    row = dict(scenario=item["id"], trial=trial, context=dict(command=item["command"],
                               mode=item["mode"], source="synthetic screenshot", path=item["path"],
                               at=item["at"], screen_sha256=sha256(image).hexdigest()),
                               expected_event=item["label"], state_before=state(h.app), requests=[], response="")
                    def factory(**kwargs):
                        kwargs.update(timeout=60.0, max_retries=0)
                        client = OpenAI(**kwargs)
                        def create(**request):
                            budget(out, "generation")
                            started = time.monotonic()
                            safe = json.loads(json.dumps(request))
                            for message in safe["messages"]:
                                if isinstance(message["content"], list):
                                    for content in message["content"]:
                                        if content["type"] == "image_url":
                                            content["image_url"]["url"] = f"screens/{item['id']}.jpg"
                            record = dict(request=safe)
                            row["requests"].append(record)
                            try:
                                result = client.chat.completions.create(**request)
                            except Exception as exc:
                                record["transport_error"] = type(exc).__name__
                                record["status_code"] = getattr(exc, "status_code", None)
                                raise
                            record.update(raw=result.choices[0].message.content, model=result.model,
                                          usage=result.usage.model_dump() if result.usage else {},
                                          seconds=round(time.monotonic() - started, 3))
                            budget(out, "generation", record["usage"])
                            return result
                        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                    def actual(*args, **kwargs):
                        row["actual_context"] = kwargs
                        try:
                            result = vision.comment_on_screenshot(*args, **kwargs)
                            row["event"] = asdict(result) if isinstance(result, Observation) else None
                            return result
                        except Exception as exc:
                            row["generation_error"] = type(exc).__name__
                            raise
                    h.ask.side_effect = actual
                    started = time.monotonic()
                    with patch.object(vision, "OpenAI", side_effect=factory):
                        if item["command"]:
                            h.app._on_chat_submitted(item["command"])
                        elif item["path"] == "poll":
                            h.app._start_auto_observe()
                        else:
                            # Controlled screenshot has already arrived; exercise actual post-capture path.
                            h.app._next_auto_at = item["at"] + overlay.AUTO_ANALYSIS_COOLDOWN_SECONDS
                            worker = overlay.ObserveWorker(image, None, None, 16000, False,
                                                           item["mode"], False, automatic=True,
                                                           image_hash=REAL_HASH(image))
                            h.app._launch_worker(worker)
                        try:
                            h.wait_idle()
                        except Exception:
                            save(out / "private" / "errors" / f"{item['id']}-{trial}.json", row)
                            raise
                    row["seconds"] = round(time.monotonic() - started, 3)
                    row["state_after"] = state(h.app)
                    if h.show_text.call_args:
                        row["response"] = h.show_text.call_args.args[0]
                    if item["mode"] == "learning":
                        row["response"] = h.app.control.learning_text.toPlainText()
                    memory = row["state_after"]["memory"]
                    if row.get("generation_error"):
                        row["decision"] = "ERROR"
                    elif row["response"]:
                        row["decision"] = "HELP" if item["command"] else (memory[-1]["action"] if memory else "SPEAK")
                    elif not h.ask.call_count:
                        row["decision"] = "SKIP_COOLDOWN" if item["at"] < row["state_before"]["next_auto_at"] else "SKIP_UNCHANGED"
                    else:
                        row["decision"] = memory[-1]["action"] if memory else "WAIT"
                    row["surface_flags"] = evaluator.text_flags(row["response"], not item["command"])
                    if any(r.get("transport_error") for r in row["requests"]):
                        save(out / "private" / "errors" / f"{item['id']}-{trial}.json", row)
                        raise RuntimeError("API transport failure; checkpoint retained, no score fabricated")
                    rows.append(row)
                    save(path, rows)
                    print(json.dumps(dict(sequence=sequence["id"], trial=trial + 1, scenario=item["id"],
                                          decision=row["decision"], response=row["response"]), ensure_ascii=False), flush=True)
            finally:
                h.tearDown()
                h.doCleanups()
            finished += 1
            if max_sequences and finished >= max_sequences:
                return


def evaluate(out, model, max_pairs=None):
    client = OpenAI(timeout=90.0, max_retries=0)
    count = 0
    for pair in read(out / "private" / "blind-map.json"):
        target = out / "judgments" / (pair["id"] + ".json")
        if target.exists() and read(target).get("valid"):
            continue
        sequence = next(s for s in SEQUENCES if s["id"] == pair["sequence"])
        candidate_rows = [read(out / "private" / "outputs" / f"{sequence['id']}-{pair[label]}.json") for label in ("A", "B")]
        if any(len(rows) != len(sequence["steps"]) for rows in candidate_rows):
            raise RuntimeError("Incomplete generation: no partial sequence judging")
        payload = evaluator.blind_payload(sequence, *candidate_rows)
        save(out / "blind-inputs" / (pair["id"] + ".json"), payload)
        budget(out, "judge")
        raw, usage, served_model = evaluator.judge(client, model, payload)
        budget(out, "judge", usage)
        record = dict(raw=raw, usage=usage, model=served_model, valid=False)
        save(target, record)
        value = evaluator.validate_judgment(json.loads(raw), {str(i + 1) for i in range(len(sequence["steps"]))})
        record.update(valid=True, judgment=value)
        save(target, record)
        print(json.dumps(dict(judged=pair["id"], winner=value["winner"], audit=pair["audit"])), flush=True)
        count += 1
        if max_pairs and count >= max_pairs:
            return


def report(out):
    protocol = read(out / "protocol.json")
    if protocol["production_sha256"] != fingerprints(PRODUCTION):
        raise RuntimeError("Production changed")
    plans = read(out / "private" / "blind-map.json")
    reviews = {}
    judgments = {}
    for pair in plans:
        artifact = read(out / "judgments" / (pair["id"] + ".json"))
        if not artifact["valid"]:
            raise RuntimeError("Invalid evaluation: baseline incomplete")
        judgments[pair["id"]] = artifact["judgment"]
        if pair["audit"]:
            continue
        for label in ("A", "B"):
            key = (pair["sequence"], pair[label])
            rows = read(out / "private" / "outputs" / f"{key[0]}-{key[1]}.json")
            scores = evaluator.adjusted_scores(artifact["judgment"][label]["scores"], rows)
            reviews.setdefault(key, []).append(scores)
    sequences, trials, all_rows = [], [], []
    for sequence in SEQUENCES:
        trial_scores = []
        for trial in range(3):
            rows = read(out / "private" / "outputs" / f"{sequence['id']}-{trial}.json")
            if len(rows) != len(sequence["steps"]):
                raise RuntimeError("Missing scenario output")
            all_rows.extend(rows)
            values = reviews[(sequence["id"], trial)]
            scores = {k: statistics.mean(v[k] for v in values) for k in evaluator.WEIGHTS}
            trial_scores.append(scores)
            trials.append(dict(sequence=sequence["id"], trial=trial, scores=scores, total=evaluator.weighted(scores)))
        scores = {k: statistics.mean(v[k] for v in trial_scores) for k in evaluator.WEIGHTS}
        sequences.append(dict(sequence=sequence["id"], title=sequence["title"], scores=scores, total=evaluator.weighted(scores),
                              repeat_range=[min(evaluator.weighted(s) for s in trial_scores), max(evaluator.weighted(s) for s in trial_scores)]))
    scores = {k: statistics.mean(s["scores"][k] for s in sequences) for k in evaluator.WEIGHTS}
    stability = []
    for item_id in [s["id"] for seq in SEQUENCES for s in seq["steps"]]:
        rows = [r for r in all_rows if r["scenario"] == item_id]
        stability.append(dict(scenario=item_id, decisions=[r["decision"] for r in rows],
                              distinct_responses=len({r["response"] for r in rows}),
                              speak_consistent=len({bool(r["response"]) for r in rows}) == 1))
    audits = []
    for pair in plans:
        if not pair["audit"]:
            continue
        first, swap = judgments[pair["original"]], judgments[pair["id"]]
        drift = [abs(first[a]["scores"][k] - swap[b]["scores"][k]) for a, b in (("A", "B"), ("B", "A")) for k in evaluator.WEIGHTS]
        mapped = {"A": "B", "B": "A", "tie": "tie"}[swap["winner"]]
        audits.append(dict(pair=pair["original"], mean_dimension_drift=statistics.mean(drift),
                           winner_consistent=first["winner"] == mapped))
    ledger = read(out / "ledger.json")
    component = {k: round(v * 20, 2) for k, v in scores.items()}
    result = dict(status="complete", engineering_score=49, character_score=round(evaluator.weighted(scores), 2),
                  components=component, assistant_likeness_risk=round(100 - component["non_assistant"], 2),
                  sequences=sequences, trials=trials, stability=stability, position_audits=audits,
                  observations=len(all_rows), actual_generation_calls=sum(len(r["requests"]) for r in all_rows),
                  silent=sum(not r["response"] for r in all_rows),
                  protocol_failures=sum(r["decision"] == "ERROR" for r in all_rows),
                  phrase_hits=sum(bool(r["surface_flags"]["patterns"]) for r in all_rows),
                  long_proactive=sum(r["surface_flags"]["long"] for r in all_rows), costs=ledger,
                  weighted_loss={k: round(evaluator.WEIGHTS[k] * (1 - v / 5), 2) for k, v in scores.items()},
                  production_unchanged=True,
                  limitations=["合成文字画面，不代表真实游戏画面识别、音色或 Live2D 表现。",
                               "独立模型调用隔离了生成逻辑与版本来源，但同厂商模型可能存在相关偏差；尚无人类校准。",
                               "A/B 比較同版本三次采样的稳定性，不是版本提升证明。",
                               "事件级评分以完整序列为单位；全静默的性格证据不足，不等于已证实人格不一致。",
                               "observe 路径从已获得截图处进入，poll 路径覆盖本地冷却/同屏；不外推整体截图识别能力。"])
    save(out / "summary.json", result)
    names = dict(presence="角色在场感", personality="人格一致性", continuity="上下文连续性", initiative="主动互动自然度",
                 entertainment="娱乐性", non_assistant="非助手感（高为好）", restraint="克制")
    lines = ["# Character Benchmark 基线", "", f"Engineering: **49/100**  ·  Character: **{result['character_score']}/100**", "",
             "这是当前版本首次角色基线；不是与工程 49 分的比较，也不是新旧版本提升。", "",
             "| 维度 | 分数 / 100 | 权重 |", "|---|---:|---:|"]
    lines += [f"| {names[k]} | {component[k]} | {evaluator.WEIGHTS[k]}% |" for k in names]
    lines += ["", f"Assistant-Likeness 风险（低为好）：{result['assistant_likeness_risk']}/100。", "",
              f"{protocol['scenarios']} 个固定场景 × 3 次，10 组连续事件；{len(all_rows)} 条完整观察记录，{result['actual_generation_calls']} 次真实生成调用。",
              f"{sum(not p['audit'] for p in plans)} 对正式盲评，2 对换位复评；静默 {result['silent']} 次，协议失败 {result['protocol_failures']} 次。",
              f"主动套话命中 {result['phrase_hits']} 次，主动长文本 {result['long_proactive']} 次。", "",
              "## 连续场景", "", "| 场景 | 角色分 | 三次范围 |", "|---|---:|---:|"]
    lines += [f"| {s['title']} | {s['total']:.2f} | {s['repeat_range'][0]:.2f}–{s['repeat_range'][1]:.2f} |" for s in sequences]
    lines += ["", "## 成本与解释边界", "", f"API 调用计数与 token：`{json.dumps(ledger)}`。生产代码 SHA-256 全部未变；生产运行 API/token 成本未变。",
              "评测费用没有可靠价格表，未推算货币金额。原始 request/response/usage、状态前后值在 private/outputs；独立裁判输入在 blind-inputs，原始裁判结果在 judgments。", ""]
    lines += ["- " + text for text in result["limitations"]]
    lines += ["", "## 换位检查", "", "```json", json.dumps(audits, ensure_ascii=False, indent=2), "```", "",
              "## 下一轮候选", "", "仅按已冻结权重列出损失，不在本次修改生产代码："]
    lines += [f"- {names[k]}：加权损失 {value} 分。" for k, value in sorted(result["weighted_loss"].items(), key=lambda kv: -kv[1])[:3]]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("character_score", "components", "costs", "production_unchanged")}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "generate", "judge", "report", "all"))
    parser.add_argument("--out", type=Path, default=ROOT / "docs/character-benchmark/baseline-2026-09-08")
    parser.add_argument("--judge-model", default="gpt-5.5")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    prepare(args.out, args.judge_model)
    if args.stage in ("generate", "all"):
        generate(args.out, args.limit)
    if args.stage in ("judge", "all"):
        evaluate(args.out, args.judge_model, args.limit)
    if args.stage in ("report", "all"):
        report(args.out)


if __name__ == "__main__":
    main()
