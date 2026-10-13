"""Start only this project's missing loopback service; never stop an adopted service."""
import json
import os
from pathlib import Path
import socket
import threading
from time import monotonic, sleep
from urllib.request import build_opener, ProxyHandler
from tts_events import event, TTSCancelled

ROOT = Path(__file__).resolve().parent
_lock = threading.Lock()
_owned = None

class LocalServiceStarting(RuntimeError):
    """A cold service is making progress; don't hold a conversation behind imports."""
    pass

def health():
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:9881/health', timeout=.7) as r:
        data=json.load(r)
    weights=Path(data.get('weights','')).resolve()
    if (data.get('status')!='ready' or not str(data.get('model','')).startswith('GPT-SoVITS ')
            or not weights.is_relative_to(ROOT/'local_tts/models')):
        raise RuntimeError('port_9881_is_not_this_projects_voice_service')
    return data

def ensure_service(cancelled=lambda:False, reply_id='service', *, wait=False):
    global _owned
    while not _lock.acquire(timeout=.1):
        if cancelled():raise TTSCancelled('cancelled_waiting_for_local_service')
    created=False
    try:
        if cancelled():raise TTSCancelled('cancelled_before_local_service')
        try:return health()
        except OSError:pass
        # An occupied but unidentifiable port is not permission to start/kill another process.
        with socket.socket() as probe:
            probe.settimeout(.3)
            occupied=probe.connect_ex(('127.0.0.1',9881))==0
        if occupied and not (_owned and _owned.running()):
            raise RuntimeError('port_9881_busy_or_service_not_ready')
        if not _owned or not _owned.running():
            if _owned:_owned.close();_owned=None
            python=ROOT/'local_tts/.venv/Scripts/python.exe'
            if not python.is_file():raise RuntimeError('local_tts_environment_missing')
            from experiments.desktop_pet.owned_process import OwnedProcess
            env=os.environ.copy()
            # This local model never needs a cloud credential.
            env.pop('OPENAI_API_KEY',None)
            event(reply_id,'LOCAL_SERVICE_STARTING',port=9881)
            _owned=OwnedProcess([str(python),'-u',str(ROOT/'local_tts/server.py')],cwd=str(ROOT),
                env=env,log=str(ROOT/'local_tts/logs/daily-service.log'))
            created=True
        # Conversations do not wait behind cold Python/training-library imports.
        # The owned service keeps starting; only an explicit local-only test waits.
        if not wait:raise LocalServiceStarting('local_service_starting')
        deadline=monotonic()+120
        while monotonic()<deadline:
            if cancelled():raise TTSCancelled('cancelled_during_local_service_start')
            if not _owned.running():raise RuntimeError('local_service_start_failed_see_daily_service_log')
            try:
                data=health()
                event(reply_id,'LOCAL_SERVICE_READY',model_loaded=data.get('model_loaded',False))
                return data
            except OSError:sleep(.2)
        raise TimeoutError('local_service_start_deadline_see_daily_service_log')
    except LocalServiceStarting:
        raise
    except BaseException:
        if created and _owned:
            _owned.close();_owned=None
        raise
    finally:_lock.release()

def close_owned():
    global _owned
    with _lock:
        if _owned:_owned.close();_owned=None
