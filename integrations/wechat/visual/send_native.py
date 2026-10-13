"""Main-process-only Win32 input. Constructing this object performs no input.

No clipboard, UIA, DLL injection, elevation, window movement or input retries.
The UI must own foreground before a transaction; only its verified WeChat
target can receive the subsequent click/Unicode/Enter sequence.
"""
import ctypes
from ctypes import wintypes as W
import os

from .send import SendRejected, window_key
from .window import Win32Windows


class MOUSEINPUT(ctypes.Structure):
    _fields_=[('dx',W.LONG),('dy',W.LONG),('mouseData',W.DWORD),('dwFlags',W.DWORD),
              ('time',W.DWORD),('dwExtraInfo',ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_=[('wVk',W.WORD),('wScan',W.WORD),('dwFlags',W.DWORD),('time',W.DWORD),('dwExtraInfo',ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_=[('uMsg',W.DWORD),('wParamL',W.WORD),('wParamH',W.WORD)]


class InputUnion(ctypes.Union):
    _fields_=[('mi',MOUSEINPUT),('ki',KEYBDINPUT),('hi',HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_=('data',)
    _fields_=[('type',W.DWORD),('data',InputUnion)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_=[('cbSize',W.UINT),('dwTime',W.DWORD)]


def keyboard(vk=0, scan=0, flags=0):
    item=INPUT(); item.type=1
    item.ki=KEYBDINPUT(vk,scan,flags,0,0)
    return item


def unicode_inputs(text):
    events=[]
    for ch in text:
        if ch=='\n':
            # Newline must not accidentally perform a submit.
            events.extend((keyboard(0x10),keyboard(0x0D),keyboard(0x0D,flags=2),keyboard(0x10,flags=2)))
        else:
            encoded=ch.encode('utf-16-le')
            for offset in range(0,len(encoded),2):
                unit=int.from_bytes(encoded[offset:offset+2],'little')
                events.extend((keyboard(scan=unit,flags=4),keyboard(scan=unit,flags=6)))
    return events


class NativeWechatInput:
    def __init__(self, preview_hwnd):
        if not preview_hwnd:
            raise ValueError('owned_preview_required')
        self.windows=Win32Windows(); self.u=self.windows.u
        self.preview_hwnd=int(preview_hwnd)
        preview=self.windows.snapshot(self.preview_hwnd)
        if preview.pid!=os.getpid():
            raise ValueError('preview_must_belong_to_coordinator')
        self.u.SendInput.argtypes=[W.UINT,ctypes.POINTER(INPUT),ctypes.c_int];self.u.SendInput.restype=W.UINT
        self.u.SetForegroundWindow.argtypes=[W.HWND];self.u.SetForegroundWindow.restype=W.BOOL
        self.u.GetLastInputInfo.argtypes=[ctypes.POINTER(LASTINPUTINFO)];self.u.GetLastInputInfo.restype=W.BOOL
        self.u.GetAsyncKeyState.argtypes=[ctypes.c_int];self.u.GetAsyncKeyState.restype=ctypes.c_short
        self.u.GetSystemMetrics.argtypes=[ctypes.c_int];self.u.GetSystemMetrics.restype=ctypes.c_int
        self.context=None;self.guard=None;self.last_input=None;self.pointer=None;self.focused=False
        self.original_pointer=None
        self.clicks=self.keys=self.submit_attempts=0

    def _activity(self):
        info=LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO),0)
        point=W.POINT()
        if not self.u.GetLastInputInfo(ctypes.byref(info)) or not self.u.GetCursorPos(ctypes.byref(point)):
            raise SendRejected('user_activity_unknown')
        return (info.dwTime,(point.x,point.y))

    def _modifiers(self):
        # Never release or alter keys the user is holding.
        if any(self.u.GetAsyncKeyState(vk)&0x8000 for vk in (1,2,4,0x10,0x11,0x12,0x5B,0x5C)):
            raise SendRejected('SEND_CANCELLED_USER_INTERACTION')

    def begin(self, context, guard):
        self.context,self.guard,self.focused=context,guard,False
        self.check(context,focused=False,check_activity=False)
        if int(self.u.GetForegroundWindow() or 0)!=self.preview_hwnd:
            raise SendRejected('SEND_CANCELLED_USER_INTERACTION')
        if self.windows.pointer_over_window(context.window['hwnd']):
            raise SendRejected('SEND_CANCELLED_USER_INTERACTION')
        self._modifiers()
        self.last_input,self.pointer=self._activity()
        self.original_pointer=self.pointer

    def check(self, context, *, focused, check_activity=True):
        if self.guard is None or not self.guard():
            raise SendRejected('stale_or_paused')
        try:
            native=self.windows.snapshot(context.window['hwnd'])
        except Exception as exc:
            raise SendRejected('window_closed') from exc
        if window_key(native.data())!=window_key(context.window) or native.minimized:
            raise SendRejected('window_identity_changed')
        foreground=int(self.u.GetForegroundWindow() or 0)
        expected=context.window['hwnd'] if focused else self.preview_hwnd
        if foreground!=expected:
            raise SendRejected('SEND_CANCELLED_USER_INTERACTION')
        self._modifiers()
        if check_activity and self.last_input is not None and self._activity()!=(self.last_input,self.pointer):
            raise SendRejected('SEND_CANCELLED_USER_INTERACTION')

    def _send(self, events, *, focused=True):
        self.check(self.context,focused=focused)
        array=(INPUT*len(events))(*events)
        result=self.u.SendInput(len(events),array,ctypes.sizeof(INPUT))
        self.last_input,self.pointer=self._activity()
        if result!=len(events):
            raise SendRejected('native_input_partial')

    def focus(self, composer):
        self.check(self.context,focused=False)
        if not isinstance(composer,list) or len(composer)!=4 or any(type(x) is not int for x in composer):
            raise SendRejected('invalid_composer')
        x,y,width,height=composer
        cw,ch=self.context.window['client_rect'][2:]
        if not (0<x<x+width<cw and 0<y<y+height<ch):
            raise SendRejected('invalid_composer')
        if not self.u.SetForegroundWindow(self.context.window['hwnd']):
            raise SendRejected('focus_rejected')
        self.focused=True
        self.check(self.context,focused=True)
        sx,sy=self.context.window['client_screen']
        px=sx+x+min(20,width//4);py=sy+y+min(18,height//4)
        vx,vy=self.u.GetSystemMetrics(76),self.u.GetSystemMetrics(77)
        vw,vh=self.u.GetSystemMetrics(78),self.u.GetSystemMetrics(79)
        if vw<2 or vh<2 or not vx<=px<vx+vw or not vy<=py<vy+vh:
            raise SendRejected('composer_outside_desktop')
        events=[]
        move=INPUT();move.type=0;move.mi=MOUSEINPUT(round((px-vx)*65535/(vw-1)),round((py-vy)*65535/(vh-1)),0,0xC001,0,0)
        events.append(move)
        for flags in (2,4):
            event=INPUT();event.type=0;event.mi=MOUSEINPUT(0,0,0,flags,0,0);events.append(event)
        self.clicks+=1
        self._send(events)

    def write(self, text):
        events=unicode_inputs(text)
        self.keys+=len(events)
        self._send(events)

    def submit(self):
        self.check(self.context,focused=True)
        self.submit_attempts+=1;self.keys+=2
        self._send([keyboard(0x0D),keyboard(0x0D,flags=2)])

    def end(self):
        try:
            # Restore only while we still own the unchanged input sequence.
            # Physical interaction means leave focus and cursor to the user.
            if (self.context and self.focused and
                    int(self.u.GetForegroundWindow() or 0)==self.context.window['hwnd'] and
                    self._activity()==(self.last_input,self.pointer) and
                    window_key(self.windows.snapshot(self.context.window['hwnd']).data())==window_key(self.context.window) and
                    self.windows.snapshot(self.preview_hwnd).pid==os.getpid()):
                self.u.SetForegroundWindow(self.preview_hwnd)
                if (int(self.u.GetForegroundWindow() or 0)==self.preview_hwnd and self.original_pointer is not None
                        and self._activity()==(self.last_input,self.pointer)):
                    self.u.SetCursorPos(*self.original_pointer)
        except Exception:
            # The target/UI may already have closed. Never retry restoration
            # or let that prevent revocation of the input capability.
            pass
        finally:
            self.context=self.guard=None;self.focused=False
