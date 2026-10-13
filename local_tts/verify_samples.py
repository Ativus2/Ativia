import os,json,sys,re,unicodedata
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
import torch
if os.name=="nt": dll=os.add_dll_directory(str(Path(torch.__file__).parent/"lib"))
from faster_whisper import WhisperModel
from opencc import OpenCC
cc=OpenCC("t2s")
model=WhisperModel(str(ROOT/"models/faster-whisper-large-v3-turbo"),device="cuda",compute_type="int8_float16",cpu_threads=4)
def normalize(text,lang):
 text=cc.convert(unicodedata.normalize("NFC",text)).lower()
 if lang=="en":
  for a,b in (("i'm","i am"),("we'll","we will"),("there's","there is"),("it's","it is")):text=text.replace(a,b)
  return re.findall(r"[a-z0-9]+",text)
 return list("".join(c for c in text if c.isalnum()))
def distance(a,b):
 prev=list(range(len(b)+1))
 for i,x in enumerate(a,1):
  cur=[i]
  for j,y in enumerate(b,1):cur.append(min(cur[-1]+1,prev[j]+1,prev[j-1]+(x!=y)))
  prev=cur
 return prev[-1]/max(1,len(a))
data=json.loads((ROOT/"logs/evaluation.json").read_text(encoding="utf-8"))
for row in data["rows"]:
 seg,info=model.transcribe(str(ROOT/row["file"]),language=row["language"],beam_size=5,condition_on_previous_text=False,vad_filter=False)
 text="".join(s.text for s in seg).strip()
 row["turbo_transcription"]=text
 row["turbo_error_rate"]=distance(normalize(row["text"],row["language"]),normalize(text,row["language"]))
 print(row["name"],round(row["turbo_error_rate"],4),text,flush=True)
(ROOT/"logs/evaluation-crosscheck.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
