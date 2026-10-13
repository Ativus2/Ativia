import os,sys,tempfile,unittest
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtGui import QFontDatabase,QFont
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from experiments.desktop_pet.window import PetWindow
from translation.overlay import SubtitleOverlay
from translation.captions import Caption
from dataclasses import replace
app=QApplication.instance() or QApplication([])
QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
app.setFont(QFont('Microsoft YaHei',10))
class CloseAndOverlayTests(unittest.TestCase):
 def test_close_during_current_answer_stays_closed_then_next_reopens(self):
  with tempfile.TemporaryDirectory() as d:
   pet=PetWindow(Path(d)/'settings.json',daily=True);pet.show()
   try:
    state=dict(state='RUNNING',learning=True,thinking=True,caption='正在解释这段韩语。',instance='test')
    pet.update_state(state);app.processEvents();self.assertTrue(pet.bubble.isVisible())
    pet.bubble.grab().save(str(ROOT/'logs/latency-explanation-close.png'))
    self.assertGreater(pet.bubble.close_button.x(),pet.bubble.width()-50)
    QTest.mouseClick(pet.bubble.close_button,Qt.LeftButton)
    self.assertFalse(pet.bubble.isVisible());self.assertEqual(pet.state['caption'],state['caption'])
    pet.update_state(dict(state,caption='正在解释这段韩语。还有后续内容。'))
    self.assertFalse(pet.bubble.isVisible())
    pet.update_state(dict(state,thinking=False,caption='本次解释完成。'));self.assertFalse(pet.bubble.isVisible())
    pet.update_state(dict(state,thinking=True,caption=''));pet.update_state(dict(state,caption='下一次 Alt+Q 解释。'))
    self.assertTrue(pet.bubble.isVisible())
    QTest.keyClick(pet.bubble,Qt.Key_Escape);self.assertFalse(pet.bubble.isVisible())
   finally:pet.tray.hide();pet.bar.hide();pet.bubble.hide();pet.hide();pet.deleteLater()
 def test_formal_wins_geometry_constant_and_epoch_guard(self):
  w=SubtitleOverlay()
  try:
   w.resize(860,270);geometry=w.geometry()
   c=Caption(1,'I think this new system works',state='PARTIAL',provider='openai',
      provisional_translation='我觉得这个新系统可行……',provisional_source='I think this new system')
   w.upsert(c);app.processEvents();self.assertIn('临时',w.text.toPlainText());self.assertEqual(w.geometry(),geometry)
   w.grab().save(str(ROOT/'logs/latency-provisional-overlay.png'))
   final=replace(c,state='TRANSLATED',translation='我认为这个新系统可行。',provisional_translation='',version=2)
   w.upsert(final);w.upsert(c);self.assertEqual(w.rows[1].translation,final.translation)
   w.reset_epoch(3);w.upsert(c);self.assertFalse(w.rows)
  finally:w.hide();w.deleteLater()
if __name__=='__main__':unittest.main()

