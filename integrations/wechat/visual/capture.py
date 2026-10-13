"""Input-free capture providers; controller instances never leave this module."""
import time,ctypes
from ctypes import wintypes as W
from PIL import Image,ImageStat
from .window import Win32Windows,same_target

class CaptureUnavailable(RuntimeError):pass

def wait_job(job,timeout=5):
    end=time.monotonic()+timeout
    while not job.done:
        if time.monotonic()>end:raise CaptureUnavailable('capture_job_timeout')
        time.sleep(.01)
    if not job.succeeded:raise CaptureUnavailable('capture_job_failed')
    return job

class WindowCaptureProvider:
    """Only safe explicit backends. All, pseudo-minimize and auto-focus are rejected."""
    BACKENDS=('maa_gdi','win32_printwindow','foreground_crop')
    def __init__(self,hwnd,backend='maa_gdi'):
        if backend not in self.BACKENDS:raise ValueError('unsafe_or_unknown_backend')
        self.windows=Win32Windows();self.identity=self.windows.snapshot(hwnd);self.backend=backend;self._controller=None
        if self.identity.minimized:raise CaptureUnavailable('minimized_pause')
        if backend=='maa_gdi':
            from maa.controller import Win32Controller
            from maa.define import MaaWin32ScreencapMethodEnum as S,MaaWin32InputMethodEnum as I,LoggingLevelEnum
            from maa.tasker import Tasker
            self._privacy_tasker=Tasker()
            # The pinned SDK returns bool for each setter; failure must not enable
            # image/debug persistence or stdout pollution in an input-free worker.
            if not all((Tasker.set_log_dir(''),Tasker.set_save_draw(False),Tasker.set_save_on_error(False),
                        Tasker.set_debug_mode(False),Tasker.set_stdout_level(LoggingLevelEnum.Off))):
                raise CaptureUnavailable('capture_privacy_options_rejected')
            self._controller=Win32Controller(hwnd,screencap_method=S.GDI,mouse_method=I.Null,keyboard_method=I.Null)
            if not self._controller.set_screenshot_use_raw_size(True):raise CaptureUnavailable('raw_size_option_rejected')
            wait_job(self._controller.post_connection())
    def capture(self):
        started=time.perf_counter();before=self.windows.snapshot(self.identity.hwnd)
        if before.minimized:raise CaptureUnavailable('minimized_pause')
        if (before.pid,before.process_started)!=(self.identity.pid,self.identity.process_started):raise CaptureUnavailable('window_identity_changed')
        if not same_target(self.identity,before):raise CaptureUnavailable('geometry_changed_rebaseline')
        foreground_before=int(self.windows.u.GetForegroundWindow() or 0)
        if self.backend=='maa_gdi':
            array=wait_job(self._controller.post_screencap()).get()
            if array is None or array.size==0:raise CaptureUnavailable('empty_image')
            image=Image.fromarray(array[:,:,::-1].copy())
        elif self.backend=='win32_printwindow':image=self._printwindow(before)
        else:
            if not before.foreground or foreground_before!=before.hwnd:raise CaptureUnavailable('foreground_required')
            from PIL import ImageGrab
            x,y=before.client_screen;w,h=before.client_size
            image=ImageGrab.grab(bbox=(x,y,x+w,y+h),all_screens=True)
        after=self.windows.snapshot(before.hwnd)
        if not same_target(before,after):raise CaptureUnavailable('geometry_changed_rebaseline')
        foreground_after=int(self.windows.u.GetForegroundWindow() or 0)
        if self.backend=='foreground_crop' and (not after.foreground or foreground_after!=before.hwnd):
            raise CaptureUnavailable('foreground_changed_during_capture')
        if image.size!=before.client_size:raise CaptureUnavailable('unverified_image_coordinate_mapping')
        if max(ImageStat.Stat(image).stddev)<1:raise CaptureUnavailable('blank_image')
        return image,dict(backend=self.backend,window=before.data(),image_size=list(image.size),
                          client_to_image_scale=[1,1],duration_ms=round((time.perf_counter()-started)*1000,2),
                          target_foreground_after=after.foreground,
                          foreground_changed=foreground_after!=foreground_before)
    def _printwindow(self,snapshot):
        u=self.windows.u;g=ctypes.WinDLL('gdi32',use_last_error=True)
        u.GetDC.argtypes=[W.HWND];u.GetDC.restype=W.HDC;u.ReleaseDC.argtypes=[W.HWND,W.HDC]
        u.PrintWindow.argtypes=[W.HWND,W.HDC,W.UINT];u.PrintWindow.restype=W.BOOL
        g.CreateCompatibleDC.argtypes=[W.HDC];g.CreateCompatibleDC.restype=W.HDC
        g.CreateCompatibleBitmap.argtypes=[W.HDC,ctypes.c_int,ctypes.c_int];g.CreateCompatibleBitmap.restype=W.HBITMAP
        g.SelectObject.argtypes=[W.HDC,W.HGDIOBJ];g.SelectObject.restype=W.HGDIOBJ
        g.DeleteObject.argtypes=[W.HGDIOBJ];g.DeleteDC.argtypes=[W.HDC]
        class Header(ctypes.Structure):
            _fields_=[('size',W.DWORD),('width',W.LONG),('height',W.LONG),('planes',W.WORD),('bits',W.WORD),('compression',W.DWORD),('image',W.DWORD),('x',W.LONG),('y',W.LONG),('used',W.DWORD),('important',W.DWORD)]
        w,h=snapshot.client_size;source=u.GetDC(snapshot.hwnd);dc=g.CreateCompatibleDC(source);bitmap=g.CreateCompatibleBitmap(source,w,h);old=g.SelectObject(dc,bitmap)
        try:
            if not u.PrintWindow(snapshot.hwnd,dc,3):raise CaptureUnavailable('PrintWindow_failed')
            header=Header(ctypes.sizeof(Header),w,-h,1,32,0,0,0,0,0,0);buffer=ctypes.create_string_buffer(w*h*4)
            g.GetDIBits.argtypes=[W.HDC,W.HBITMAP,W.UINT,W.UINT,ctypes.c_void_p,ctypes.c_void_p,W.UINT]
            if g.GetDIBits(dc,bitmap,0,h,buffer,ctypes.byref(header),0)!=h:raise CaptureUnavailable('GetDIBits_incomplete')
            return Image.frombytes('RGB',(w,h),buffer.raw,'raw','BGRX')
        finally:
            g.SelectObject(dc,old);g.DeleteObject(bitmap);g.DeleteDC(dc);u.ReleaseDC(snapshot.hwnd,source)
    def close(self):
        self._controller=None
    def click(self,*args,**kwargs):raise PermissionError('wechat_input_disabled')
    input_text=click;press_key=click;paste=click;send=click
