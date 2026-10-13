"""Run a known local job with complete timestamped stdout/stderr logs."""
from datetime import datetime
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parent
jobs={"prepare":["prepare_references.py","--all"],"references":["prepare_references.py"],"evaluate":["evaluate.py","--voice","dataset_voice_2"],"hardware":["check_runtime.py"]}
if len(sys.argv)!=2 or sys.argv[1] not in jobs:raise SystemExit("Choose prepare, references, evaluate, or hardware")
name=sys.argv[1]
path=ROOT/"logs"/(datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+name+".log")
print("LOG",str(path),flush=True)
with path.open("w",encoding="utf-8") as out:
    result=subprocess.run([sys.executable,"-u",str(ROOT/jobs[name][0]),*jobs[name][1:]],stdout=out,stderr=subprocess.STDOUT,cwd=ROOT.parent)
print("FINISHED",name,"exit",result.returncode,flush=True)
sys.exit(result.returncode)
