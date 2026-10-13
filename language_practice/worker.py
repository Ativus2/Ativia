"""Bounded, cancellable I/O. No audio libraries load until an explicit audio action."""
import asyncio
from array import array
import io
import os
import threading
import wave

from PySide6.QtCore import QObject, Signal
from .core import PracticeTurn, connection


class PracticeWorker(QObject):
    result = Signal(str, object)
    failed = Signal(str, str)
    status = Signal(str, str)
    finished = Signal(str)

    def __init__(self, token, kind, settings, payload, parent=None):
        super().__init__(parent)
        self.token, self.kind, self.settings, self.payload = token, kind, settings, payload
        self.cancelled = threading.Event()
        self.record_done = threading.Event()
        self.loop = self.task = self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='ativia-practice-' + self.kind, daemon=True)
        self.thread.start()

    def stop_recording(self):
        self.record_done.set()

    def stop(self):
        self.cancelled.set()
        self.record_done.set()
        if self.loop and self.task:
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass

    def _status(self, text):
        if not self.cancelled.is_set():
            self.status.emit(self.token, text)

    def _run(self):
        try:
            payload = self._record() if self.kind == 'record' else self.payload
            if self.cancelled.is_set():
                return
            result = asyncio.run(self._request(payload))
            if self.cancelled.is_set():
                return
            if self.kind == 'question_audio':
                self._play(result)
                result = None
            if not self.cancelled.is_set():
                self.result.emit(self.token, result)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.cancelled.is_set():
                code = getattr(exc, 'status_code', None)
                message = ('API密钥或访问权限无效，请检查设置。' if code in (401, 403) else
                           'API额度不足或请求过多，请稍后重试。' if code == 429 else
                           str(exc) if isinstance(exc, PracticeInputError) else
                           '连接或处理失败，请检查网络、API和模型。当前题目与输入已保留，可重试。')
                self.failed.emit(self.token, message)
        finally:
            self.payload = None
            self.finished.emit(self.token)

    def _record(self):
        # Only the system default microphone is opened. No loopback/system capture.
        import sounddevice as sd
        info = sd.query_devices(kind='input')
        rate = int(info['default_samplerate'])
        if not 8000 <= rate <= 192000:
            raise PracticeInputError('麦克风采样率不受支持，请更换默认输入设备。')
        blocks, count, overflow = [], 0, False
        limit = rate * 90 * 2

        def capture(data, frames, timing, status):
            nonlocal count, overflow
            if self.cancelled.is_set() or self.record_done.is_set():
                return
            overflow = overflow or bool(status)
            block = bytes(data)[:max(0, limit - count)]
            blocks.append(block)
            count += len(block)
            if count >= limit:
                self.record_done.set()

        with sd.RawInputStream(samplerate=rate, channels=1, dtype='int16',
                               blocksize=max(1, rate // 50), callback=capture):
            self._status('正在听你说 · 点击“说完了”结束；每次最多90秒')
            self.record_done.wait(90)
            self.record_done.set()
        if self.cancelled.is_set():
            return b''
        pcm = b''.join(blocks)
        if overflow:
            raise PracticeInputError('录音发生中断，请重新录制；也可以直接输入回答。')
        samples = array('h', pcm)
        if len(samples) < rate // 4 or not samples or max(abs(x) for x in samples) < 150:
            raise PracticeInputError('没有听到清楚的声音，请检查默认麦克风后重试。')
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(pcm)
        self._status('录音已结束，正在识别；字幕仍可阅读')
        return output.getvalue()

    async def _request(self, payload):
        from openai import AsyncOpenAI
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        if self.cancelled.is_set():
            raise asyncio.CancelledError()
        async with AsyncOpenAI(**connection(self.settings, audio=self.kind != 'generate')) as client:
            async with asyncio.timeout(45):
                if self.kind == 'generate':
                    response = await client.chat.completions.create(
                        model=self.settings.model.strip() or os.getenv('OPENAI_MODEL', '').strip() or 'gpt-4o-mini',
                        messages=payload, response_format={'type': 'json_object'}, max_completion_tokens=1800)
                    try:
                        return PracticeTurn.parse(response.choices[0].message.content or '')
                    except ValueError as exc:
                        raise PracticeInputError(str(exc)) from None
                if self.kind == 'record':
                    response = await client.audio.transcriptions.create(
                        model=self.settings.audio_model, language=self.settings.language,
                        file=('practice.wav', payload, 'audio/wav'))
                    text = (response if isinstance(response, str) else response.text or '').strip()
                    if not text:
                        raise PracticeInputError('未识别到回答，请再说一次或直接输入。')
                    return text
                if self.kind == 'question_audio':
                    response = await client.audio.speech.create(
                        model=self.settings.speech_model, voice='alloy', input=payload,
                        response_format='pcm', speed=0.9)
                    return response.content
                raise PracticeInputError('未知练习操作。')

    def _play(self, pcm):
        if self.cancelled.is_set():
            return
        import sounddevice as sd
        if not pcm or len(pcm) % 2:
            raise PracticeInputError('题目音频无效；你仍可阅读屏幕上的题目。')
        self._status('正在朗读题目（AI合成语音）· 参考回答不会播放')
        # PCM output from the speech endpoint is 24 kHz, signed 16-bit, mono.
        with sd.RawOutputStream(samplerate=24000, channels=1, dtype='int16') as output:
            for offset in range(0, len(pcm), 960):
                if self.cancelled.is_set():
                    output.abort()
                    return
                output.write(pcm[offset:offset + 960])


class PracticeInputError(ValueError):
    """Only deliberately composed, secret-free messages are shown to the learner."""
