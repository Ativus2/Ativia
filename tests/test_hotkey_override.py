import ctypes as C
from ctypes import wintypes as W
import threading
import unittest
from unittest.mock import Mock,patch
from PySide6.QtWidgets import QApplication
from hotkey import _AltQChord,_KeyboardData,_OverrideHook,AltQWatcher,_Filter

app=QApplication.instance() or QApplication([])

class ChordTests(unittest.TestCase):
    def test_exact_altq_triggers_once_and_consumes_matching_keyup(self):
        c=_AltQChord()
        self.assertEqual(c.process(0x51,True,False,True,False),(True,True))
        self.assertEqual(c.process(0x51,True,False,True,False),(True,False))
        self.assertEqual(c.process(0x51,False,True,False,False),(True,False))
        self.assertEqual(c.process(0x51,True,False,True,False),(True,True))
    def test_plain_q_altgr_and_other_shortcuts_are_untouched(self):
        for key,alt,other in [(0x51,False,False),(0x51,True,True),(0x41,True,False),(0x51,False,True)]:
            c=_AltQChord();self.assertEqual(c.process(key,True,False,alt,other),(False,False))
            self.assertEqual(c.process(key,False,True,alt,other),(False,False))
    def make_hook(self):
        hook=object.__new__(_OverrideHook);hook.user=Mock();hook.user.CallNextHookEx.return_value=77
        hook.user.GetAsyncKeyState.return_value=0;hook.handle=None;hook.stopping=threading.Event()
        hook.chord=_AltQChord();hook.notify=Mock();return hook
    def test_native_callback_consumes_altq_and_passes_negative_code(self):
        hook=self.make_hook();data=_KeyboardData(0x51,0,0x20,0,0);ptr=C.addressof(data)
        self.assertEqual(hook._callback(0,0x104,ptr),1);hook.notify.assert_called_once()
        self.assertEqual(hook._callback(0,0x104,ptr),1);hook.notify.assert_called_once()
        self.assertEqual(hook._callback(0,0x105,ptr),1)
        self.assertEqual(hook._callback(-1,0x104,0),77)
    def test_modifier_and_callback_failure_pass_through(self):
        hook=self.make_hook();data=_KeyboardData(0x51,0,0x20,0,0)
        hook.user.GetAsyncKeyState.return_value=-32768
        self.assertEqual(hook._callback(0,0x104,C.addressof(data)),77);hook.notify.assert_not_called()
        hook.user.GetAsyncKeyState.return_value=0;hook.notify.side_effect=RuntimeError('offline')
        self.assertEqual(hook._callback(0,0x104,C.addressof(data)),77)
    def test_stopped_hook_never_consumes_key(self):
        hook=self.make_hook();hook.stopping.set()
        self.assertEqual(hook._callback(0,0x104,0),77);hook.notify.assert_not_called()

class WatcherTests(unittest.TestCase):
    def test_conflict_uses_override_and_queued_delivery_and_cleans_up(self):
        user=Mock();user.RegisterHotKey.return_value=False
        with patch('hotkey.C.WinDLL',return_value=user),patch('hotkey.C.get_last_error',return_value=1409),patch('hotkey._OverrideHook') as factory:
            hook=factory.return_value;hook.start.return_value=True
            watcher=AltQWatcher();self.assertTrue(watcher.available);self.assertEqual(watcher.backend,'override')
            self.assertFalse(watcher._retry.isActive());self.assertEqual(watcher.error,'')
            calls=[];watcher.activated.connect(lambda:calls.append(True))
            watcher._override_activated.emit();self.assertFalse(calls);app.processEvents();self.assertEqual(calls,[True])
            watcher.stop();watcher._override_activated.emit();app.processEvents();self.assertEqual(calls,[True])
            watcher._register();self.assertEqual(user.RegisterHotKey.call_count,1)
            hook.stop.assert_called_once();user.UnregisterHotKey.assert_not_called()
    def test_normal_registration_does_not_install_hook(self):
        user=Mock();user.RegisterHotKey.return_value=True
        with patch('hotkey.C.WinDLL',return_value=user),patch('hotkey._OverrideHook') as factory:
            watcher=AltQWatcher();self.assertEqual(watcher.backend,'registered');factory.assert_not_called()
            watcher.stop();watcher.stop();user.UnregisterHotKey.assert_called_once()
    def test_override_failure_keeps_retry_and_error_and_then_recovers(self):
        user=Mock();user.RegisterHotKey.side_effect=[False,True]
        with patch('hotkey.C.WinDLL',return_value=user),patch('hotkey.C.get_last_error',return_value=1409),patch('hotkey._OverrideHook') as factory:
            hook=factory.return_value;hook.start.return_value=False;hook.error=5
            watcher=AltQWatcher();self.assertFalse(watcher.available);self.assertIn('1409',watcher.error)
            self.assertIn('5',watcher.error);self.assertTrue(watcher._retry.isActive());hook.stop.assert_called()
            watcher._register();self.assertTrue(watcher.available);self.assertFalse(watcher._retry.isActive());watcher.stop()
    def test_override_does_not_double_handle_wm_hotkey(self):
        owner=Mock();owner.available=True;owner.backend='override';owner._id=0xA771
        f=_Filter(owner);msg=W.MSG();msg.message=0x312;msg.wParam=owner._id
        self.assertEqual(f.nativeEventFilter(b'windows_generic_MSG',C.addressof(msg)),(False,0))
        owner.activated.emit.assert_not_called()

if __name__=='__main__':unittest.main()
