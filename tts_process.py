"""Reap a blocked backend process before fallback/cancellation; never leave a thread behind."""
from contextvars import ContextVar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

from tts_audio import InvalidAudio, delete_audio, validate_audio_file
from tts_events import TTSCancelled, TTSFailure, GPUProtected

operation_context = ContextVar("tts_operation_context", default=None)


def run_backend(backend, **payload):
    context = operation_context.get() or {}
    cancelled = context.get("cancelled", lambda: False)
    try:
        timeout = float(os.getenv("ATIVIA_" + backend.upper() + "_TTS_DEADLINE_SECONDS", "90" if backend == "local" else "60"))
    except ValueError:
        timeout = 90 if backend == "local" else 60
    timeout = max(.2, min(300, timeout))
    if cancelled():
        raise TTSCancelled("cancelled_before_request")
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="ativia-tts-")
    os.close(fd)
    payload.update(backend=backend, reply_id=context.get("reply_id", "standalone"), timeout=timeout, output=path)
    process = None
    success = False
    try:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("tts_transport.py"))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        request = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        deadline = monotonic() + timeout
        while True:
            if cancelled():
                raise TTSCancelled("cancelled_during_request")
            if monotonic() >= deadline:
                raise TimeoutError(backend + "_total_deadline_exceeded")
            try:
                stdout, _stderr = process.communicate(request, timeout=min(.1, max(.01, deadline - monotonic())))
                break
            except subprocess.TimeoutExpired:
                request = None
        if cancelled():
            raise TTSCancelled("cancelled_after_request")
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError):
            raise TTSFailure("transport_exited_without_result") from None
        if not result.get("ok") or process.returncode:
            reason = result.get("reason", "transport_failed")
            if backend == 'local' and result.get('gpu_protected'):
                raise GPUProtected(result.get('gpu_reason') or 'server_gpu_protected')
            if result.get("invalid_audio"):
                raise InvalidAudio(reason)
            raise TTSFailure(f"{reason}; HTTP={result.get('status')}")
        validate_audio_file(path)
        success = True
        return path
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if not success:
            delete_audio(path)
