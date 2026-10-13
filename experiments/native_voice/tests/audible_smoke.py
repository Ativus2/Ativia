"""Final one-session smoke plus local render-endpoint energy, no audio retained."""
import json,subprocess,sys,time
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from audio import SystemAudioRecorder
node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
recorder=SystemAudioRecorder(seconds=1)
levels=[];error=None;process=None
try:
    recorder.start()
    process=subprocess.Popen([str(node),'tests/smoke.mjs','--final'],cwd=ROOT/'experiments/native_voice')
    deadline=time.monotonic()+90
    while process.poll() is None and time.monotonic()<deadline:
        samples=recorder.snapshot()
        levels.append({'unix_ms':int(time.time()*1000),'rms':float(np.sqrt(np.mean(samples*samples)))})
        time.sleep(.1)
    if process.poll() is None:
        process.terminate();process.wait(timeout=5);error='finite_smoke_deadline'
except Exception as exc:error=type(exc).__name__+': '+str(exc)
finally:
    recorder.stop()
    (ROOT/'docs/pause-native-voice/render-endpoint.json').write_text(json.dumps({
        'method':'Windows default WASAPI render endpoint loopback; aggregate RMS only',
        'acoustic_human_pass':False,'other_system_audio_not_excluded':True,
        'no_audio_file_saved':True,'error':error,'samples':levels},indent=2),encoding='utf-8')
