import tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from companion_runtime import CompanionRuntime
from assistant_pause import PauseGate
from performance.scheduler import TaskScheduler
from integrations.wechat.config import WechatConfigStore,WechatConfig
from integrations.wechat.controller import WechatController
from integrations.wechat.ui import WechatDialog
from memory.ui import MemoryDialog
from memory.memory_candidate import MemoryCandidateEvaluator
from tests_support_wechat import make_event
app=QApplication.instance() or QApplication([])
class WechatUiTests(unittest.TestCase):
    def test_disabled_controller_settings_roundtrip_and_close(self):
        from PySide6.QtWidgets import QWidget
        with tempfile.TemporaryDirectory() as d:
            runtime=CompanionRuntime(Path(d)/'companion.json');runtime.memory.ready.wait(3)
            overlay=SimpleNamespace(control=QWidget(),companion=runtime,_pause_gate=PauseGate(True),scheduler=TaskScheduler())
            controller=WechatController(overlay,WechatConfigStore(Path(d)/'wechat.json'));dialog=WechatDialog(controller)
            try:
                dialog.groups.setPlainText('Ativia测试群');dialog.owner.setText('example-owner');dialog.bot.setText('Ativia');dialog.save()
                self.assertEqual(controller.store.load().owner_names,('example-owner',));self.assertIsNone(controller.service)
                self.assertTrue(controller.store.load().dry_run);self.assertTrue(dialog.dry_run.isChecked())
                self.assertIn('DRY RUN',dialog.status.text());self.assertIn('不会向微信发送消息',dialog.status.text())
                self.assertEqual(controller.snapshot()['app_state'],'PAUSED');controller.close();self.assertTrue(controller.closed_finished())
            finally:dialog.close();runtime.close();runtime.memory.close_future.result(3)
    def test_group_memory_viewer_identity_scope_and_explicit_visibility_edit(self):
        with tempfile.TemporaryDirectory() as d:
            runtime=CompanionRuntime(Path(d)/'companion.json');runtime.memory.ready.wait(3)
            c=MemoryCandidateEvaluator().evaluate(make_event('我不吃香菜','A','g'));runtime.memory.change('save',c).result(3)
            dialog=MemoryDialog(runtime)
            try:
                dialog.refresh();item=dialog.lists[0].item(0);dialog.selected(item)
                self.assertIn('group',item.text());self.assertIn('范围：group',dialog.metadata.text())
                dialog.visibility.setCurrentIndex(dialog.visibility.findData('public'));dialog.change_visibility();dialog.pending.result(3);dialog.refresh()
                self.assertEqual(runtime.memory.snapshot[0]['visibility'],'public');self.assertEqual(runtime.memory.snapshot[0]['subject_id'],'A')
                dialog.change('delete');dialog.pending.result(3);self.assertFalse(runtime.memory.snapshot)
            finally:dialog.close();runtime.close();runtime.memory.close_future.result(3)
    def test_group_viewer_command_filters_and_deletes_via_live_cache(self):
        from PySide6.QtWidgets import QWidget
        from integrations.wechat.identity import group_id
        with tempfile.TemporaryDirectory() as d:
            runtime=CompanionRuntime(Path(d)/'companion.json');runtime.memory.ready.wait(3)
            for person,group in (('A','g'),('B','h')):runtime.memory.change('save',MemoryCandidateEvaluator().evaluate(make_event('我不吃香菜',person,group_id(group)))).result(3)
            overlay=SimpleNamespace(control=QWidget(),companion=runtime,_pause_gate=PauseGate(True),scheduler=TaskScheduler())
            controller=WechatController(overlay,WechatConfigStore(Path(d)/'wechat.json'))
            try:
                rows=controller.command(dict(action='wechat-memory',person='A',group='g'))['memories'];self.assertEqual(len(rows),1)
                controller.command(dict(action='wechat-delete',id=rows[0]['id'])).result(3)
                self.assertEqual(controller.command(dict(action='wechat-memory',person='A',group='g'))['memories'],[])
            finally:controller.close();runtime.close();runtime.memory.close_future.result(3)
if __name__=='__main__':unittest.main()
