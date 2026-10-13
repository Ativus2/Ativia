"""PCM adapter using Ativia's existing sounddevice / soundcard capture mechanisms.
No import of audio.py: its persona/TTS imports must stay outside this pipeline.
"""
import asyncio
import queue
from collections import deque
import threading
from time import monotonic
from .base import ASRError

class AudioCapture:
    def __init__(self, source='microphone', block_ms=20, pre_roll_ms=400):
        self.source = source
        self.sample_rate = 16000
        self.input_channels=1
        self.block_ms=block_ms;self.pre_roll_ms=pre_roll_ms
        self.queue = queue.Queue(maxsize=max(20,2000//block_ms))
        self.preroll=deque(maxlen=max(1,pre_roll_ms//block_ms))
        self.discontinuities=0;self.captured_blocks=0
        self.driver_notices=deque(maxlen=128)
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.failure = False
        self.dropped = 0

    async def prepare(self):
        if self.source == 'microphone':
            def rate():
                import sounddevice as sd
                device=sd.query_devices(kind='input')
                return int(device['default_samplerate']),min(2,int(device['max_input_channels']))
            self.sample_rate,self.input_channels = await asyncio.to_thread(rate)
        return self.sample_rate

    def _put(self, pcm):
        if self.stopped.is_set(): return
        self.preroll.append(pcm);self.captured_blocks+=1
        try: self.queue.put_nowait((pcm, monotonic()))
        except queue.Full:
            self.dropped += 1
            try: self.queue.get_nowait()
            except queue.Empty: pass
            try: self.queue.put_nowait((pcm, monotonic()))
            except queue.Full: pass

    def _record(self):
        try:
            if self.source == 'microphone':
                import sounddevice as sd
                def callback(data, frames, timing, status):
                    if status.input_overflow: self.dropped += 1
                    pcm=bytes(data)
                    if self.input_channels==2:
                        import audioop
                        pcm=audioop.tomono(pcm,2,.5,.5)
                    self._put(pcm)
                with sd.RawInputStream(samplerate=self.sample_rate, channels=self.input_channels, dtype='int16',
                                       blocksize=self.sample_rate*self.block_ms//1000, callback=callback):
                    self.ready.set()
                    self.stopped.wait()
            else:
                import soundcard as sc
                import numpy as np
                speaker = sc.default_speaker()
                if speaker is None: raise RuntimeError('no output device')
                mic = sc.get_microphone(id=speaker.name, include_loopback=True)
                # Keep WASAPI init/read/close on one capture thread.
                with mic.recorder(samplerate=self.sample_rate, channels=1) as recorder:
                    self.ready.set()
                    while not self.stopped.is_set():
                        # Count driver discontinuities without discarding the delivered block.
                        import warnings
                        with warnings.catch_warnings(record=True) as notices:
                            warnings.simplefilter('always',sc.mediafoundation.SoundcardRuntimeWarning)
                            data = recorder.record(numframes=self.sample_rate*self.block_ms//1000)
                        count=sum('discontinuity' in str(w.message).lower() for w in notices)
                        self.discontinuities+=count
                        if count:self.driver_notices.append(dict(timestamp=monotonic(),count=count,captured_blocks=self.captured_blocks))
                        self._put((np.clip(data[:, 0], -1, 1)*32767).astype('<i2').tobytes())
        except Exception:
            self.failure = True
        finally: self.stopped.set()

    async def blocks(self):
        self.thread = threading.Thread(target=self._record, name='ativia-asr-audio', daemon=True)
        self.thread.start()
        while not self.stopped.is_set():
            try: pcm, timestamp = self.queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.005); continue
            if monotonic()-timestamp > 1:
                self.dropped += 1; continue
            yield pcm, timestamp
        if self.failure: raise ASRError('音频采集失败，请检查所选麦克风或系统播放设备。')

    async def stop(self):
        self.stopped.set()
        if self.thread:
            await asyncio.to_thread(self.thread.join, 2)
            if self.thread.is_alive():
                raise ASRError('音频设备尚未释放，请检查设备后重启 Ativia。')
