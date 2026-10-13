import os
import sys
import json
import shutil
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent
import psutil
import torch
import imageio_ffmpeg
import nltk
assert torch.cuda.is_available(), "CUDA not available in isolated environment"
torch.set_num_threads(4)
x = torch.randn(256,256,device="cuda")
y=x@x
assert torch.isfinite(y).all()
torch.cuda.synchronize()
info = dict(python=sys.version, torch=torch.__version__,cuda_runtime=torch.version.cuda,cuda_available=True,
            gpu=torch.cuda.get_device_name(0),vram_bytes=torch.cuda.get_device_properties(0).total_memory,
            free_vram_bytes=torch.cuda.mem_get_info()[0],ram_bytes=psutil.virtual_memory().total,
            ram_available=psutil.virtual_memory().available,disks={d:dict(zip(("total","used","free"),shutil.disk_usage(d))) for d in ("C:/","D:/")})
(ROOT/"logs"/"hardware.json").write_text(json.dumps(info,indent=2))
print(json.dumps(info),flush=True)
(ROOT/"bin").mkdir(exist_ok=True)
shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(),ROOT/"bin"/"ffmpeg.exe")
for name in ("averaged_perceptron_tagger","averaged_perceptron_tagger_eng","cmudict"):
    if not nltk.download(name,download_dir=str(ROOT/"models"/"nltk_data"),quiet=True):raise RuntimeError("NLTK download failed: "+name)
print("RUNTIME_CHECK_PASSED",flush=True)
