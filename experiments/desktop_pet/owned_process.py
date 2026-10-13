"""Windows jobs contain only this launcher's child processes, never other browsers."""
import ctypes
from ctypes import wintypes as w
import subprocess
import time

class BasicLimits(ctypes.Structure):
    _fields_=[('per_process',ctypes.c_int64),('per_job',ctypes.c_int64),('flags',w.DWORD),
        ('min_work',ctypes.c_size_t),('max_work',ctypes.c_size_t),('active',w.DWORD),
        ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
class IO(ctypes.Structure):
    _fields_=[(n,ctypes.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
class ExtendedLimits(ctypes.Structure):
    _fields_=[('basic',BasicLimits),('io',IO),('process_memory',ctypes.c_size_t),
        ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]

class OwnedProcess:
    def __init__(self, args, *, cwd, env=None, log=None, visible=False):
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.kernel.CreateJobObjectW.restype=w.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        self.kernel.TerminateJobObject.argtypes=[w.HANDLE,w.UINT]
        self.kernel.CloseHandle.argtypes=[w.HANDLE]
        self.job=self.kernel.CreateJobObjectW(None,None)
        limits=ExtendedLimits();limits.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.job or not self.kernel.SetInformationJobObject(self.job,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.log=open(log,'ab') if log else None
        self.process=None
        try:
            startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow=1 if visible else 0
            self.process=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                stdout=self.log or subprocess.DEVNULL,stderr=self.log or subprocess.DEVNULL,
                creationflags=0x4 | (0 if visible else subprocess.CREATE_NO_WINDOW),startupinfo=startup)
            if not self.kernel.AssignProcessToJobObject(self.job,w.HANDLE(int(self.process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
            ntdll=ctypes.WinDLL('ntdll');ntdll.NtResumeProcess.argtypes=[w.HANDLE];ntdll.NtResumeProcess.restype=ctypes.c_long
            if ntdll.NtResumeProcess(w.HANDLE(int(self.process._handle)))!=0:raise RuntimeError('Unable to resume owned process')
        except Exception:
            if self.process:self.process.kill()
            self.close();raise

    def close(self):
        if self.job:
            if not self.kernel.TerminateJobObject(self.job,0):raise ctypes.WinError(ctypes.get_last_error())
            self.kernel.CloseHandle(self.job);self.job=None
        if self.process:
            self.process.wait(timeout=3)
        if self.log:self.log.close();self.log=None

    def running(self):return self.process is not None and self.process.poll() is None
