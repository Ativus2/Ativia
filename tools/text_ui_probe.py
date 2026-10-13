"""Local Qt/entry/region verification. Captures only this test's own windows, no APIs."""
import argparse,os,sys,json,tempfile,time
from datetime import datetime
from pathlib import Path
from functools import partial
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
parser=argparse.ArgumentParser();parser.add_argument('--output-dir')
args=parser.parse_args()
out=Path(args.output_dir) if args.output_dir else ROOT/'local_tts/logs'/('ui-probe-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
out.mkdir(parents=True,exist_ok=False)
temp=tempfile.TemporaryDirectory();os.environ['ATIVIA_DEVICE_DIR']=str(Path(temp.name)/'device')
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QLabel
from PySide6.QtGui import QImage
import daily_desktop
daily_desktop.PetWindow=partial(daily_desktop.PetWindow,settings_path=Path(temp.name)/'pet.json')
original=daily_desktop.DailyDesktop.__init__;began=time.monotonic()
def initialize(self,*a,**kw):
    original(self,*a,**kw)
    self.overlay.ui_metrics.append(dict(kind='entry_to_ready_ui',ms=(time.monotonic()-began)*1000))
    def show():
        self.open_chat();QTimer.singleShot(250,capture_ui)
    def capture_ui():
        assert self.control.grab().save(str(out/'text-chat.png'))
        assert self.pet.grab().save(str(out/'text-pet.png'))
        self.overlay._pause_all();QTimer.singleShot(200,capture_paused)
    def capture_paused():
        assert self.control.grab().save(str(out/'text-paused.png'))
        self.control.hide();self.pet.hide_pet()
        self.calibration=QLabel('Ativia region capture check\nABC 123 / 中文小字测试\nOnly this window is captured.')
        self.calibration.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.calibration.setStyleSheet('background:white;color:black;font:12px "Microsoft YaHei";padding:12px;')
        self.calibration.resize(400,200);self.calibration.move(120,120);self.calibration.show()
        QTimer.singleShot(250,capture_region)
    def capture_region():
        from screenshot import capture_selected_jpeg
        rect=self.calibration.geometry();data=capture_selected_jpeg(rect);image=QImage.fromData(data)
        scale=self.calibration.screen().devicePixelRatio()
        assert (image.width(),image.height())==(round(rect.width()*scale),round(rect.height()*scale))
        image.save(str(out/'selected-region.png'))
        record=self.overlay.snapshot();record['region_dimensions']=[image.width(),image.height()]
        record['region_bytes']=len(data);record['paid_requests']=0
        (out/'ui-entry-validation.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        self.calibration.close();self.overlay._quit()
    QTimer.singleShot(0,show)
daily_desktop.DailyDesktop.__init__=initialize
sys.argv=[str(ROOT/'main.py'),'--text-only']
import main
try:main.main()
finally:temp.cleanup()
