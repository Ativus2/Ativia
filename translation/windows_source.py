"""Windows Live Captions source. No audio libraries, OCR, or transcript persistence."""
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ERRORS = {
    'window_missing': '无法找到 Windows 实时字幕窗口，请先手动打开 Windows 实时字幕。',
    'setup_required': '请先完成 Windows 实时字幕初始化，并将源语言设置为韩语或英语、安装相应语言包。',
    'window_closed': 'Windows 实时字幕已关闭，请重新开始翻译。',
    'automation_failed': '无法读取 Windows 实时字幕控件；请检查系统更新、窗口权限或重新打开实时字幕。',
}

class SourceError(RuntimeError):
    pass

class WindowsLiveCaptionsBackend:
    def __init__(self, close_owned=True):
        self.process = None
        self.close_owned = close_owned
        self.window = None
        self.ready = False
        self._closing = None
        self._hidden_requested = False

    @staticmethod
    def available():
        return (sys.platform == 'win32' and sys.getwindowsversion().build >= 22621
                and (Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/LiveCaptions.exe').exists())

    @staticmethod
    def executable():
        if not WindowsLiveCaptionsBackend.available():
            raise SourceError('需要支持实时字幕的 Windows 11 22H2 或更新版本；未切换到其他识别后端。')
        source = ROOT / 'tools/CaptionBridge.cs'
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        cache = ROOT / 'local_tts/cache/livecaptions'
        cache.mkdir(parents=True, exist_ok=True)
        output = cache / ('CaptionBridge-' + digest + '.exe')
        if not output.exists():
            framework = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319'
            args = [str(framework / 'csc.exe'), '/nologo', '/target:exe', '/optimize+', '/out:' + str(output),
                    '/r:' + str(framework / 'WPF/UIAutomationClient.dll'),
                    '/r:' + str(framework / 'WPF/UIAutomationTypes.dll'),
                    '/r:' + str(framework / 'WPF/WindowsBase.dll'),
                    '/r:' + str(framework / 'System.Web.Extensions.dll'), str(source)]
            result = subprocess.run(args, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise SourceError('实时字幕读取组件编译失败；请检查 Windows .NET Framework 4.x。')
        return output

    async def events(self):
        import json
        executable = await asyncio.to_thread(self.executable)
        self.process = await asyncio.create_subprocess_exec(str(executable), str(os.getpid()),
            'yes' if self.close_owned else 'no', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, limit=262144)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(self.process.stdout.readline(), 20 if not self.ready else 6)
                except asyncio.TimeoutError:
                    raise SourceError('Windows 字幕读取超时，请检查实时字幕是否完成初始化。') from None
                if not line:
                    raise SourceError('Windows 字幕读取组件已退出。')
                event = json.loads(line.decode('utf-8-sig'))
                if event['kind'] == 'error':
                    raise SourceError(ERRORS.get(event['code'], ERRORS['automation_failed']))
                if event['kind'] == 'window': self.window = event
                if event['kind'] == 'ready': self.ready = True
                yield event
        finally:
            await self.close()

    def command(self, command):
        if command in ('show','hide'): self._hidden_requested = command == 'hide'
        if self.process and self.process.returncode is None and self.process.stdin:
            try: self.process.stdin.write((command + '\n').encode('ascii'))
            except (BrokenPipeError, ConnectionResetError): pass

    async def close(self):
        if self._closing:
            await asyncio.shield(self._closing)
            return
        process = self.process
        if not process: return
        self.process = None
        self._closing = asyncio.create_task(self._close_process(process))
        await asyncio.shield(self._closing)

    async def _close_process(self, process):
        if process.stdin:
            try: process.stdin.write(b'stop\n')
            except (BrokenPipeError, ConnectionResetError): pass
            process.stdin.close()
            try: await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError): pass
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()  # only our own bridge, never LiveCaptions
            await process.wait()
            self._restore_after_hang()

    def _restore_after_hang(self):
        if not self.window: return
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        pid = wintypes.DWORD()
        h = self.window['hwnd']
        user.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == self.window['pid']:
            if self._hidden_requested and not self.window.get('minimized', False):
                user.ShowWindow(h, 4)
            if self.window['owned'] and self.ready and self.close_owned:
                user.PostMessageW(h, 0x10, 0, 0)

