"""Small per-instance admission gate and owned resource cleanup, no business logic."""
from functools import wraps
import threading
from PySide6.QtCore import QThread


class PauseGate:
    def __init__(self, paused=False):
        self.state='PAUSED' if paused else 'RUNNING'
        self.epoch=0
        self._lock=threading.RLock()
        self._subscribers=[]

    def allows(self, epoch=None):
        with self._lock:
            return self.state=='RUNNING' and (epoch is None or epoch==self.epoch)

    def transition(self, state):
        with self._lock:
            if state in ('PAUSING','RESUMING'):self.epoch+=1
            self.state=state
            subscribers=tuple(self._subscribers);epoch=self.epoch
        for callback in subscribers:
            try:callback(state,epoch)
            except Exception:pass

    def subscribe(self,callback):
        with self._lock:self._subscribers.append(callback)
        def unsubscribe():
            with self._lock:
                if callback in self._subscribers:self._subscribers.remove(callback)
        return unsubscribe


def running_only(method):
    @wraps(method)
    def guarded(self,*args,**kwargs):
        gate=getattr(self,'_pause_gate',None)
        if gate is not None and not gate.allows():return
        return method(self,*args,**kwargs)
    return guarded


class ResourceChange(QThread):
    def __init__(self, operation, parent):
        super().__init__(parent);self.operation=operation;self.error=None

    def run(self):
        try:self.operation()
        except Exception as exc:self.error=type(exc).__name__+': '+str(exc)
