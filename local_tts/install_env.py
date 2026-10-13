import pathlib,subprocess,sys,shutil
root=pathlib.Path(__file__).resolve().parent
py=root/'.venv/Scripts/python.exe'; uv=shutil.which('uv')
steps=[('install-torch',[uv,'pip','install','--python',str(py),'torch==2.7.0','torchaudio==2.7.0','--index-url','https://download.pytorch.org/whl/cu128']),('install-inference',[uv,'pip','install','--python',str(py),'numpy<2','scipy','librosa==0.10.2','soundfile','pytorch-lightning>=2.4','ffmpeg-python','onnxruntime','cn2an','pypinyin','g2p_en','sentencepiece','transformers==4.51.3','peft==0.17.1','PyYAML','psutil','jieba_fast','jieba','split-lang','fast_langdetect>=0.3.1','wordsegment','rotary_embedding_torch','ToJyutping','g2pk2','ko_pron','opencc','fastapi','uvicorn','x_transformers','torchmetrics<=1.5','pydantic<=2.10.6','faster-whisper','huggingface_hub<1','einops','eunjeon','imageio-ffmpeg','matplotlib','tensorboard','setuptools==80.9.0','gradio==4.44.1','pandas==2.3.3'])]
for name,cmd in steps:
 print('START',name,flush=True)
 with (root/'logs'/f'{name}.log').open('w',encoding='utf-8') as log:
  result=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
 if result.returncode:
  print((root/'logs'/f'{name}.log').read_text(encoding='utf-8')[-9000:],flush=True);sys.exit(result.returncode)
 print('DONE',name,flush=True)
with (root/'logs'/'requirements-lock.txt').open('w') as out: subprocess.run([uv,'pip','freeze','--python',str(py)],stdout=out,check=True)
print('INSTALL_COMPLETE',flush=True)
