"""Small v2Pro acoustic fine-tune, with cross-ASR label QA and fail-closed feature checks."""
import argparse,gc,hashlib,json,os,re,shutil,subprocess,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent
VENDOR=ROOT/"vendor/GPT-SoVITS"
PRE=VENDOR/"GPT_SoVITS/pretrained_models"
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--run-name",default="voice-v2pro")
parser.add_argument("--epochs",type=int,default=6)
args=parser.parse_args()
if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",args.run_name) or not 2<=args.epochs<=100 or args.epochs%2:
    parser.error("Use a simple unique run name and an even epoch count between 2 and 100")
EXP=ROOT/"training"/args.run_name
if (EXP/"completion.json").exists():
    parser.error("This run is complete. Supply --run-name with a new name to preserve existing results.")
MODEL_NAME="ativia_voice" if args.run_name=="voice-v2pro" else "ativia_"+args.run_name.replace("-","_")
EXP.mkdir(parents=True,exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8")
os.environ["PYTHONUTF8"]="1"
os.environ["OMP_NUM_THREADS"]="4"
os.environ["MKL_NUM_THREADS"]="4"
import torch
if os.name=="nt":dll=os.add_dll_directory(str(Path(torch.__file__).parent/"lib"))
assert torch.cuda.is_available(),"CUDA required"
from faster_whisper import WhisperModel
from opencc import OpenCC
cc=OpenCC("t2s")
def norm(text):return "".join(c for c in cc.convert(text) if c.isalnum())
def cer(a,b):
 a,b=norm(a),norm(b);prev=list(range(len(b)+1))
 for i,x in enumerate(a,1):
  cur=[i]
  for j,y in enumerate(b,1):cur.append(min(cur[-1]+1,prev[j]+1,prev[j-1]+(x!=y)))
  prev=cur
 return prev[-1]/max(1,len(a))
rows=[json.loads(line) for line in (ROOT/"data/annotations.jsonl").read_text(encoding="utf-8").splitlines()]
reserved={Path(v["reference"]).stem for v in json.loads((ROOT/"configs/voices.json").read_text(encoding="utf-8")).values()}
reserved.update(Path(r).stem for r in json.loads((ROOT/"logs/evaluation.json").read_text(encoding="utf-8"))["holdout"])
validation_names={Path(line.split("|")[0]).stem for line in (ROOT/"data/validation.list").read_text(encoding="utf-8").splitlines()}
reserved.update(validation_names)
candidates=sorted((r for r in rows if not r["qa_flags"] and 3<=r["seconds"]<=8 and r["avg_logprob"]>-.46 and Path(r["audio"]).stem not in reserved),key=lambda r:r["avg_logprob"],reverse=True)[:120]
qa_path=EXP/"label-crosscheck.jsonl"
checked={r["source"]:r for r in map(json.loads,qa_path.read_text(encoding="utf-8").splitlines())} if qa_path.exists() else {}
asr=WhisperModel(str(ROOT/"models/faster-whisper-medium"),device="cuda",compute_type="int8_float16",cpu_threads=4)
accepted=[]
for row in candidates:
 if row["source"] not in checked:
  seg,_=asr.transcribe(str(ROOT/row["audio"]),language="zh",beam_size=5,condition_on_previous_text=False,vad_filter=True)
  text="".join(s.text for s in seg).strip()
  item=dict(row,second_asr=text,asr_disagreement=cer(row["text"],text))
  with qa_path.open("a",encoding="utf-8") as f:f.write(json.dumps(item,ensure_ascii=False)+"\n")
  checked[row["source"]]=item
 item=checked[row["source"]]
 if item["asr_disagreement"]<=.12:accepted.append(item)
 if len(accepted)>=48:break
print("LABEL_QA",len(checked),"accepted",len(accepted),flush=True)
if len(accepted)<24:raise RuntimeError("Not enough independently consistent labels; training refused")
del asr;gc.collect();torch.cuda.empty_cache()
listfile=EXP/"selected.list"
listfile.write_text("\n".join(f'{(ROOT/r["audio"]).as_posix()}|dataset_voice|zh|{cc.convert(r["text"]).replace(chr(124),"，")}' for r in accepted)+"\n",encoding="utf-8")
(EXP/"selected.json").write_text(json.dumps(accepted,ensure_ascii=False,indent=2),encoding="utf-8")
# The discriminator belongs to exactly the same pretraining release as the generator.
disc=PRE/"v2Pro/s2Dv2Pro.pth"
url="https://huggingface.co/lj1995/GPT-SoVITS/resolve/336b2ec4e8d4ac74740798dd40af44e74659ecaf/v2Pro/s2Dv2Pro.pth"
if not disc.exists():
 with urllib.request.urlopen(url,timeout=180) as source,disc.with_suffix(".partial").open("wb") as out:shutil.copyfileobj(source,out,1024*1024)
 disc.with_suffix(".partial").replace(disc)
with disc.open("rb") as f:digest=hashlib.file_digest(f,"sha256").hexdigest()
(EXP/"discriminator-source.json").write_text(json.dumps(dict(url=url,sha256=digest),indent=2))
env=dict(os.environ,inp_text=str(listfile),inp_wav_dir=str(ROOT/"data/prepared"),exp_name="ativia_voice",i_part="0",all_parts="1",opt_dir=str(EXP),bert_pretrained_dir=str(PRE/"chinese-roberta-wwm-ext-large"),cnhubert_base_dir=str(PRE/"chinese-hubert-base"),sv_path=str(PRE/"sv/pretrained_eres2netv2w24s4ep4.ckpt"),is_half="True",version="v2Pro",_CUDA_VISIBLE_DEVICES="0",PYTHONPATH=str(VENDOR)+os.pathsep+str(VENDOR/"GPT_SoVITS"),NLTK_DATA=str(ROOT/"models/nltk_data"),PATH=str(ROOT/"bin")+os.pathsep+os.environ["PATH"])
(EXP/"environment.json").write_text(json.dumps({k:env[k] for k in ("inp_text","inp_wav_dir","exp_name","i_part","all_parts","opt_dir","bert_pretrained_dir","cnhubert_base_dir","sv_path","is_half","version","_CUDA_VISIBLE_DEVICES")},indent=2))
def run(label,args):
 print("STAGE",label,flush=True)
 with (EXP/(label+".log")).open("a",encoding="utf-8") as log:
  result=subprocess.run([sys.executable,"-u",*args],cwd=VENDOR,env=env,stdout=log,stderr=subprocess.STDOUT)
 if result.returncode:raise RuntimeError(label+" failed; inspect saved log")
for stage in ("1-get-text","2-get-hubert-wav32k","2-get-sv"):
 run(stage,[str(VENDOR/"GPT_SoVITS/prepare_datasets"/(stage+".py"))])
shutil.copy2(EXP/"2-name2text-0.txt",EXP/"2-name2text.txt")
expected={Path(r["audio"]).name for r in accepted}
text_names={line.split("\t")[0] for line in (EXP/"2-name2text.txt").read_text(encoding="utf-8").splitlines()}
assert expected==text_names,"Text preparation dropped selected records"
for name in expected:
 for folder in ("3-bert","4-cnhubert","7-sv_cn"):
  feature=torch.load(EXP/folder/(name+".pt"),map_location="cpu",weights_only=True)
  assert torch.isfinite(feature).all(),(name,folder,"nonfinite features")
 assert (EXP/"5-wav32k"/name).is_file(),name
print("FEATURES_VALIDATED",len(expected),flush=True)
config=json.loads((VENDOR/"GPT_SoVITS/configs/s2v2Pro.json").read_text(encoding="utf-8"))
config["train"].update(epochs=args.epochs,batch_size=2,learning_rate=5e-5,text_low_lr_rate=.2,fp16_run=True,log_interval=10,gpu_numbers="0",pretrained_s2G=str(PRE/"v2Pro/s2Gv2Pro.pth"),pretrained_s2D=str(disc),save_every_epoch=2,if_save_latest=0,if_save_every_weights=True)
config["data"]["exp_dir"]=str(EXP)
config["model"]["version"]="v2Pro"
config.update(s2_ckpt_dir=str(EXP/"tensorboard"),save_weight_dir=str(ROOT/"models/trained"),name=MODEL_NAME)
(ROOT/"models/trained").mkdir(exist_ok=True)
(EXP/"logs_s2_v2Pro").mkdir(exist_ok=True)
configfile=EXP/"finetune-s2.json";configfile.write_text(json.dumps(config,indent=2))
# Keep a documented Windows adaptation: avoid five spawned loaders duplicating RAM.
trainer=VENDOR/"GPT_SoVITS/s2_train.py"
original=trainer.read_text(encoding="utf-8")
backup=EXP/"s2_train.upstream.py"
if not backup.exists():backup.write_text(original,encoding="utf-8")
adapted=original.replace("num_workers=5,","num_workers=0,").replace("persistent_workers=True,","persistent_workers=False,").replace("prefetch_factor=3,","prefetch_factor=None,")
trainer.write_text(adapted,encoding="utf-8")
(EXP/"adaptation.json").write_text(json.dumps(dict(reason="Windows RAM-safe DataLoader; no model/loss changes",changes=["num_workers: 5 -> 0","persistent_workers: true -> false","prefetch_factor: 3 -> null"]),indent=2))
run("train-s2",[str(trainer),"--config",str(configfile)])
weights=list((ROOT/"models/trained").glob(f"{MODEL_NAME}_e{args.epochs}_*.pth"))
if not weights:raise RuntimeError("Training returned without final exported checkpoint")
(EXP/"completion.json").write_text(json.dumps(dict(status="completed",epochs=args.epochs,clips=len(accepted),seconds=sum(r["seconds"] for r in accepted),weights=[str(p) for p in weights],stage="SoVITS acoustic only; GPT pretrained retained"),indent=2))
print("TRAINING_COMPLETE",str(weights[0]),flush=True)
