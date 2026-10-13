"""Nova-3 streaming protocol, bounded retries and no credential-bearing logs."""
import asyncio
from collections import OrderedDict, deque
import json
import logging
import os
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode
from .base import ASRProvider, ASRError
from .models import TranscriptEvent


def load_api_key():
    key = os.environ.get('DEEPGRAM_API_KEY', '').strip()
    if not key:
        from dotenv import dotenv_values
        key = (dotenv_values(Path(__file__).resolve().parents[2] / '.env').get('DEEPGRAM_API_KEY') or '').strip()
    return key


class DeepgramASR(ASRProvider):
    name = 'deepgram'
    accepts_audio = True
    endpoint = 'wss://api.deepgram.com/v1/listen'

    def __init__(self, language='ko', hotwords=(), endpoint_ms=650, sample_rate=16000,
                 connector=None, key_loader=load_api_key, retry_delays=(.5, 1, 2)):
        super().__init__(language, hotwords)
        self.endpoint_ms, self.sample_rate = endpoint_ms, sample_rate
        self.connector, self.key_loader, self.retry_delays = connector, key_loader, retry_delays
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.revisions = OrderedDict()
        self.audio_marks = deque(maxlen=600)  # 60 seconds at 100 ms/block
        self.audio_seconds = 0.0
        self.last_send = 0.0
        self.finalized = asyncio.Event()

    def url(self):
        if self.language not in ('auto', 'ko', 'en', 'ja', 'zh'):
            raise ASRError('Deepgram 识别语言无效。')
        params = [('model', 'nova-3'), ('language', 'multi' if self.language == 'auto' else self.language),
                  ('encoding', 'linear16'), ('sample_rate', str(self.sample_rate)), ('channels', '1'),
                  ('interim_results', 'true'), ('punctuate', 'true'),
                  ('endpointing', str(self.endpoint_ms))]
        params += [('keyterm', word) for word in self.hotwords[:100]]
        return self.endpoint + '?' + urlencode(params)

    def parse(self, data, now=None):
        now = monotonic() if now is None else now
        if data.get('type') != 'Results': return None
        if data.get('from_finalize'): self.finalized.set()
        alternatives = data.get('channel', {}).get('alternatives') or []
        if not alternatives: return None
        alt = alternatives[0]
        text = (alt.get('transcript') or '').strip()[:8000]
        if not text: return None
        start = float(data.get('start', 0))
        end = start + float(data.get('duration', 0))
        # The start time identifies a hypothesis; duration grows during interim updates.
        segment_id = f'{start:.6f}'
        signature = (text, bool(data.get('is_final')))
        previous, revision = self.revisions.get(segment_id, (None, 0))
        if signature != previous: revision += 1
        self.revisions[segment_id] = (signature, revision)
        while len(self.revisions) > 256: self.revisions.popitem(last=False)
        words = alt.get('words') or []
        detected = alt.get('languages') or []
        language = (detected[0] if detected else
                    next((w['language'] for w in reversed(words) if w.get('language')), None))
        language = language or (self.language if self.language != 'auto' else 'und')
        latency = None
        # Timing is measured from the last recognized word's captured audio end.
        # Never label websocket connect time as ASR latency.
        word_end = float(words[-1]['end']) if words and 'end' in words[-1] else end
        for offset, captured in reversed(self.audio_marks):
            if offset <= word_end + .11:
                estimate = captured + (word_end - offset)
                latency = round(max(0, now - estimate)*1000, 1)
                break
        confidence = alt.get('confidence')
        if not isinstance(confidence, (int, float)): confidence = None
        return TranscriptEvent(text, bool(data.get('is_final')), confidence, language, now,
            self.name, segment_id, revision, self.epoch, bool(data.get('speech_final')),
            start, end, latency, cumulative=True)

    async def _send(self, data):
        async with self.send_lock:
            if not self.ws: return False
            await asyncio.wait_for(self.ws.send(data), 2)
            self.last_send = monotonic()
            return True

    async def push_audio(self, pcm, captured_at=None):
        if not self.ready.is_set() or not self.ws: return False
        try:
            if await self._send(pcm):
                self.audio_seconds += len(pcm)/(2*self.sample_rate)
                self.audio_marks.append((self.audio_seconds, captured_at or monotonic()))
                return True
        except Exception:
            # Reader owns reconnect. Closing its socket wakes it; never log exceptions.
            if self.ws:
                try: await asyncio.wait_for(self.ws.close(),2)
                except Exception: pass
        return False

    async def finalize(self):
        self.finalized.clear()
        await self._send(json.dumps({'type': 'Finalize'}))
        try: await asyncio.wait_for(self.finalized.wait(), 5)
        except TimeoutError: pass  # no final acknowledgement is required for empty audio

    async def _keepalive(self):
        while True:
            await asyncio.sleep(3)
            if monotonic() - self.last_send >= 3:
                await self._send(json.dumps({'type': 'KeepAlive'}))

    async def run(self):
        key = self.key_loader()
        if not key:
            raise ASRError('未配置 DEEPGRAM_API_KEY；请在项目 .env 中设置，或切换 Windows。')
        connector = self.connector
        if connector is None:
            try: from websockets.asyncio.client import connect
            except ImportError:
                raise ASRError('缺少 websockets 依赖，请安装项目 requirements.txt。') from None
            connector = connect
        # Private disabled logger: websockets debug logging must never expose auth headers.
        logger = logging.Logger('ativia.asr.transport'); logger.disabled = True
        url = self.url()
        for attempt in range(len(self.retry_delays) + 1):
            heartbeat = None
            try:
                self.set_status('Deepgram 正在连接…')
                async with connector(url, additional_headers={'Authorization': 'Token ' + key},
                                     open_timeout=6, close_timeout=1, max_size=262144,
                                     max_queue=16, logger=logger) as ws:
                    self.ws = ws
                    self.audio_seconds = 0; self.audio_marks.clear(); self.revisions.clear()
                    self.last_send = monotonic(); self.ready.set()
                    self.set_status('Deepgram Nova-3 · 已连接' +
                        (' · 自动多语不含韩语/中文' if self.language == 'auto' else ' · '+self.language))
                    heartbeat = asyncio.create_task(self._keepalive())
                    async for payload in ws:
                        data = json.loads(payload)
                        if data.get('type') == 'Error': raise ASRError('Deepgram 服务返回识别错误。')
                        event = self.parse(data)
                        if event: self.emit(event)
                    raise ConnectionError('closed')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(getattr(exc, 'response', None), 'status_code', None)
                if code in (401, 403):
                    raise ASRError(f'Deepgram HTTP {code}：密钥无效或权限不足，请检查 DEEPGRAM_API_KEY；可切换 Windows。') from None
                if code == 400:
                    raise ASRError('Deepgram HTTP 400：识别参数不受支持，请检查语言和术语；可切换 Windows。') from None
                self.ready.clear(); self.ws = None
                self.epoch += 1; self.on_reset(self.epoch)
                self.set_status(f'Deepgram disconnected · 重连 {attempt}/{len(self.retry_delays)}')
                if attempt >= len(self.retry_delays):
                    raise ASRError('Deepgram disconnected · 已停止重试；可在设置中切换 Windows。') from None
                await asyncio.sleep(self.retry_delays[attempt])
            finally:
                self.ready.clear(); self.ws = None
                if heartbeat:
                    heartbeat.cancel(); await asyncio.gather(heartbeat, return_exceptions=True)
