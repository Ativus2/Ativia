"""Optional global developer shortcut. Conflict leaves the menu usable."""
import os,ctypes as C
from ctypes import wintypes as W
from PySide6.QtCore import QAbstractNativeEventFilter,QCoreApplication,QTimer
class PerformanceShortcut(QAbstractNativeEventFilter):
    def __init__(self,callback):
        super().__init__();self.callback=callback;self.identity=0xA74F;self.registered=False
        if os.name=='nt':
            self.user=C.WinDLL('user32',use_last_error=True)
            self.user.RegisterHotKey.argtypes=[W.HWND,C.c_int,W.UINT,W.UINT]
            self.user.UnregisterHotKey.argtypes=[W.HWND,C.c_int]
            self.registered=bool(self.user.RegisterHotKey(None,self.identity,0x4003,0x7B))
            if self.registered:QCoreApplication.instance().installNativeEventFilter(self)
    def nativeEventFilter(self,kind,message):
        if self.registered:
            msg=W.MSG.from_address(int(message))
            if msg.message==0x312 and msg.wParam==self.identity:
                QTimer.singleShot(0,self.callback);return True,0
        return False,0
    def close(self):
        if self.registered:
            self.user.UnregisterHotKey(None,self.identity)
            QCoreApplication.instance().removeNativeEventFilter(self);self.registered=False
