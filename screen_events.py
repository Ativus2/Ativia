"""Cheap visual-change and executable metadata gate, before any Vision request."""
import os
from pathlib import Path
from functools import lru_cache
from time import monotonic
from PySide6.QtCore import QThread,Signal
from personality.engine import MODES

def foreground_application():
    if os.name!='nt':return {'application':'','window':0,'owned':False}
    import ctypes as C
    from ctypes import wintypes as W
    u=C.WinDLL('user32');k=C.WinDLL('kernel32')
    u.GetForegroundWindow.restype=W.HWND
    u.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
    k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
    k.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
    k.CloseHandle.argtypes=[W.HANDLE]
    window=u.GetForegroundWindow();pid=W.DWORD();u.GetWindowThreadProcessId(window,C.byref(pid))
    handle=k.OpenProcess(0x1000,False,pid.value)
    name=''
    if handle:
        try:
            buf=C.create_unicode_buffer(1024);size=W.DWORD(1024)
            if k.QueryFullProcessImageNameW(handle,0,buf,C.byref(size)):name=Path(buf.value).name.lower()
        finally:k.CloseHandle(handle)
    return dict(application=name,window=int(window or 0),owned=pid.value==os.getpid())

def classify_change(previous_hash,current_hash,previous_meta,meta,mode):
    if mode in ('CLASS','FOCUS','SLEEP'):return 'none'
    if meta.get('owned'):return 'none'
    if previous_hash is None:return 'window_changed'
    distance=(previous_hash^current_hash).bit_count()
    if distance<=6:return 'none'
    if previous_meta.get('window')!=meta.get('window'):return 'window_changed'
    return 'large_visual_change' if distance>=12 else 'none'

class CaptureProbe(QThread):
    ready=Signal(object)
    def __init__(self,capture,hasher):
        super().__init__();self.capture=capture;self.hasher=hasher
    def run(self):
        try:
            image=self.capture()
            if self.isInterruptionRequested():return
            # Pixels/hash are cheap and stay local; metadata contains no window titles.
            image_hash=self.hasher(image);metadata=foreground_application()
            self.ready.emit(dict(image=image,image_hash=image_hash,metadata=metadata))
        except Exception as exc:self.ready.emit(dict(error=type(exc).__name__))
