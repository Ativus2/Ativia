"""Disposable transport subprocess. All operations have a parent-owned hard deadline.

No Qt, microphone, model downloads or LLM calls are loaded here. API credentials
are inherited in the environment, never included in command-line arguments/logs.
"""
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from tts_audio import InvalidAudio, MAX_AUDIO_BYTES, validate_wav_bytes, finalize_openai_streaming_wav


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("redirect_rejected")


def download_local(data):
    url = data["endpoint"]
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname != "127.0.0.1" or target.username or target.password:
        raise RuntimeError("non_loopback_endpoint_rejected")
    request = Request(url + "/tts", json.dumps(dict(text=data["text"], voice=data["voice"],
                      speed=data["speed"], language="auto", resource_policy=data.get('resource_policy', {})), ensure_ascii=False).encode("utf-8"),
                      {"Content-Type": "application/json", "X-Ativia-Reply-ID": data["reply_id"]})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=data["timeout"]) as response:
        if response.status != 200:
            raise RuntimeError("unexpected_http_status")
        if "audio/wav" not in response.headers.get("Content-Type", "").lower():
            raise InvalidAudio("incorrect_content_type")
        content = response.read(MAX_AUDIO_BYTES + 1)
        advertised = response.headers.get("Content-Length")
        if advertised is not None and int(advertised) != len(content):
            raise InvalidAudio("http_content_length_mismatch")
        return content


def download_openai(data):
    from openai import OpenAI
    with OpenAI(timeout=data["timeout"], max_retries=0) as client:
        with client.audio.speech.with_streaming_response.create(
            model=data["model"], voice=data["voice"], input=data["text"],
            instructions=data["instructions"], speed=data["speed"], response_format="wav",
        ) as response:
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > MAX_AUDIO_BYTES:
                    raise InvalidAudio("audio_exceeds_size_limit")
                chunks.append(chunk)
            return b"".join(chunks)


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        content = download_local(data) if data["backend"] == "local" else finalize_openai_streaming_wav(download_openai(data))
        validate_wav_bytes(content)
        Path(data["output"]).write_bytes(content)
        print(json.dumps(dict(ok=True, size=len(content))))
    except Exception as exc:
        status = getattr(exc, "status_code", getattr(exc, "code", None))
        protected = False
        detail = ''
        if isinstance(exc, HTTPError):
            if data['backend'] == 'local' and exc.code == 409:
                try:
                    detail = json.loads(exc.read(4096)).get('detail', '')
                    protected = isinstance(detail, str) and detail.startswith('GPU_PROTECTED:')
                except (OSError, ValueError, AttributeError):
                    pass
            exc.close()
        print(json.dumps(dict(ok=False, invalid_audio=isinstance(exc, InvalidAudio),
                              gpu_protected=protected,
                              gpu_reason=detail if protected else '',
                              reason=str(exc) if isinstance(exc, InvalidAudio) else type(exc).__name__,
                              status=status if isinstance(status, int) else None)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
