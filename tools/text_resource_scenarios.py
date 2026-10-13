"""Offline real Qt/backend resource run. Fixtures are NOT real cloud latency evidence."""
import argparse, json, os, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
parser=argparse.ArgumentParser();parser.add_argument('--legacy',action='store_true');parser.add_argument('--output',required=True)
parser.add_argument('--seconds',type=float,default=60)
args=parser.parse_args();started=time.monotonic()
if Path(args.output).exists():parser.error('Output already exists; use a new path to preserve previous measurements.')
temp=tempfile.TemporaryDirectory();os.environ['LOCALAPPDATA']=temp.name
os.environ['ATIVIA_DEVICE_DIR']=str(Path(temp.name)/'device')
os.environ['ATIVIA_TTS_EVENT_LOG']=str(Path(temp.name)/'tts.jsonl')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from runtime_settings import SettingsStore,RuntimeSettings
SettingsStore().save(RuntimeSettings(mic_enabled=False,system_audio_enabled=False))
from unittest.mock import patch
# Hard network prohibition; no actual input devices are started by this scenario.
try: import httpx2 as httpx
except ImportError: import httpx
blocked=patch.object(httpx.Client,'send',side_effect=AssertionError('offline resource run: network forbidden'));blocked.start()
from tools.windows_resources import sample_tree,summarize
from daily_desktop import DailyDesktop
import daily_desktop
from functools import partial
daily_desktop.PetWindow=partial(daily_desktop.PetWindow,settings_path=Path(temp.name)/'pet-settings.json')
original=DailyDesktop.__init__;records=[];latencies=[];desktop=None;index=0;rows=[];sampler=None
stages=['cold_pet','open_chat_fixture','image_encoding_fixture','paused','repeated_operations']
def initialize(self,*a,**kw):
    global desktop
    original(self,*a,**kw);desktop=self
    latencies.append(dict(kind='startup_including_harness_imports',ms=(time.monotonic()-started)*1000))
    QTimer.singleShot(250,start_stage)
DailyDesktop.__init__=initialize
def start_stage():
    global index,rows,sampler
    o=desktop.overlay;control=o.control
    if index==len(stages):
        data=dict(legacy=args.legacy,offline=True,cloud_requests=0,real_text_first_token_ms=None,
            timing=latencies,stages=records,modules=[m for m in sys.modules if m.split('.')[0] in
                ('audio','soundcard','sounddevice','numpy','torch','speech_activity','tts_pipeline','gpu_monitor','voice_pipeline','openai','httpx')])
        out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(data,indent=2),encoding='utf-8')
        print(json.dumps(dict(output=str(out),summary=[dict(stage=r['stage'],**r['summary']) for r in records])),flush=True)
        print('requesting application shutdown',flush=True)
        o._quit();return
    name=stages[index];began=time.monotonic()
    if name=='cold_pet':
        if args.legacy:control._speak=False
        o._resume_all();control.hide()
    elif name=='open_chat_fixture':
        desktop.open_chat()
        for n in range(8):control.append_message('你' if n%2==0 else 'Ativia',f'离线界面资源样本 {n}：用于检查聊天渲染，不代表真实模型回答。')
    elif name=='image_encoding_fixture':
        from PySide6.QtGui import QImage,QColor
        from screenshot import qimage_to_jpeg
        image=QImage(2560,1440,QImage.Format_RGB32);image.fill(QColor('white'))
        encoded=qimage_to_jpeg(image);del image,encoded
    elif name=='paused':o._pause_all()
    elif name=='repeated_operations':
        for n in range(30):
            desktop.open_chat();control.close();o._resume_all();QTest.qWait(10)
            o._stop_current_reply();o._pause_all();QTest.qWait(10)
        desktop.open_chat()
    QTest.qWait(200)
    latencies.append(dict(kind=name+'_setup',ms=(time.monotonic()-began)*1000))
    rows=[sample_tree(os.getpid())]
    sampler=QTimer();sampler.setInterval(2000)
    def tick():
        global index
        rows.append(sample_tree(os.getpid()))
        if rows[-1]['at']-rows[0]['at']>=args.seconds:
            sampler.stop()
            records.append(dict(stage=name,summary=summarize(rows),samples=rows))
            print(name+' measured',flush=True);index+=1;QTimer.singleShot(0,start_stage)
    sampler.timeout.connect(tick);sampler.start()
sys.argv=[str(ROOT/'main.py')]+([] if args.legacy else ['--text-only'])
import main
try:main.main()
finally:
    import logging
    print('application loop exited; closing test log handlers',flush=True)
    blocked.stop();logging.shutdown();temp.cleanup()
    print('scenario cleanup complete',flush=True)
