import tempfile
import unittest
from pathlib import Path
from PySide6.QtCore import QPoint,QRect,QSize,Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from experiments.desktop_pet.window import PetWindow,clamp_position

app=QApplication.instance() or QApplication([])
class PetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'settings.json'
        self.pet=PetWindow(self.path);self.pet.show();QTest.qWait(20);self.commands=[]
        self.pet.command.connect(lambda c,x:self.commands.append(c))
    def tearDown(self):self.pet.dispose();self.pet.deleteLater();QTest.qWait(10);self.temp.cleanup()
    def test_transparent_small_mask_and_passive_body(self):
        self.assertTrue(self.pet.testAttribute(Qt.WA_TranslucentBackground));self.assertLessEqual(self.pet.width(),180)
        self.assertFalse(self.pet.mask().contains(QPoint(0,0)))
        QTest.mouseClick(self.pet,Qt.LeftButton,pos=QPoint(80,80));self.assertEqual(self.commands,[])
        self.assertTrue(self.pet.tray.contextMenu().isWindow())
        self.assertFalse(self.pet.tray.contextMenu().isVisible())
    def test_negative_monitors_and_offscreen_restore(self):
        result=clamp_position(QPoint(-9999,9999),QSize(160,188),[QRect(-1920,0,1920,1080),QRect(0,0,1920,1080)])
        self.assertTrue(QRect(-1920,0,1920,1080).contains(QRect(result,QSize(160,188))))
    def test_hover_bar_entry_does_not_start_capture(self):
        QTest.mouseMove(self.pet,QPoint(80,80));QTest.qWait(40)
        self.pet.bar.show();QTest.mouseMove(self.pet.bar,QPoint(30,15));QTest.qWait(350)
        self.assertTrue(self.pet.bar.isVisible());self.assertEqual(self.commands,[])
    def test_hide_keeps_tray_without_pause_and_stops_animation(self):
        self.pet.update_state(dict(state='RUNNING',speaking=True,mic_active=True,caption='测试字幕',instance='test'))
        self.pet.hide_pet();self.assertTrue(self.pet.tray.isVisible());self.assertFalse(self.pet.animation.isActive())
        self.assertFalse(self.commands);self.pet.reveal();self.assertTrue(self.pet.isVisible())
    def test_size_and_position_persist(self):
        self.pet.set_size(180);self.pet.move(100,100);self.pet.save()
        import json
        saved=json.loads(self.path.read_text());self.assertEqual(saved['size'],180);self.assertEqual(saved['position'],[100,100])
    def test_caption_is_not_speaking_and_pause_requires_backend_state(self):
        self.pet.update_state(dict(state='RUNNING',mic_active=True,caption='只有字幕',speaking=False,instance='test'))
        self.assertEqual(self.pet.display_state(),'正在收音')
        self.pet.primary();self.assertEqual(self.commands,['pause']);self.assertEqual(self.pet.display_state(),'正在收音')
        self.pet.update_state(dict(state='PAUSING',mic_active=True,instance='test'));self.assertEqual(self.pet.display_state(),'正在暂停')
        self.pet.update_state(dict(state='PAUSED',mic_active=False,instance='test'));self.assertEqual(self.pet.display_state(),'已完全暂停')
    def test_pause_stays_available_while_connecting_or_resuming(self):
        for state in ('CONNECTING','RESUMING'):
            self.pet.update_state(dict(state=state,mic_active=True,instance='test'))
            self.assertTrue(self.pet.bar.primary.isEnabled());self.pet.bar.primary.click()
        self.assertEqual(self.commands,['pause','pause'])
    def test_drag_moves_window_without_issuing_any_command(self):
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent,QPointF
        begin=self.pet.pos();local=QPoint(70,70);global_pos=self.pet.mapToGlobal(local)
        QTest.mousePress(self.pet,Qt.LeftButton,pos=local)
        event=QMouseEvent(QEvent.MouseMove,QPointF(local+QPoint(30,20)),QPointF(global_pos+QPoint(30,20)),Qt.NoButton,Qt.LeftButton,Qt.NoModifier)
        QApplication.sendEvent(self.pet,event);QTest.mouseRelease(self.pet,Qt.LeftButton,pos=local+QPoint(30,20))
        self.assertNotEqual(self.pet.pos(),begin);self.assertFalse(self.commands)
