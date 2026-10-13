import tempfile,time,unittest
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from experiments.desktop_pet.window import PetWindow
from pet_state import pet_state,PetState,bubble_preview
from companion_runtime import CompanionRuntime
from memory.ui import MemoryDialog
app=QApplication.instance() or QApplication([])

class DesktopTests(unittest.TestCase):
    def test_state_precedence_and_short_preview(self):
        for state,expected in ((dict(state='PAUSED',speaking=True),PetState.PAUSED),
            (dict(state='RUNNING',mode='SLEEP'),PetState.SLEEPING),
            (dict(state='RUNNING',thinking=True),PetState.THINKING),
            (dict(state='RUNNING',speaking=True),PetState.SPEAKING),
            (dict(state='RUNNING',mic_active=True),PetState.LISTENING)):
            self.assertEqual(pet_state(state),expected)
        text='这是一段用来检查完整聊天不会被截断的长回复。'*20
        self.assertLessEqual(len(bubble_preview(text).splitlines()),3)

    def test_pet_menus_bubble_expansion_and_explanation(self):
        with tempfile.TemporaryDirectory() as d:
            pet=PetWindow(Path(d)/'pet.json',daily=True);pet.show()
            try:
                full='这是一段长回复。'*50
                pet.update_state(dict(state='RUNNING',mode='NORMAL',caption=full,thinking=False))
                self.assertEqual(pet.state['caption'],full)
                self.assertLess(len(pet.bubble.toPlainText()),len(full))
                clicked=[];pet.open_panel.connect(lambda:clicked.append(True))
                QTest.mouseClick(pet.bubble.viewport(),Qt.LeftButton)
                self.assertEqual(clicked,[True])
                labels=[a.text() for a in pet.make_menu().actions()]
                self.assertEqual(len(labels),8);self.assertTrue(any('Memory' in s for s in labels))
                pet.update_state(dict(state='RUNNING',mode='CLASS',learning=True,caption=full+'课堂'))
                self.assertEqual(pet.bubble.toPlainText(),full+'课堂')
                self.assertFalse(pet.animation.isActive())
                QTest.keyClick(pet.bubble,Qt.Key_Escape);self.assertFalse(pet.bubble.isVisible())
            finally:pet.dispose()

    def test_memory_ui_changes_survive_restart_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            runtime=CompanionRuntime(Path(d)/'companion.json');runtime.memory.ready.wait(3)
            dialog=MemoryDialog(runtime);dialog.show()
            def flush():
                runtime.memory.submit(lambda:None).result(3);app.processEvents();dialog.refresh()
            try:
                dialog.editor.setPlainText('以后玩游戏不要主动教学');dialog.save_memory();flush()
                dialog.lists[0].setCurrentRow(0);dialog.selected(dialog.lists[0].currentItem())
                identity=dialog.identity;self.assertTrue(identity)
                self.assertTrue(dialog.remove.isEnabled());self.assertTrue(dialog.pin.isEnabled())
                QTest.mouseClick(dialog.pin,Qt.LeftButton);flush();self.assertEqual(runtime.memory.snapshot[0]['pinned'],1)
                self.assertIn('固定：是',dialog.metadata.text())
                dialog.tabs.setCurrentIndex(1);self.assertIsNone(dialog.identity);self.assertFalse(dialog.remove.isEnabled())
                dialog.tabs.setCurrentIndex(0);self.assertEqual(dialog.identity,identity)
                dialog.editor.setPlainText('以后我玩游戏不要主动给攻略');dialog.save_memory();flush()
                self.assertIn('攻略',runtime.memory.snapshot[0]['content'])
                dialog.toggle_pin();flush();self.assertEqual(runtime.memory.snapshot[0]['pinned'],0)
                dialog.change('delete');flush();self.assertEqual(runtime.memory.snapshot,())
            finally:dialog.close();runtime.close();runtime.memory.close_future.result(3)

if __name__=='__main__':unittest.main()

