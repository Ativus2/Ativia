"""Read-only NVIDIA/Windows telemetry. No torch import or CUDA context creation."""
from collections import deque
from dataclasses import dataclass, field
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import threading
from time import monotonic


class Utilization(C.Structure):
    _fields_ = [('gpu', C.c_uint), ('memory', C.c_uint)]


class Memory(C.Structure):
    _fields_ = [('total', C.c_ulonglong), ('free', C.c_ulonglong), ('used', C.c_ulonglong)]


class NvidiaReader:
    def __init__(self):
        path = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/nvml.dll') if os.name == 'nt' else 'libnvidia-ml.so.1'
        self.lib = C.CDLL(path)
        self.lib.nvmlInit_v2.restype = C.c_int
        self.check(self.lib.nvmlInit_v2())
        self.handle = C.c_void_p()
        self.lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [C.c_uint, C.POINTER(C.c_void_p)]
        self.check(self.lib.nvmlDeviceGetHandleByIndex_v2(0, C.byref(self.handle)))
        self.lib.nvmlDeviceGetUtilizationRates.argtypes = [C.c_void_p, C.POINTER(Utilization)]
        self.lib.nvmlDeviceGetMemoryInfo.argtypes = [C.c_void_p, C.POINTER(Memory)]

    @staticmethod
    def check(code):
        if code:
            raise RuntimeError('nvml_error_' + str(code))

    def read(self):
        use, memory = Utilization(), Memory()
        self.check(self.lib.nvmlDeviceGetUtilizationRates(self.handle, C.byref(use)))
        self.check(self.lib.nvmlDeviceGetMemoryInfo(self.handle, C.byref(memory)))
        if not memory.total or not 0 <= use.gpu <= 100 or memory.used > memory.total:
            raise RuntimeError('invalid_nvml_sample')
        return dict(utilization=float(use.gpu), used_mb=memory.used / 1048576,
                    total_mb=memory.total / 1048576, free_mb=memory.free / 1048576)

    def close(self):
        self.lib.nvmlShutdown()


def process_names():
    """Toolhelp snapshot reads executable names only, without admin/WMI/command lines."""
    if os.name != 'nt':
        return frozenset(p.read_text().strip().lower() for p in Path('/proc').glob('[0-9]*/comm'))
    class Entry(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('pid', W.DWORD),
                    ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                    ('exe', W.WCHAR * 260)]
    lib = C.WinDLL('kernel32', use_last_error=True)
    lib.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    lib.CreateToolhelp32Snapshot.restype = W.HANDLE
    lib.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    lib.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    lib.CloseHandle.argtypes = [W.HANDLE]
    handle = lib.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        raise OSError('process_snapshot_unavailable')
    try:
        item = Entry(); item.dwSize = C.sizeof(item)
        ok = lib.Process32FirstW(handle, C.byref(item))
        if not ok:
            raise OSError('process_snapshot_empty')
        names = set()
        while ok:
            names.add(item.exe.lower())
            ok = lib.Process32NextW(handle, C.byref(item))
        return frozenset(names)
    finally:
        lib.CloseHandle(handle)


@dataclass(frozen=True)
class Sample:
    at: float
    utilization: float | None = None
    used_mb: float | None = None
    total_mb: float | None = None
    free_mb: float | None = None
    processes: frozenset = field(default_factory=frozenset)
    error: str = ''
    process_error: bool = False

    @property
    def valid(self):
        return not self.error and all(v is not None for v in (self.utilization, self.used_mb, self.total_mb, self.free_mb)) and self.total_mb > 0


class GpuMonitor:
    def __init__(self, interval=1.0, reader_factory=NvidiaReader, processes=process_names):
        self.interval, self.reader_factory, self.processes = interval, reader_factory, processes
        self._history = deque(maxlen=180)  # Includes both ends of a 60-second recovery window.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='tts-gpu-monitor', daemon=True)
            self._thread.start()

    def _run(self):
        reader = None
        try:
            while not self._stop.is_set():
                try:
                    if reader is None:
                        reader = self.reader_factory()
                    data = reader.read()
                    try:
                        names, process_error = self.processes(), False
                    except Exception:
                        names, process_error = frozenset(), True
                    sample = Sample(at=monotonic(), **data, processes=names, process_error=process_error)
                except Exception as exc:
                    sample = Sample(at=monotonic(), error=type(exc).__name__ + ':' + str(exc))
                with self._lock:
                    self._history.append(sample)
                self._stop.wait(self.interval)
        finally:
            if reader is not None:
                reader.close()

    def history(self):
        with self._lock:
            return list(self._history)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


_monitor = GpuMonitor()


def get_monitor():
    _monitor.start()
    return _monitor
