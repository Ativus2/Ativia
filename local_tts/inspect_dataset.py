import sys,zipfile,pathlib,wave,hashlib,json,collections,numpy as np
sys.stdout.reconfigure(encoding='utf-8')
root=pathlib.Path('local_tts').resolve(); target=root/'data'/'raw'; target.mkdir(exist_ok=True)
archive=pathlib.Path(r'C:\Users\kzcru\Downloads\%E6%B0%B8%E9%9B%8F%E5%A1%94%E8%8F%B2.zip')
rows=[]
with zipfile.ZipFile(archive) as z:
 for info in z.infolist():
  if info.is_dir(): continue
  rel=pathlib.PurePosixPath(info.filename)
  if rel.is_absolute() or '..' in rel.parts or ':' in info.filename or '\\' in info.filename: raise ValueError('Unsafe archive path')
  if rel.suffix.lower()!='.wav': continue
  path=target.joinpath(*rel.parts); path.parent.mkdir(parents=True,exist_ok=True)
  content=z.read(info); path.write_bytes(content)
  with wave.open(str(path)) as w:
   sr=w.getframerate(); x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
  win=sr//50; frames=x[:len(x)//win*win].reshape(-1,win); rms=np.sqrt(np.mean(frames**2,axis=1))
  rows.append(dict(path=str(path.relative_to(root)),seconds=len(x)/sr,sample_rate=sr,channels=1,pcm_bits=16,sha256=hashlib.sha256(content).hexdigest(),peak=float(np.max(np.abs(x))),clip_ratio=float(np.mean(np.abs(x)>=.999)),rms_db=float(20*np.log10(max(np.sqrt(np.mean(x*x)),1e-9))),quiet_frame_ratio=float(np.mean(rms<.005)),p10_frame_db=float(20*np.log10(max(np.percentile(rms,10),1e-9)))))
summary=dict(files=len(rows),seconds=sum(r['seconds'] for r in rows),archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),formats=['WAV PCM16 mono 44100Hz'],transcripts=False,duplicates=len(rows)-len(set(r['sha256'] for r in rows)),clipped_files=sum(r['clip_ratio']>.001 for r in rows),mostly_quiet_files=sum(r['quiet_frame_ratio']>.6 for r in rows),duration_quantiles=np.percentile([r['seconds'] for r in rows],[0,25,50,75,100]).tolist())
(root/'logs'/'dataset-inspection.json').write_text(json.dumps(dict(summary=summary,files=rows),ensure_ascii=False,indent=2),encoding='utf-8')
candidates=sorted((r for r in rows if 4<=r['seconds']<=9 and r['clip_ratio']==0 and .03<r['quiet_frame_ratio']<.35),key=lambda r: r['quiet_frame_ratio'])[:12]
(root/'configs'/'reference-candidates.json').write_text(json.dumps(candidates,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False)); print('CANDIDATES',json.dumps(candidates[:3],ensure_ascii=False))
