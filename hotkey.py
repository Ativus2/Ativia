"""Alt+Q registration with a narrowly scoped, user-requested conflict override.

Only Alt+Q is consumed. No text, other shortcuts or keyboard history is stored.
The low-level hook has its own message thread so Qt rendering/network work cannot
block input dispatch. It is removed when the watcher stops.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import threading
from PySide6.QtCore import QObject,QAbstractNativeEventFilter,QCoreApplication,Signal,QTimer,Qt


class _Filter(QAbstractNativeEventFilter):
    def __init__(self,owner):super().__init__();self.owner=owner
    def nativeEventFilter(self,event_type,message):
        kind=event_type.encode() if isinstance(event_type,str) else bytes(event_type)
        if self.owner.available and getattr(self.owner,'backend','registered')=='registered' and kind in (b'windows_generic_MSG',b'windows_dispatcher_MSG'):
            msg=W.MSG.from_address(int(message))
            if msg.message==0x0312 and msg.wParam==self.owner._id:
                QTimer.singleShot(0,self.owner.activated.emit);return True,0
        return False,0


class _KeyboardData(C.Structure):
    _fields_=[('vkCode',W.DWORD),('scanCode',W.DWORD),('flags',W.DWORD),
              ('time',W.DWORD),('dwExtraInfo',C.c_size_t)]


class _AltQChord:
    """Latch Q until key-up: no repeats, no Ctrl/Shift/Win or AltGr interception."""
    def __init__(self):self.held=False
    def process(self,key,down,up,alt,other_modifier):
        if key!=0x51:return False,False
        if up:
            consumed=self.held;self.held=False;return consumed,False
        if not down:return False,False
        if self.held:return True,False
        if alt and not other_modifier:
            self.held=True;return True,True
        return False,False


class _OverrideHook:
    def __init__(self,notify):
        self.notify=notify;self.ready=threading.Event();self.stopping=threading.Event()
        self.thread=None;self.thread_id=0;self.handle=None;self.error=0
        self._callback_ref=None;self.chord=_AltQChord()
        self.user=C.WinDLL('user32',use_last_error=True)
        self.kernel=C.WinDLL('kernel32',use_last_error=True)
        self._callback_type=C.WINFUNCTYPE(C.c_ssize_t,C.c_int,W.WPARAM,W.LPARAM)
        self.user.SetWindowsHookExW.argtypes=[C.c_int,self._callback_type,W.HINSTANCE,W.DWORD]
        self.user.SetWindowsHookExW.restype=W.HHOOK
        self.user.UnhookWindowsHookEx.argtypes=[W.HHOOK];self.user.UnhookWindowsHookEx.restype=W.BOOL
        self.user.CallNextHookEx.argtypes=[W.HHOOK,C.c_int,W.WPARAM,W.LPARAM]
        self.user.CallNextHookEx.restype=C.c_ssize_t
        self.user.GetAsyncKeyState.argtypes=[C.c_int];self.user.GetAsyncKeyState.restype=C.c_short
        self.user.GetMessageW.argtypes=[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT];self.user.GetMessageW.restype=C.c_int
        self.user.PeekMessageW.argtypes=[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT,W.UINT]
        self.user.PostThreadMessageW.argtypes=[W.DWORD,W.UINT,W.WPARAM,W.LPARAM]
        self.kernel.GetCurrentThreadId.restype=W.DWORD
        self.kernel.GetModuleHandleW.argtypes=[W.LPCWSTR];self.kernel.GetModuleHandleW.restype=W.HMODULE

    def start(self):
        self.thread=threading.Thread(target=self._run,name='ativia-altq-override',daemon=True)
        self.thread.start()
        if not self.ready.wait(2):self.error=1460;self.stop();return False
        return bool(self.handle)

    def _callback(self,code,event,pointer):
        if code>=0 and not self.stopping.is_set():
            try:
                data=_KeyboardData.from_address(pointer)
                if data.vkCode==0x51:
                    other=any(self.user.GetAsyncKeyState(k)&0x8000 for k in (0x10,0x11,0x5B,0x5C))
                    consume,trigger=self.chord.process(data.vkCode,event in (0x100,0x104),
                        event in (0x101,0x105),bool(data.flags&0x20),other)
                    if trigger:self.notify()
                    if consume:return 1
            except Exception:
                # Fail open for input; exceptions must not escape a native callback.
                self.chord.held=False
        return self.user.CallNextHookEx(self.handle,code,event,pointer)

    def _run(self):
        try:
            self.thread_id=self.kernel.GetCurrentThreadId()
            msg=W.MSG();self.user.PeekMessageW(C.byref(msg),None,0,0,0)
            self._callback_ref=self._callback_type(self._callback)
            self.handle=self.user.SetWindowsHookExW(13,self._callback_ref,self.kernel.GetModuleHandleW(None),0)
            if not self.handle:self.error=C.get_last_error()
            self.ready.set()
            if not self.handle:return
            while not self.stopping.is_set():
                if self.user.GetMessageW(C.byref(msg),None,0,0)<=0:break
        finally:
            if self.handle:self.user.UnhookWindowsHookEx(self.handle);self.handle=None
            self.ready.set()

    def stop(self):
        self.stopping.set()
        if self.thread_id:self.user.PostThreadMessageW(self.thread_id,0x12,0,0)
        if self.thread and self.thread is not threading.current_thread():self.thread.join(2)


class AltQWatcher(QObject):
    activated=Signal()
    status_changed=Signal(bool,str)
    _override_activated=Signal()
    def __init__(self,parent=None):
        super().__init__(parent)
        self.available=False;self.error='';self.backend='unavailable';self._filter=None;self._id=0xA771
        self._override=None;self._stopped=False;self.activation_count=0
        self._override_activated.connect(self._deliver_override,Qt.QueuedConnection)
        self.activated.connect(self._count_activation)
        if os.name!='nt':self.error='当前系统未注册全局 Alt+Q，请使用框选按钮。';return
        self._user=C.WinDLL('user32',use_last_error=True)
        self._user.RegisterHotKey.argtypes=[W.HWND,C.c_int,W.UINT,W.UINT]
        self._user.RegisterHotKey.restype=W.BOOL
        self._user.UnregisterHotKey.argtypes=[W.HWND,C.c_int]
        self._user.UnregisterHotKey.restype=W.BOOL
        self._filter=_Filter(self);QCoreApplication.instance().installNativeEventFilter(self._filter)
        self._retry=QTimer(self);self._retry.setInterval(3000);self._retry.timeout.connect(self._register)
        self._register()
        QCoreApplication.instance().aboutToQuit.connect(self.stop)

    def _count_activation(self):self.activation_count+=1
    def _deliver_override(self):
        if not self._stopped and self.available and self.backend=='override':self.activated.emit()

    def _register(self):
        if self.available or self._stopped:return
        previous=self.error
        if self._user.RegisterHotKey(None,self._id,0x0001|0x4000,0x51):
            self.backend='registered';self.available=True;self.error='';self._retry.stop()
            self.status_changed.emit(True,'Alt+Q 已启用');return
        code=C.get_last_error()
        if code==1409:
            candidate=_OverrideHook(self._override_activated.emit)
            if candidate.start():
                self._override=candidate;self.backend='override';self.available=True;self.error=''
                self._retry.stop();self.status_changed.emit(True,'Alt+Q 已由 Ativia 优先接管（退出后释放）');return
            candidate.stop()
            self.error=f'Alt+Q 被占用（Windows 1409），优先接管失败（Windows {candidate.error}）。请使用框选按钮。'
        else:
            self.error=f'Alt+Q 注册失败（Windows {code}），请使用框选按钮。'
        self._retry.start()
        if previous!=self.error:self.status_changed.emit(False,self.error)

    def snapshot(self):
        return dict(available=self.available,backend=self.backend,error=self.error,
                    activations=self.activation_count,
                    hook_thread_alive=bool(self._override and self._override.thread and self._override.thread.is_alive()))

    def stop(self):
        self._stopped=True
        if hasattr(self,'_retry'):self._retry.stop()
        if self.available and self.backend=='registered':self._user.UnregisterHotKey(None,self._id)
        self.available=False;self.backend='unavailable'
        if self._override:self._override.stop();self._override=None
        if self._filter:
            QCoreApplication.instance().removeNativeEventFilter(self._filter);self._filter=None
