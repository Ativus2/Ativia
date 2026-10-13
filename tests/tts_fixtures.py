import io
from pathlib import Path
import tempfile
import wave
import numpy as np


def wav_bytes(seconds=.2, amplitude=2000, rate=24000):
    samples = (amplitude * np.sin(2 * np.pi * 440 * np.arange(int(rate * seconds)) / rate)).astype('<i2')
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return stream.getvalue()


def audio_file(case, **kwargs):
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    path = Path(folder.name) / 'speech.wav'
    path.write_bytes(wav_bytes(**kwargs))
    return str(path)
