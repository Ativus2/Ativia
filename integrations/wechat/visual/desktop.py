"""Read-only interactive-desktop admission; never unlocks or switches desktops."""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes as W
import os


@dataclass(frozen=True)
class DesktopStatus:
    available: bool
    reason: str
    session_id: int | None = None


def classify_desktop(session_id, connection_state, input_name, thread_name):
    if session_id <= 0 or connection_state != 0:  # WTSActive only.
        return DesktopStatus(False, 'session_unavailable', session_id)
    if input_name.casefold() != 'default' or thread_name.casefold() != 'default':
        return DesktopStatus(False, 'desktop_unavailable', session_id)
    return DesktopStatus(True, 'desktop_available', session_id)


def _native_state():
    if os.name != 'nt':
        raise OSError('windows_required')
    u=ctypes.WinDLL('user32',use_last_error=True)
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    w=ctypes.WinDLL('wtsapi32',use_last_error=True)
    k.ProcessIdToSessionId.argtypes=[W.DWORD,ctypes.POINTER(W.DWORD)]
    k.ProcessIdToSessionId.restype=W.BOOL
    k.GetCurrentThreadId.restype=W.DWORD
    u.OpenInputDesktop.argtypes=[W.DWORD,W.BOOL,W.DWORD];u.OpenInputDesktop.restype=W.HANDLE
    u.CloseDesktop.argtypes=[W.HANDLE];u.CloseDesktop.restype=W.BOOL
    u.GetThreadDesktop.argtypes=[W.DWORD];u.GetThreadDesktop.restype=W.HANDLE
    u.GetUserObjectInformationW.argtypes=[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD)]
    u.GetUserObjectInformationW.restype=W.BOOL
    w.WTSQuerySessionInformationW.argtypes=[W.HANDLE,W.DWORD,ctypes.c_int,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(W.DWORD)]
    w.WTSQuerySessionInformationW.restype=W.BOOL
    w.WTSFreeMemory.argtypes=[ctypes.c_void_p]
    session=W.DWORD()
    if not k.ProcessIdToSessionId(os.getpid(),ctypes.byref(session)):
        raise OSError('session_unknown')
    data=ctypes.c_void_p();size=W.DWORD()
    if not w.WTSQuerySessionInformationW(None,session.value,8,ctypes.byref(data),ctypes.byref(size)):
        raise OSError('session_state_unknown')
    try:
        if not data or size.value<ctypes.sizeof(ctypes.c_int):raise OSError('session_state_invalid')
        connected=ctypes.cast(data,ctypes.POINTER(ctypes.c_int)).contents.value
    finally:w.WTSFreeMemory(data)
    handle=u.OpenInputDesktop(0,False,0x0001)  # DESKTOP_READOBJECTS, no switching/access changes.
    if not handle:raise OSError('input_desktop_inaccessible')
    def name(target):
        buf=ctypes.create_unicode_buffer(256);needed=W.DWORD()
        if not target or not u.GetUserObjectInformationW(target,2,buf,ctypes.sizeof(buf),ctypes.byref(needed)):
            raise OSError('desktop_name_unknown')
        return buf.value
    try:return session.value,connected,name(handle),name(u.GetThreadDesktop(k.GetCurrentThreadId()))
    finally:u.CloseDesktop(handle)


def probe_desktop():
    try:return classify_desktop(*_native_state())
    except Exception:return DesktopStatus(False,'desktop_state_unknown')
