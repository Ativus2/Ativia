"""Generate multilingual probes and score ASR consistency / speaker embeddings locally."""
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.request import Request, build_opener, ProxyHandler
ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
OPENER = build_opener(ProxyHandler({}))

def generate(name, text, language, voice):
    payload = dict(text=text, language=language, voice=voice, speed=1.2)
    existing = ROOT / "samples" / (name+".wav")
    if "--reuse-existing" in sys.argv and existing.exists():
        print("REUSE",name,flush=True)
        return dict(name=name,text=text,language=language,voice=voice,file=str(existing.relative_to(ROOT)),elapsed=None,reused=True)
    begin = time.monotonic()
    req = Request("http://127.0.0.1:9881/tts", json.dumps(payload,ensure_ascii=False).encode(), {"Content-Type":"application/json"})
    with OPENER.open(req,timeout=180) as r: content=r.read()
    path = ROOT / "samples" / (name+".wav")
    path.write_bytes(content)
    row = dict(name=name, text=text, language=language, voice=voice, file=str(path.relative_to(ROOT)),elapsed=time.monotonic()-begin)
    print("GENERATED", name, len(content), round(row["elapsed"],2),flush=True)
    return row

with OPENER.open("http://127.0.0.1:9881/health",timeout=10) as response: HEALTH=json.load(response)
MODEL=HEALTH["model"]
voices = json.loads((ROOT/"configs/voices.json").read_text(encoding="utf-8"))
rows = []
for voice in voices:
    rows.append(generate("reference-"+voice,"先别急着点下一步，把题目里给出的条件再看一遍。","zh",voice))
# CPU speaker feature comparison, so the server keeps the GPU available.
import numpy as np
import soundfile as sf
import torch
import torchaudio
VENDOR = ROOT/"vendor/GPT-SoVITS"
os.chdir(VENDOR)
sys.path[:0]=[str(VENDOR),str(VENDOR/"GPT_SoVITS")]
from sv import SV
torch.set_num_threads(4)
sv = SV("cpu",False)
def embedding(path):
    samples,sr=sf.read(path,dtype="float32")
    wav=torch.tensor(samples).reshape(1,-1)
    if sr!=16000:wav=torchaudio.functional.resample(wav,sr,16000)
    return torch.nn.functional.normalize(sv.compute_embedding3(wav).float(),dim=-1).squeeze()
source = json.loads((ROOT/"logs/dataset-inspection.json").read_text(encoding="utf-8"))["files"]
used = {Path(v["reference"]).stem for v in voices.values()}
holdout=[r for r in source if 4<=r["seconds"]<=9 and r["quiet_frame_ratio"]<.3 and Path(r["path"]).stem not in used][::45][:5]
held_embeddings=[embedding(ROOT/r["path"]) for r in holdout]
for row in rows:
    emb=embedding(ROOT/row["file"])
    ref=embedding(ROOT/voices[row["voice"]]["reference"])
    row["similarity_to_reference"]=float(emb@ref)
    row["similarity_to_holdout"]=float(np.median([float(emb@v) for v in held_embeddings]))
best = sys.argv[sys.argv.index("--voice")+1] if "--voice" in sys.argv else max(rows,key=lambda r:r["similarity_to_holdout"])["voice"]
print("BEST_REFERENCE",best,flush=True)
probes=[("zh-short","我在这里。这个地方先别着急，我们一步一步看清楚。","zh"),
        ("zh-long","这道题可以分成三个步骤。先把已知条件列出来，确认哪些量保持不变。然后选择合适的公式，把单位统一以后再代入计算。最后检查答案是否符合题目的要求。如果某一步还不理解，就回到对应的定义，用一个简单的例子验证。不要只记住最后的答案，弄清楚每一步为什么这样做，下一次遇到类似的问题才会更容易。","zh"),
        ("ko-short","안녕하세요. 저는 여기 있어요. 이 문제를 함께 살펴볼까요?","ko"),
        ("ko-long","먼저 문제에서 주어진 조건을 확인하세요. 중요한 단어에 표시하고, 모르는 부분은 작은 단계로 나누어 생각해 보세요. 마지막으로 답을 다시 확인하면 실수를 줄일 수 있어요.","ko"),
        ("en-short","Hello, I am here. Let us take a closer look at this question together.","en"),
        ("en-long","First, read the question carefully and write down the information you already know. Then divide the problem into smaller steps. Check the units before you calculate, and make sure the final answer actually addresses the question. If something is unclear, we can work through a simple example together.","en")]
for name,text,lang in probes: rows.append(generate(name,text,lang,best))
# Independent medium ASR kept separate from the turbo model used for reference labels.
if os.name=="nt":
    dll=os.add_dll_directory(str(Path(torch.__file__).parent/"lib"))
from faster_whisper import WhisperModel
from opencc import OpenCC
asr=WhisperModel(str(ROOT/"models/faster-whisper-medium"),device="cpu" if "--cpu-asr" in sys.argv else "cuda",compute_type="int8" if "--cpu-asr" in sys.argv else "int8_float16",cpu_threads=4)
converter=OpenCC("t2s")
def normalized(text,lang):
    text=converter.convert(unicodedata.normalize("NFC",text)).lower()
    if lang=="en":return re.findall(r"[a-z0-9]+",text)
    return list("".join(c for c in text if c.isalnum()))
def error_rate(a,b):
    previous=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        current=[i]
        for j,y in enumerate(b,1): current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(x!=y)))
        previous=current
    return previous[-1]/max(1,len(a))
for row in rows:
    data,sr=sf.read(ROOT/row["file"],dtype="float32")
    seg,_=asr.transcribe(str(ROOT/row["file"]),language=row["language"],beam_size=5,condition_on_previous_text=False)
    transcription="".join(s.text for s in seg).strip()
    row.update(transcription=transcription,asr_error_rate=error_rate(normalized(row["text"],row["language"]),normalized(transcription,row["language"])),
               duration=len(data)/sr,sample_rate=sr,clip_ratio=float(np.mean(np.abs(data)>=.999)),finite=bool(np.isfinite(data).all()))
    if "similarity_to_reference" not in row:
        emb=embedding(ROOT/row["file"])
        row["similarity_to_reference"]=float(emb@embedding(ROOT/voices[best]["reference"]))
        row["similarity_to_holdout"]=float(np.median([float(emb@v) for v in held_embeddings]))
    print("EVALUATED",json.dumps(row,ensure_ascii=False),flush=True)
    (ROOT/"logs/evaluation-progress.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
result=dict(model=MODEL,method=HEALTH.get("method", "zero-shot"),weights=HEALTH.get("weights"),best_voice=best,rows=rows,
            holdout=[r["path"] for r in holdout],similarity_note="ERes2Net cosine similarity; internal model proxy, not a human rating or percentage",
            asr_note="Independent Whisper medium: text consistency proxy, not human intelligibility rating")
(ROOT/"logs/evaluation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print("EVALUATION_COMPLETE",flush=True)
