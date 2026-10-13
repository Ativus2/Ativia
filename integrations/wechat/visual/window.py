"""Read-only Windows HWND discovery and geometry. No activation or style mutations."""
import ctypes
from ctypes import wintypes as W
from dataclasses import dataclass,asdict
import psutil

@dataclass(frozen=True)
class WindowSnapshot:
    hwnd:int;pid:int;process_started:float;process_name:str;exe:str;title:str;class_name:str
    window_rect:tuple;client_rect:tuple;client_screen:tuple;dpi:int;minimized:bool;foreground:bool
    @property
    def client_size(self):return (self.client_rect[2],self.client_rect[3])
    def data(self):return asdict(self)

class Win32Windows:
    def __init__(self):
        self.u=ctypes.WinDLL('user32',use_last_error=True)
        self.u.GetForegroundWindow.restype=W.HWND;self.u.IsWindow.argtypes=[W.HWND]
        self.u.IsIconic.argtypes=[W.HWND];self.u.IsWindowVisible.argtypes=[W.HWND]
        self.u.GetWindowRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
        self.u.GetClientRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
        self.u.ClientToScreen.argtypes=[W.HWND,ctypes.POINTER(W.POINT)]
        self.u.GetWindowThreadProcessId.argtypes=[W.HWND,ctypes.POINTER(W.DWORD)]
        self.u.GetWindowTextW.argtypes=[W.HWND,W.LPWSTR,ctypes.c_int]
        self.u.GetClassNameW.argtypes=[W.HWND,W.LPWSTR,ctypes.c_int]
        self.u.GetDpiForWindow.argtypes=[W.HWND]
        self.u.GetCursorPos.argtypes=[ctypes.POINTER(W.POINT)];self.u.GetCursorPos.restype=W.BOOL
        self.u.WindowFromPoint.argtypes=[W.POINT];self.u.WindowFromPoint.restype=W.HWND
        self.u.GetAncestor.argtypes=[W.HWND,W.UINT];self.u.GetAncestor.restype=W.HWND
        # Per-process only; no persistent system settings are changed.
        self.u.SetProcessDpiAwarenessContext.argtypes=[ctypes.c_void_p]
        self.u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    def pointer_over_window(self,hwnd):
        """Read the actual root hit target, without activating or changing windows."""
        point=W.POINT()
        if not self.u.GetCursorPos(ctypes.byref(point)):return True  # Uncertain: pause.
        hit=int(self.u.WindowFromPoint(point) or 0)
        if not hit:return False
        root=int(self.u.GetAncestor(hit,2) or 0)  # GA_ROOT, never GA_ROOTOWNER.
        return not root or root==int(hwnd)
    def snapshot(self,hwnd):
        if not self.u.IsWindow(hwnd):raise RuntimeError('window_closed')
        pid=W.DWORD();self.u.GetWindowThreadProcessId(hwnd,ctypes.byref(pid));p=psutil.Process(pid.value)
        wr=W.RECT();cr=W.RECT();pt=W.POINT()
        if not self.u.GetWindowRect(hwnd,ctypes.byref(wr)) or not self.u.GetClientRect(hwnd,ctypes.byref(cr)):raise RuntimeError('geometry_unavailable')
        if not self.u.ClientToScreen(hwnd,ctypes.byref(pt)):raise RuntimeError('geometry_unavailable')
        text=ctypes.create_unicode_buffer(1024);cls=ctypes.create_unicode_buffer(256)
        self.u.GetWindowTextW(hwnd,text,len(text));self.u.GetClassNameW(hwnd,cls,len(cls))
        return WindowSnapshot(int(hwnd),pid.value,p.create_time(),p.name(),p.exe(),text.value,cls.value,
            (wr.left,wr.top,wr.right-wr.left,wr.bottom-wr.top),(0,0,cr.right,cr.bottom),(pt.x,pt.y),
            int(self.u.GetDpiForWindow(hwnd)),bool(self.u.IsIconic(hwnd)),int(self.u.GetForegroundWindow() or 0)==int(hwnd))
    def discover(self,process_names=('Weixin.exe','WeChat.exe')):
        found=[];names={n.lower() for n in process_names}
        callback=ctypes.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        def visit(hwnd,_):
            if self.u.IsWindowVisible(hwnd):
                try:
                    s=self.snapshot(hwnd)
                    if s.process_name.lower() in names and s.client_size[0]>=400 and s.client_size[1]>=300:found.append(s)
                except (OSError,psutil.Error,RuntimeError):pass
            return True
        fn=callback(visit);self.u.EnumWindows(fn,0);return found

def same_target(before,after):
    return (before.hwnd,before.pid,before.process_started,before.client_screen,before.client_size,before.dpi)==(after.hwnd,after.pid,after.process_started,after.client_screen,after.client_size,after.dpi) and not after.minimized
