"""Provider-neutral, immutable recognition events. Times are monotonic seconds."""
from dataclasses import dataclass, field
from typing import Optional

@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    confidence: Optional[float]
    language: str
    timestamp: float
    provider: str
    segment_id: str
    revision: int = 1
    epoch: int = 0
    speech_final: bool = False
    audio_start: Optional[float] = None
    audio_end: Optional[float] = None
    asr_latency_ms: Optional[float] = None
    # True only for a source that revises the same occurrence cumulatively.
    cumulative: bool = False

    metadata: dict = field(default_factory=dict)
