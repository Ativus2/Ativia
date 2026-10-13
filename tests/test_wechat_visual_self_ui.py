"""Offscreen preview-widget regression; no worker, model, or real client exists."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication


_entrypoint = Path(__file__).resolve().parents[1] / "tools" / "wechat_visual.py"
_spec = importlib.util.spec_from_file_location("_wechat_visual_self_ui_entrypoint", _entrypoint)
_ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ui)


class FakeCoordinator:
    """UI-facing state only. No production coordinator or transport is created."""
    def __init__(self):
        self.action = "self-test"
        self.self_test_token = "012345ABCDEF"
        self.user_paused = False
        self.preview = None
        self.candidate = {
            "window": {"hwnd": 123, "pid": 456, "process_started": 1700000000.0,
                       "dpi": 168, "client_rect": [0, 0, 1400, 900], "client_screen": [10, 20]},
            "header": "Ativia测试群",
            "layout": {"header": [0, 0, 1400, 80], "message": [0, 80, 1400, 600],
                       "input": [0, 680, 1400, 220]},
        }
        self.state = dict(model_remaining=6, status="WAITING_USER_BINDING", capture="fake",
                          frames=0, events=0, bound=False, paused=False,
                          self_test_observations=0, self_test_preview_count=0,
                          self_test_status="WAITING_SELF_TEST", self_test_consumed=False,
                          self_test_expired=False, listener_ready=False)
        self.confirm_candidate = Mock(side_effect=self._confirm)

    @property
    def self_test_text(self):
        return ("@Ativia 自测 " + self.self_test_token + " 请用一句话回应你好。"
                if self.self_test_token else "")

    def snapshot(self):
        return dict(self.state)

    def _confirm(self, *, bottom_confirmed, self_sent_confirmed):
        # Simulate backend rejection so the UI must convey BOTH independent
        # checkbox values and clear them only after an accepted confirmation.
        if not (bottom_confirmed and self_sent_confirmed):
            raise ValueError("missing explicit confirmation")
        return True

    def set_user_paused(self, paused):
        self.user_paused = paused
        self.state["paused"] = paused


class VisualSelfTestUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Tripwires are local to this test. QWidget signals never reach native
        # input APIs, sockets, or an executable, even if the UI later regresses.
        self.no_process = patch("subprocess.Popen", side_effect=AssertionError("UI test may not spawn"))
        self.no_network = patch("socket.socket.connect", side_effect=AssertionError("UI test may not connect"))
        self.process = self.no_process.start()
        self.network = self.no_network.start()
        self.addCleanup(self.no_process.stop)
        self.addCleanup(self.no_network.stop)
        self.coordinator = FakeCoordinator()
        self.stop = Mock()
        self.window = _ui.create_window(self.coordinator, self.stop)
        self.window.refresh()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.process.assert_not_called()
        self.network.assert_not_called()

    def check_both(self):
        self.window.confirmed.setChecked(True)
        self.window.self_sent.setChecked(True)

    def assert_checks_revoked(self):
        self.assertFalse(self.window.confirmed.isChecked())
        self.assertFalse(self.window.self_sent.isChecked())

    def test_displays_exact_challenge_and_local_only_mode(self):
        self.assertEqual(self.window.challenge.toPlainText(),
                         "@Ativia 自测 012345ABCDEF 请用一句话回应你好。")
        self.assertTrue(self.window.challenge.isReadOnly())
        self.assertTrue(self.window.preview.isReadOnly())
        self.assertIn("单次自身消息自测", self.window.windowTitle())
        self.assertIn("微信点击/按键/粘贴/发送=0", self.window.status.text())
        self.assertIn("入站事件=0", self.window.status.text())
        self.assertIn("无需等待 LISTENER_READY", self.window.notice.text())
        self.coordinator.confirm_candidate.assert_not_called()

    def test_button_forwards_both_confirmations_and_clears_after_acceptance(self):
        for bottom, own in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(bottom=bottom, self_sent=own):
                self.window.confirmed.setChecked(bottom)
                self.window.self_sent.setChecked(own)
                self.window.confirm_button.click()
                self.coordinator.confirm_candidate.assert_called_with(
                    bottom_confirmed=bottom, self_sent_confirmed=own)
                if bottom and own:
                    self.assert_checks_revoked()
                    self.assertIn("已提交本次确认", self.window.notice.text())
                else:
                    self.assertIn("不满足条件", self.window.notice.text())

    def test_new_token_and_pause_revoke_previous_checks(self):
        self.check_both()
        self.coordinator.self_test_token = "ABCDEF012345"
        self.window.refresh()
        self.assert_checks_revoked()
        self.assertIn("ABCDEF012345", self.window.challenge.toPlainText())
        self.assertNotIn("012345ABCDEF", self.window.challenge.toPlainText())
        self.check_both()
        self.window.pause.click()
        self.window.refresh()
        self.assertTrue(self.coordinator.user_paused)
        self.assert_checks_revoked()
        self.assertFalse(self.window.confirm_button.isEnabled())
        self.window.pause.click()
        self.window.refresh()
        self.assert_checks_revoked()
        self.assertTrue(self.window.confirm_button.isEnabled())
        self.coordinator.confirm_candidate.assert_not_called()

    def test_window_identity_dpi_layout_and_binding_changes_revoke_checks(self):
        original = deepcopy(self.coordinator.candidate)
        changes = (
            ("process restart", lambda c: c["window"].update(process_started=1700000100.0)),
            ("dpi", lambda c: c["window"].update(dpi=192)),
            ("layout", lambda c: c["layout"].update(message=[0, 80, 1400, 580])),
            ("group header", lambda c: c.update(header="其他群")),
        )
        for name, change in changes:
            with self.subTest(change=name):
                self.coordinator.candidate = deepcopy(original)
                self.window.refresh()
                self.check_both()
                change(self.coordinator.candidate)
                self.window.refresh()
                self.assert_checks_revoked()
        self.check_both()
        self.coordinator.state["status"] = "UNBOUND"
        self.window.refresh()
        self.assert_checks_revoked()
        self.check_both()
        self.coordinator.candidate = None
        self.window.refresh()
        self.assert_checks_revoked()
        self.assertFalse(self.window.confirm_button.isEnabled())
        self.coordinator.confirm_candidate.assert_not_called()

    def test_expired_consumed_or_missing_challenge_disables_confirmation(self):
        for state_key in ("self_test_expired", "self_test_consumed"):
            with self.subTest(state=state_key):
                self.coordinator.state.update(self_test_expired=False, self_test_consumed=False)
                self.coordinator.state[state_key] = True
                self.check_both()
                self.window.refresh()
                self.assertFalse(self.window.confirm_button.isEnabled())
                self.window.confirm_button.click()
                self.coordinator.confirm_candidate.assert_not_called()
                self.assertIn("已过期" if state_key == "self_test_expired" else "不能再次触发",
                              self.window.notice.text())
        self.coordinator.state.update(self_test_expired=False, self_test_consumed=False)
        self.coordinator.self_test_token = None
        self.window.refresh()
        self.assertEqual(self.window.challenge.toPlainText(), "")
        self.assertFalse(self.window.confirm_button.isEnabled())

    def test_self_preview_label_counters_and_revocation(self):
        self.coordinator.state.update(self_test_observations=1, self_test_preview_count=1,
                                      self_test_status="SELF_TEST_PREVIEW_READY", self_test_consumed=True)
        self.coordinator.preview = {"trace_id": "fake-self-test", "reply": "你好，今天也一起聊聊吧。"}
        self.window.refresh()
        self.assertIn("不计群友入站验收", self.window.preview.toPlainText())
        self.assertIn("未发送到微信", self.window.preview.toPlainText())
        self.assertIn("你好，今天也一起聊聊吧。", self.window.preview.toPlainText())
        self.assertIn("独立自测：读取=1 | 预览=1", self.window.status.text())
        self.assertIn("入站事件=0", self.window.status.text())
        self.assertFalse(self.coordinator.state["listener_ready"])
        self.coordinator.preview = None
        self.window.refresh()
        self.assertEqual(self.window.preview.toPlainText(), "")
        self.coordinator.confirm_candidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
