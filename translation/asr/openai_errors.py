"""Safe OpenAI error fields. Never serialize requests, headers, or audio packets."""
import json
import re

FIELDS = ('type', 'code', 'message', 'param', 'event_id')


def safe_text(value, secrets=(), limit=1200):
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        return '[non-scalar omitted]'
    text = str(value)
    if len(text) > 65536:
        return '[oversized field omitted]'
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'sk-[A-Za-z0-9_-]+', '[redacted]', text)
    # A server message can quote bad request values. Strip credential assignments
    # and audio fields before truncating so a partial secret cannot survive.
    text = re.sub(r'(?i)\bauthorization\b[^\r\n]*', '[authorization omitted]', text)
    text = re.sub(r'(?i)\bbearer\s+\S+', '[credential omitted]', text)
    text = re.sub(r"""(?i)["']?(?:api[_-]?key|access[_-]?token)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,}]+)""",
                  '[credential omitted]', text)
    text = re.sub(r"""(?i)["']?(?:audio|pcm|audio_data)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,}]+)""",
                  '[audio omitted]', text)
    text = re.sub(r'[A-Za-z0-9+/=_-]{128,}', '[long data omitted]', text)
    text = ''.join(c if c >= ' ' and c != '\x7f' else ' ' for c in text)
    return text[:limit]


def error_fields(data, secrets=()):
    data = data if isinstance(data, dict) else {}
    error = data.get('error')
    error = error if isinstance(error, dict) else {}
    fields = {name: safe_text(error.get(name), secrets, 1200 if name == 'message' else 256)
              for name in FIELDS}
    fields['server_event_id'] = safe_text(data.get('event_id'), secrets, 256)
    return fields


def transport_error(exc, secrets=()):
    """Read only bounded JSON error bodies, never str(exc), headers, or request."""
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    data = {}
    body = getattr(response, 'body', None)
    if isinstance(body, (str, bytes, bytearray)) and len(body) <= 65536:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            pass
    received = getattr(exc, 'rcvd', None)
    fields = error_fields(data, secrets)
    if fields['message'] is None and received is not None:
        fields['message'] = safe_text(getattr(received, 'reason', None), secrets)
    fields.update(
        transport='http_handshake' if status is not None else
                  'websocket_close' if received is not None else 'network',
        exception_type=type(exc).__name__,
        http_status=status if isinstance(status, int) else None,
        close_code=getattr(received, 'code', None))
    if not isinstance(fields['close_code'], int):
        fields['close_code'] = None
    return fields
