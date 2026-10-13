import urllib.request,json,pathlib,time,hashlib,concurrent.futures,zipfile,shutil
ROOT=pathlib.Path(__file__).resolve().parent
VENDOR=ROOT/'vendor'/'GPT-SoVITS'; PRE=VENDOR/'GPT_SoVITS'/'pretrained_models'
HEADERS={'User-Agent':'Ativia-local-tts/1.0'}
def request(url):return urllib.request.urlopen(urllib.request.Request(url,headers=HEADERS),timeout=120)
def download(job):
 url,path=job;path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists():return dict(url=url,path=str(path.relative_to(ROOT)),bytes=path.stat().st_size,sha256=hashlib.file_digest(path.open('rb'),'sha256').hexdigest())
 print('DOWNLOAD',path.name,flush=True)
 partial=path.with_name(path.name+'.partial')
 for attempt in range(3):
  try:
   with request(url) as source,partial.open('wb') as out:shutil.copyfileobj(source,out,1024*1024)
   if partial.stat().st_size<10:raise ValueError('Empty download')
   partial.replace(path);break
  except Exception:
   if attempt==2:raise
   time.sleep(2+attempt*3)
 print('READY',path.name,path.stat().st_size,flush=True)
 with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
 return dict(url=url,path=str(path.relative_to(ROOT)),bytes=path.stat().st_size,sha256=digest)
def files(repo,patterns,target):
 revisions={'lj1995/GPT-SoVITS':'336b2ec4e8d4ac74740798dd40af44e74659ecaf','Systran/faster-whisper-medium':'08e178d48790749d25932bbc082711ddcfdfbc4f','mobiuslabsgmbh/faster-whisper-large-v3-turbo':'0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf','XXXXRT/GPT-SoVITS-Pretrained':'0c47645e02a7bc3688d7b263b0042c81e3cd82cd'}
 info=json.load(request('https://huggingface.co/api/models/'+repo+'/revision/'+revisions[repo]));rev=info['sha'];names=[s['rfilename'] for s in info['siblings']]
 chosen=[n for n in names if any(n==p or (p.endswith('/') and n.startswith(p) and pathlib.PurePosixPath(n).suffix in ('.json','.txt','.bin','.safetensors')) for p in patterns)]
 print('MODEL',repo,rev,chosen,flush=True)
 return [('https://huggingface.co/'+repo+'/resolve/'+rev+'/'+urllib.parse.quote(n,safe='/'),target/n) for n in chosen]
import urllib.parse
jobs=files('lj1995/GPT-SoVITS',['s1v3.ckpt','v2Pro/s2Gv2Pro.pth','v2Pro/s2Dv2Pro.pth','v2Pro/s2Gv2ProPlus.pth','sv/pretrained_eres2netv2w24s4ep4.ckpt','chinese-roberta-wwm-ext-large/','chinese-hubert-base/'],PRE)
jobs+=files('Systran/faster-whisper-medium',['config.json','model.bin','tokenizer.json','vocabulary.json','vocabulary.txt','preprocessor_config.json'],ROOT/'models'/'faster-whisper-medium')
jobs+=files('mobiuslabsgmbh/faster-whisper-large-v3-turbo',['config.json','model.bin','tokenizer.json','vocabulary.json','vocabulary.txt','preprocessor_config.json'],ROOT/'models'/'faster-whisper-large-v3-turbo')
jobs+=files('XXXXRT/GPT-SoVITS-Pretrained',['G2PWModel.zip'],ROOT/'models')
jobs.append(('https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin',PRE/'fast_langdetect'/'lid.176.bin'))
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
 results=list(pool.map(download,jobs))
(ROOT/'logs'/'model-downloads.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
with zipfile.ZipFile(ROOT/'models'/'G2PWModel.zip') as z:
 for info in z.infolist():
  rel=pathlib.PurePosixPath(info.filename)
  if info.is_dir():continue
  if rel.is_absolute() or '..' in rel.parts:raise ValueError('Unsafe archive')
  p=VENDOR/'GPT_SoVITS'/'text'/str(rel);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(info))
print('ALL_MODELS_READY',flush=True)
