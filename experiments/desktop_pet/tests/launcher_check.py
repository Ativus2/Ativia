"""Check actual start/reuse/stop entrypoints at zero paid-session budget."""
import asyncio,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from aiohttp import ClientSession,ClientTimeout
from experiments.desktop_pet.owned_process import OwnedProcess
python=ROOT/'experiments/realtime_prototype/.venv/Scripts/python.exe'
main=ROOT/'experiments/desktop_pet/main.py';runtime=ROOT/'experiments/desktop_pet/logs/runtime.json'
report={'paid_calls':0,'checks':[]}
launcher=OwnedProcess([str(python),str(main),'--session-limit','0','--probe'],cwd=str(ROOT),
    log=ROOT/'experiments/desktop_pet/logs/launcher-check.log')
async def run():
    for _ in range(100):
        if runtime.exists():break
        await asyncio.sleep(.1)
    config=json.loads(runtime.read_text())
    await asyncio.sleep(1)
    duplicate=await asyncio.create_subprocess_exec(str(python),str(main),'--probe','--session-limit','0',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    await duplicate.communicate()
    assert duplicate.returncode==2,'duplicate launcher was admitted'
    report['checks'].append('second launcher rejected before spawning another service')
    panel=await asyncio.create_subprocess_exec(str(python),str(main),'--open-panel','--probe',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    await panel.communicate();assert panel.returncode==0
    async with ClientSession(timeout=ClientTimeout(total=10)) as client:
        async with client.ws_connect(f"http://127.0.0.1:{config['port']}/bridge") as ws:
            await ws.send_json(dict(type='hello',role='control',key=config['key']))
            for _ in range(50):
                data=await asyncio.wait_for(ws.receive_json(),3)
                if data.get('type')=='state' and data.get('page_connected'):
                    assert data['mic_active'] is False and data['cloud_connected'] is False;break
            else:raise AssertionError('panel did not attach')
    report['checks'].append('debug entry reuses launcher and opens production page with microphone off')
    stop=await asyncio.create_subprocess_exec(str(python),str(ROOT/'experiments/desktop_pet/stop.py'),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await stop.communicate();assert stop.returncode==0,err.decode(errors='replace')
    assert not runtime.exists()
    for _ in range(30):
        if not launcher.running():break
        await asyncio.sleep(.1)
    assert not launcher.running(),'launcher still running after stop acknowledgement'
    report['checks'].append('stop entry ends same session and removes owned launcher/service/browser')
try:asyncio.run(run())
except Exception as error:report['error']=type(error).__name__+': '+str(error)
finally:launcher.close()
(ROOT/'docs/desktop-pet-trial/launcher-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
if report.get('error'):raise SystemExit(1)
