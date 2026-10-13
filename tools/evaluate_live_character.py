"""Bounded live smoke test: 4 synthetic screens x 2 versions, no private desktop capture.

Run explicitly; never part of unit test discovery. Stops after the first API error.
Results retain model replies for review, not fabricated subjective ratings.
"""
import argparse
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from character import CharacterMemory, Observation
from persona import PROFILE
import vision

CASES = [
    ("coding", "Editor | parser.py", ["def read_names(lines):", "    names = []", "    for line in lines:", "        names.append(line.strip())", "    return names", "", "Terminal: ready"], "quiet"),
    ("empty", "", [], "quiet"),
    ("error", "Terminal | current run", ["python app.py", "Traceback (most recent call last):", "  File app.py, line 3", "    import numpy", "ModuleNotFoundError: No module named 'numpy'"], "grounded reaction or quiet"),
    ("twist", "Game | The last page", ["THE END?", "The detective removes the mask.", "It was the cat all along.", "Ending unlocked: The Smallest Detective"], "grounded reaction or quiet"),
]


def scene_contract(name: str, observation: Observation, delivered: str) -> bool:
    """Narrow fixture criteria, not a general-purpose personality/accuracy score."""
    if name == "coding":
        return observation.scene == "coding" and observation.event == "none" and not delivered
    if name == "empty":
        return observation.scene == "unknown" and observation.confidence == "low" and not delivered
    if name == "error":
        return (observation.event == "error" and not observation.focused
                and "numpy" in delivered.casefold() and len(delivered) <= 24)
    if name == "twist":
        return (observation.scene == "game" and observation.event in {"success", "surprise"}
                and not observation.focused and "猫" in delivered and len(delivered) <= 24
                and not any(word in delivered for word in ("凶手", "真凶", "犯人", "杀手", "嫌犯", "嫌疑人")))
    raise ValueError("Unknown fixture")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iteration", type=int, choices=(4, 5), default=4)
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--trial", choices=("a", "b"), default="a")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    suffix = "" if args.trial == "a" else "-b"
    output_dir = ROOT / f"docs/character-iterations/live-eval-{args.iteration}{suffix}"
    output_dir.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("baseline_vision", ROOT / f"docs/character-iterations/before-{args.iteration}/vision.py")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    report = {"kind": "live model smoke test on synthetic fixtures; no subjective scores",
              "maximum_calls": 4 if args.candidate_only else 8, "attempted_calls": 0, "results": []}
    def save():
        (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, title, lines, expected in CASES:
        picture = Image.new("RGB", (1000, 620), "#18212b")
        draw = ImageDraw.Draw(picture)
        font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 25)
        draw.rectangle((0, 0, 1000, 62), fill="#293643")
        draw.text((24, 18), title, font=font, fill="white")
        for index, line in enumerate(lines):
            draw.text((30, 95 + index * 48), line, font=font, fill="#e0e6ed")
        picture.save(output_dir / f"{name}.png")
        image_bytes = io.BytesIO()
        picture.save(image_bytes, format="JPEG", quality=85)
        versions = [(f"after-{args.iteration}{suffix}", vision)] if args.candidate_only else [
            (f"before-{args.iteration}", baseline), (f"after-{args.iteration}{suffix}", vision)]
        for version, module in versions:
            row = {"case": name, "version": version, "expected": expected}
            report["results"].append(row)
            def client_factory(**kwargs):
                # Bounded smoke-test limits, not a change to application settings.
                kwargs.update(timeout=20.0, max_retries=0)
                client = OpenAI(**kwargs)
                def create(**request):
                    report["attempted_calls"] += 1
                    save()
                    result = client.chat.completions.create(**request)
                    if result.usage:
                        row["usage"] = result.usage.model_dump()
                    return result
                return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            try:
                with patch.object(module, "OpenAI", side_effect=client_factory):
                    result = module.comment_on_screenshot(image_bytes.getvalue(), automatic=True,
                                                          observe_scene=True, observation_context="[]")
                if not isinstance(result, Observation):
                    raise ValueError("Unexpected protocol result")
                row["observation"] = result.__dict__
                candidate = result
                if (module is vision or args.iteration >= 5) and not PROFILE.allows(result.intent, result.event):
                    candidate = replace(result, intent="WAIT", reply="")
                row["delivered_reply"] = CharacterMemory().accept(candidate, 100)
                row["quiet_contract_passed"] = not row["delivered_reply"] if expected == "quiet" else None
                row["scene_contract_passed"] = scene_contract(name, result, row["delivered_reply"])
            except Exception as exc:
                # Do not write request URLs, headers, API keys or raw server errors.
                cause = exc.__cause__ or exc
                row["error_type"] = type(cause).__name__
                row["status_code"] = getattr(cause, "status_code", None)
                report["stopped_on_error"] = True
                save()
                print(json.dumps({"stopped": True, "error_type": row["error_type"], "status_code": row["status_code"], "report": str(output_dir / "report.json")}))
                return
            save()
            print(json.dumps({"case": name, "version": version, "reply": row["delivered_reply"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
