"""Shared strict PCM16 WAV validation, used after BOTH providers and before playback."""
from dataclasses import dataclass
import io
import os
from pathlib import Path
import struct
import wave

import numpy as np

MAX_AUDIO_BYTES = 32 * 1024 * 1024
MIN_AUDIO_BYTES = 2048
MIN_AUDIO_SECONDS = 0.08


class InvalidAudio(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioInfo:
    size: int
    duration: float
    sample_rate: int
    channels: int
    frames: int
    rms: float


class SpeechAudio(str):
    def __new__(cls, path, *, reply_id, backend, info, spoken_text):
        item = super().__new__(cls, path)
        item.reply_id, item.backend, item.info, item.spoken_text = reply_id, backend, info, spoken_text
        return item


def finalize_openai_streaming_wav(content):
    """Finalize ONLY explicit unknown RIFF/data lengths after a complete HTTP body.

    Ordinary truncated files are never repaired. Local responses do not use this.
    """
    if not isinstance(content, bytes) or len(content) < 44 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        return content
    if struct.unpack_from('<I', content, 4)[0] != 0xffffffff:
        return content
    data = bytearray(content)
    offset, block_align = 12, None
    while offset + 8 <= len(data):
        tag, size = data[offset:offset+4], struct.unpack_from('<I', data, offset+4)[0]
        if tag == b'fmt ' and size >= 16 and offset + 8 + size <= len(data):
            kind, channels, rate, byte_rate, block_align, bits = struct.unpack_from('<HHIIHH', data, offset+8)
            if kind != 1 or bits != 16 or block_align != channels * 2 or byte_rate != rate * block_align:
                raise InvalidAudio('invalid_streaming_pcm_format')
        if tag == b'data':
            actual = len(data) - offset - 8
            if size != 0xffffffff or not block_align or actual % block_align:
                raise InvalidAudio('invalid_streaming_length_sentinel')
            struct.pack_into('<I', data, 4, len(data) - 8)
            struct.pack_into('<I', data, offset+4, actual)
            return bytes(data)
        if offset + 8 + size > len(data):
            raise InvalidAudio('truncated_streaming_wave_header')
        offset += 8 + size + size % 2
    raise InvalidAudio('streaming_wave_missing_data')


def validate_wav_bytes(content):
    if not isinstance(content, (bytes, bytearray)) or not MIN_AUDIO_BYTES <= len(content) <= MAX_AUDIO_BYTES:
        raise InvalidAudio("empty_or_abnormal_byte_length")
    if content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        raise InvalidAudio("not_riff_wave")
    if struct.unpack_from("<I", content, 4)[0] + 8 != len(content):
        raise InvalidAudio("riff_length_mismatch")
    try:
        with wave.open(io.BytesIO(content), "rb") as wav:
            rate, channels, frames = wav.getframerate(), wav.getnchannels(), wav.getnframes()
            if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2 or channels not in (1, 2):
                raise InvalidAudio("unsupported_pcm_format")
            if not 8000 <= rate <= 96000 or frames / rate < MIN_AUDIO_SECONDS:
                raise InvalidAudio("zero_or_unreasonably_short_duration")
            samples = wav.readframes(frames)
            if len(samples) != frames * channels * 2:
                raise InvalidAudio("truncated_pcm_data")
    except (wave.Error, EOFError, struct.error, ValueError) as exc:
        raise InvalidAudio("unparseable_wave") from exc
    pcm = np.frombuffer(samples, dtype="<i2").astype(np.float32)
    # Reject zeros and DC-only files, which decode but produce no speech.
    ac = pcm.reshape(-1, channels)
    ac = ac - ac.mean(axis=0)
    rms = float(np.sqrt(np.mean(ac * ac)))
    if rms < 2 or float(np.max(np.abs(ac))) < 16:
        raise InvalidAudio("silent_or_dc_only_pcm")
    return AudioInfo(len(content), frames / rate, rate, channels, frames, rms)


def validate_audio_file(path):
    if not isinstance(path, (str, os.PathLike)) or not str(path):
        raise InvalidAudio("missing_audio_path")
    try:
        p = Path(path)
        if not p.is_file():
            raise InvalidAudio("audio_file_not_found")
        if not MIN_AUDIO_BYTES <= p.stat().st_size <= MAX_AUDIO_BYTES:
            raise InvalidAudio("empty_or_abnormal_file_size")
        return validate_wav_bytes(p.read_bytes())
    except OSError as exc:
        raise InvalidAudio("audio_file_unreadable") from exc


def delete_audio(path):
    if isinstance(path, (str, os.PathLike)) and str(path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
