"""Offline ASR, conservative trim/resample, resumable annotation with QA flags."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.stdout.reconfigure(encoding="utf-8")
import time
ROOT = Path(__file__).resolve().parent
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
import torch
DLL_HANDLES = []
if os.name == "nt":
    DLL_HANDLES.append(os.add_dll_directory(str(Path(torch.__file__).parent / "lib")))
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

parser = argparse.ArgumentParser()
parser.add_argument("--all", action="store_true")
args = parser.parse_args()
model = WhisperModel(str(ROOT / "models" / "faster-whisper-large-v3-turbo"), device="cuda", compute_type="int8_float16", cpu_threads=4)
source = "logs/dataset-inspection.json" if args.all else "configs/reference-candidates.json"
records = json.loads((ROOT / source).read_text(encoding="utf-8"))
if args.all:
    records = records["files"]
manifest = ROOT / "data" / ("annotations.jsonl" if args.all else "reference-transcripts.jsonl")
existing = {}
if manifest.exists():
    existing = {r["source"]: r for r in map(json.loads, manifest.read_text(encoding="utf-8").splitlines())}
for index, row in enumerate(records):
    if row["path"] in existing:
        continue
    samples, sr = sf.read(ROOT / row["path"], dtype="float32")
    # Keep original slices; trim only quiet leading/trailing edges, retaining 150 ms context.
    win = max(1, sr // 100)
    energy = np.array([np.sqrt(np.mean(samples[i:i+win]**2)) for i in range(0, len(samples), win)])
    active = np.flatnonzero(energy > 0.005)
    flags = []
    if not len(active):
        flags.append("no_active_audio")
        begin, end = 0, len(samples)
    else:
        begin = max(0, int(active[0]*win - sr*.15))
        end = min(len(samples), int((active[-1]+1)*win + sr*.15))
    trimmed = samples[begin:end]
    audio16 = resample_poly(trimmed, 160, 441).astype(np.float32)
    started = time.monotonic()
    segments, info = model.transcribe(audio16, language="zh", beam_size=5, condition_on_previous_text=False, vad_filter=True)
    segments = list(segments)
    text = "".join(s.text for s in segments).strip()
    avg_logprob = float(np.mean([s.avg_logprob for s in segments])) if segments else -99.0
    if not text: flags.append("empty_transcript")
    if any(word in text for word in ("明镜", "明鏡", "字幕由", "字幕提供", "Amara.org")):
        flags.append("suspected_asr_hallucination")
    if text and not 1.5 <= len(text.replace(" ", "")) / (len(trimmed)/sr) <= 12:
        flags.append("implausible_speech_rate")
    if avg_logprob < -0.6: flags.append("low_asr_confidence")
    if row["quiet_frame_ratio"] > .6: flags.append("mostly_quiet")
    if any(s.compression_ratio > 2.4 for s in segments): flags.append("asr_repetition")
    if len(trimmed)/sr < 2: flags.append("too_short_for_training")
    out = ROOT / "data" / "prepared" / (Path(row["path"]).stem + ".wav")
    out.parent.mkdir(exist_ok=True)
    sf.write(out, resample_poly(trimmed, 320, 441), 32000, subtype="PCM_16")
    item = dict(source=row["path"], audio=str(out.relative_to(ROOT)), text=text, language="zh",
                seconds=len(trimmed)/sr, trim_start=begin/sr, trim_end=end/sr, avg_logprob=avg_logprob,
                qa_flags=flags, reviewed="automated_only", asr_model="mobiuslabsgmbh/faster-whisper-large-v3-turbo", elapsed=time.monotonic()-started)
    with manifest.open("a", encoding="utf-8") as f: f.write(json.dumps(item, ensure_ascii=False)+"\n")
    existing[row["path"]] = item
    print(index+1, len(records), json.dumps(item, ensure_ascii=False), flush=True)
valid = [r for r in existing.values() if not r["qa_flags"]]
if args.all:
    valid.sort(key=lambda r:r["source"])
    for name, rows in (("validation.list",valid[::20]), ("train.list",[r for i,r in enumerate(valid) if i%20])):
        (ROOT/"data"/name).write_text("\n".join(f'{(ROOT/r["audio"]).as_posix()}|dataset_voice|zh|{r["text"]}' for r in rows)+"\n",encoding="utf-8")
else:
    valid = [r for r in valid if 3 <= r["seconds"] <= 9.5]
    valid.sort(key=lambda r:r["avg_logprob"], reverse=True)
    if not valid: raise RuntimeError("No reference passed automated QA")
    profiles = {}
    for index, row in enumerate(valid[:3]):
        key = "dataset_voice" if index == 0 else f"dataset_voice_{index+1}"
        profiles[key] = dict(label="数据集音色" + ("" if index==0 else f" · 参考 {index+1}"), reference=row["audio"], text=row["text"], language="zh")
    (ROOT/"configs"/"voices.json").write_text(json.dumps(profiles,ensure_ascii=False,indent=2),encoding="utf-8")
print("ASR_DONE",len(existing),"accepted",len(valid),flush=True)
