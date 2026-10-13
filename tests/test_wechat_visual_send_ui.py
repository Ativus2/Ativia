"""Offscreen send controls with fake coordinator; no process or native input."""
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QMessageBox
import test_wechat_visual_self_ui as fixture


class SendUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.c=fixture.FakeCoordinator();self.c.action='run';self.c.send_context=object();self.c.listener_ready=True
        self.c.enable_send=Mock();self.c.disable_send=Mock();self.c.send_test_message=Mock()
        self.c.state.update(listener_ready=True,model_remaining=None,send=dict(enabled=False,state='SEND_DISABLED'))
        self.window=fixture._ui.create_window(self.c,Mock());self.window.show();self.window.refresh()
    def tearDown(self):self.window.close();self.window.deleteLater();self.app.processEvents()
    def test_default_off_and_manual_test_disabled(self):
        self.assertIn('OFF',self.window.send_status.text());self.assertFalse(self.window.send_test.isEnabled())
        self.c.send_test_message.assert_not_called();self.c.enable_send.assert_not_called()
    def test_cancel_does_not_enable(self):
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Cancel):self.window.enable_send()
        self.c.enable_send.assert_not_called()
    def test_confirmation_applies_only_to_current_binding(self):
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes):self.window.enable_send()
        self.c.enable_send.assert_called_once_with(confirmed=True)
        self.c.enable_send.reset_mock()
        def changed(*args):self.c.send_context=object();return QMessageBox.Yes
        with patch.object(QMessageBox,'question',side_effect=changed):self.window.enable_send()
        self.c.enable_send.assert_not_called()
    def test_manual_send_uses_controller_once_then_disables_button_until_result(self):
        self.c.state['send']=dict(enabled=True,state='SEND_ARMED',queue_size=0)
        self.window.refresh();self.window.send_text.setPlainText('用户选定的测试文字')
        self.window.send_test.click()
        self.c.send_test_message.assert_called_once_with('用户选定的测试文字')
        self.assertFalse(self.window.send_test.isEnabled())
    def test_disable_keeps_ui_and_pause_stop_independent(self):
        self.c.state['send']=dict(enabled=True,state='SEND_ARMED',queue_size=0)
        self.window.refresh();self.window.send_disable.click();self.c.disable_send.assert_called_once()
        self.assertTrue(self.window.isVisible());self.assertFalse(self.c.user_paused)
    def test_queue_or_active_stage_disables_manual_double_submit(self):
        for send in [dict(enabled=True,queue_size=1),dict(enabled=True,queue_size=0,active_stage='WRITE')]:
            self.c.state['send']=send;self.window.refresh();self.assertFalse(self.window.send_test.isEnabled())


if __name__=='__main__':unittest.main()
