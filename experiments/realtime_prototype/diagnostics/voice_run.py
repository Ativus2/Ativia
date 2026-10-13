"""Finite diagnostic runner. All outputs are new, immutable historical benchmark is read-only."""
import argparse,json,subprocess,sys
from pathlib import Path
from time import monotonic
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from tools.realtime_benchmark.common import GpuTrace
OUT=ROOT/'docs/quick-dialogue-diagnostic'
NODE=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['vad','voice','interruption','echo','screenshot'])
    parser.add_argument('--output',type=Path,default=OUT);args=parser.parse_args()
    folder=args.output/args.phase
    if folder.exists():raise RuntimeError('refuse_overwrite_existing_run:'+str(folder))
    folder.mkdir(parents=True)
    errorlog=(folder/'browser-errors.log').open('w',encoding='utf-8')
    process=subprocess.Popen([str(NODE),str(Path(__file__).with_name('browser.cjs'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=errorlog,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    def command(data):
        process.stdin.write(json.dumps(data)+'\n');process.stdin.flush()
        result=json.loads(process.stdout.readline())
        if not result.get('ok'):raise RuntimeError(result.get('error','browser_failed'))
        return result['result']
    rows=[];gpu=GpuTrace()
    try:
        ready=json.loads(process.stdout.readline());assert ready.get('ready'),ready
        if args.phase=='voice':
            gpu.start()
            for index in range(1,21):
                for stt in (['file','realtime'] if index%2 else ['realtime','file']):
                    start=monotonic()
                    try:row=command(dict(type='replay',index=index,stt=stt))
                    except Exception as exc:row=dict(index=index,stt_adapter=stt,status='failed',error=str(exc))
                    row.update(gpu.peaks(start,monotonic()));rows.append(row)
                    with (folder/'rows.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
                    print(json.dumps(dict(index=index,stt=stt,status=row['status'],first_ms=row.get('first_meaningful_audible_ms'),
                        stt_ms=row.get('speech_end_to_stt_final_ms'),llm_ms=row.get('stt_final_to_first_token_ms')),ensure_ascii=False),flush=True)
            gpu.stop();(folder/'gpu.json').write_text(json.dumps(gpu.samples),encoding='utf-8')
        else:
            rows=command(dict(type=args.phase,count=20,path=str(folder/'ui.png')))
            print(json.dumps(dict(phase=args.phase,complete=True)),flush=True)
        (folder/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        if gpu.thread and gpu.thread.is_alive():gpu.stop()
        process.stdin.close()
        try:process.wait(15)
        except subprocess.TimeoutExpired:process.terminate();process.wait(5)
        errorlog.close()

if __name__=='__main__':main()
