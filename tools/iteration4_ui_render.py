import os,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtCore import Qt
from types import SimpleNamespace
from experiments.desktop_pet.window import PetWindow
from memory.ui import MemoryDialog
from companion_runtime import CompanionRuntime
from performance.panel import PerformancePanel
from performance.scheduler import TaskScheduler
from translation.diagnostics import DiagnosticBuffer
app=QApplication([])
for name in ('msyh.ttc','segoeui.ttf','malgun.ttf'):
    path=Path('C:/Windows/Fonts')/name
    if path.exists():QFontDatabase.addApplicationFont(str(path))
app.setFont(QFont('Microsoft YaHei',10))
out=ROOT/'docs/iteration4/benchmark'
with tempfile.TemporaryDirectory() as d:
    r=CompanionRuntime(Path(d)/'companion.json');r.memory.ready.wait(3)
    r.remember_input('以后玩熟悉的游戏不要主动教我');r.memory.submit(lambda:None).result(3)
    pet=PetWindow(Path(d)/'pet.json',daily=True);pet.show()
    pet.update_state(dict(state='RUNNING',mode='NORMAL',caption='终于忙完了。先歇一会儿吧，接下来的事慢慢来。',thinking=False))
    pet.bar.show();pet.position_popups();app.processEvents()
    pet.grab().save(str(out/'pet.png'));pet.bar.grab().save(str(out/'hover.png'));pet.bubble.grab().save(str(out/'short-bubble.png'))
    menu=pet.make_menu();menu.show();app.processEvents();menu.grab().save(str(out/'menu.png'));menu.hide()
    memory=MemoryDialog(r);memory.show();app.processEvents();memory.lists[0].setCurrentRow(0);app.processEvents()
    memory.grab().save(str(out/'memory.png'));memory.hide()
    owner=SimpleNamespace(scheduler=TaskScheduler(),translation=SimpleNamespace(diagnostics=DiagnosticBuffer(),worker=None))
    perf=PerformancePanel(owner);perf.show();app.processEvents();perf.refresh();app.processEvents()
    perf.grab().save(str(out/'performance.png'));perf.hide();perf.pool.shutdown(wait=True,cancel_futures=True)
    pet.update_state(dict(state='RUNNING',mode='CLASS',learning=True,caption='작업을 마쳤어요.\n中文翻译：工作完成了。',thinking=False))
    app.processEvents();pet.bubble.grab().save(str(out/'explanation.png'))
    from controls import ControlBar
    chat=ControlBar(text_only=True);chat.set_entertainment_result('辛苦了。终于能把今天放下了。');chat.show();app.processEvents()
    chat.grab().save(str(out/'conversation.png'));chat.close()
    pet.dispose();r.close();r.memory.close_future.result(3)
print('UI render complete; deterministic fixture text, fonts explicitly loaded for offscreen Qt.')

