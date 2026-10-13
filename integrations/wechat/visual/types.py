"""Validated, platform-independent observations; no native handles are dereferenced here.

Coordinates are image pixels unless explicitly called screen/client coordinates.
Visual identifiers describe an observation in a capture session, never a WeChat ID.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Literal

TARGET_GROUP = "Ativia测试群"
ALLOWED_SOURCES = frozenset({"local_ocr", "local_template", "vision_test_crop", "synthetic"})


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(name)


def _text(value, name, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value):
        raise ValueError(name)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self):
        for name in ("x", "y"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise ValueError(name)
        _integer(self.width, "width", 1)
        _integer(self.height, "height", 1)

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def inside(self, width, height):
        return self.x >= 0 and self.y >= 0 and self.right <= width and self.bottom <= height


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    process_started: str
    # Client rectangle in screen coordinates: moving/resizing changes the binding.
    client_rect: Rect
    dpi: int

    def __post_init__(self):
        _integer(self.hwnd, "hwnd", 1)
        _integer(self.pid, "pid", 1)
        _integer(self.dpi, "dpi", 1)
        _text(self.process_started, "process_started", 128, required=True)
        if not isinstance(self.client_rect, Rect):
            raise ValueError("client_rect")


@dataclass(frozen=True)
class VisualMessage:
    text: str
    box: Rect
    sender_name: str = ""
    speaker_hint: str = ""
    side: Literal["left", "right", "center", "unknown"] = "unknown"
    kind: Literal["text", "quote", "system", "image", "voice", "file", "unknown"] = "unknown"
    confidence: float = 0.0
    complete: bool = False
    quote_separated: bool = False
    body_text: str | None = None
    self_state: Literal["incoming", "self", "unknown"] = "unknown"
    source: str = "local_ocr"

    def __post_init__(self):
        _text(self.text, "text", 4000)
        _text(self.sender_name, "sender_name", 128)
        _text(self.speaker_hint, "speaker_hint", 256)
        if self.body_text is not None:
            _text(self.body_text, "body_text", 4000)
        if not isinstance(self.box, Rect):
            raise ValueError("box")
        if self.side not in {"left", "right", "center", "unknown"}:
            raise ValueError("side")
        if self.kind not in {"text", "quote", "system", "image", "voice", "file", "unknown"}:
            raise ValueError("kind")
        if self.self_state not in {"incoming", "self", "unknown"}:
            raise ValueError("self_state")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence")
        if type(self.complete) is not bool or type(self.quote_separated) is not bool:
            raise ValueError("message_flags")
        if self.source not in ALLOWED_SOURCES:
            raise ValueError("message_source")

    @property
    def body(self):
        if self.kind == "quote":
            return self.body_text if self.quote_separated and self.body_text is not None else ""
        return self.text if self.kind == "text" else ""


@dataclass(frozen=True)
class VisualFrame:
    capture_session_id: str
    epoch: int
    window: WindowIdentity
    image_width: int
    image_height: int
    observed_at: float
    header_text: str
    messages: tuple[VisualMessage, ...] = ()
    at_bottom: bool = False
    layout_stable: bool = False
    scroll_detected: bool = False
    capture_ok: bool = True
    context_token: str = ""
    frame_sequence: int = 0
    minimized: bool = False
    paused: bool = False
    empty_chat_confirmed: bool = False
    header_complete: bool = False

    def __post_init__(self):
        _text(self.capture_session_id, "capture_session_id", 128, required=True)
        _text(self.header_text, "header_text", 256)
        _text(self.context_token, "context_token", 256)
        for name in ("epoch", "frame_sequence"):
            _integer(getattr(self, name), name)
        for name in ("image_width", "image_height"):
            _integer(getattr(self, name), name, 1)
            if getattr(self, name) > 32768:
                raise ValueError(name)
        if not isinstance(self.window, WindowIdentity):
            raise ValueError("window")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)) or not math.isfinite(self.observed_at):
            raise ValueError("observed_at")
        for name in ("at_bottom", "layout_stable", "scroll_detected", "capture_ok", "minimized", "paused", "empty_chat_confirmed", "header_complete"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(name)
        if not isinstance(self.messages, tuple) or len(self.messages) > 256:
            raise ValueError("messages")
        if any(not isinstance(message, VisualMessage) or not message.box.inside(self.image_width, self.image_height) for message in self.messages):
            raise ValueError("message_bounds")
        if any(a.box.y > b.box.y for a, b in zip(self.messages, self.messages[1:])):
            raise ValueError("message_order")
        if self.empty_chat_confirmed and self.messages:
            raise ValueError("empty_chat_conflict")


def has_explicit_trigger(text: str) -> bool:
    """A case-sensitive displayed text token; never assert native mention metadata."""
    return isinstance(text, str) and bool(re.search(r"(?<![A-Za-z0-9_@])@Ativia(?![A-Za-z0-9_])", text))


@dataclass(frozen=True)
class VisualEvent:
    observation_id: str
    binding_id: str
    group_name: str
    speaker_session_id: str
    sender_name: str
    text: str
    observed_at: float
    capture_session_id: str
    capture_epoch: int
    sequence: int
    confidence: float
    source: str
    identity_quality: str = "visual_session"
    transport: str = "visual"
    id_source: str = "visual_observation"
    timestamp_source: str = "first_observed"
    is_self: bool = False
    mention_kind: str = "displayed_text"

    def __post_init__(self):
        for name in ("observation_id", "binding_id", "speaker_session_id", "capture_session_id"):
            _text(getattr(self, name), name, 256, required=True)
        _text(self.sender_name, "sender_name", 128, required=True)
        _text(self.text, "text", 4000, required=True)
        _integer(self.capture_epoch, "capture_epoch")
        _integer(self.sequence, "sequence", 1)
        if self.group_name != TARGET_GROUP or not self.speaker_session_id.startswith("wechat:visual:person:"):
            raise ValueError("visual_scope_or_identity")
        if self.observation_id != f"{self.capture_session_id}:t{self.sequence:08d}":
            raise ValueError("visual_observation_id")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)) or not math.isfinite(self.observed_at):
            raise ValueError("observed_at")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence")
        if (self.identity_quality not in {"visual_session", "visual_observation"} or self.transport != "visual" or
                self.id_source != "visual_observation" or self.timestamp_source != "first_observed" or
                self.is_self is not False or self.mention_kind != "displayed_text" or self.source not in ALLOWED_SOURCES):
            raise ValueError("visual_event_provenance")

    @property
    def is_at_me(self):
        return has_explicit_trigger(self.text)

    def to_chat_event(self):
        """Keep the established platform/scope; never fabricate wxauto friend fields."""
        from chat_events import ChatEvent
        from ..identity import group_id
        return ChatEvent(
            platform="wechat", conversation_type="group", conversation_id=group_id(self.group_name),
            sender_id=self.speaker_session_id, sender_name=self.sender_name, text=self.text,
            timestamp=self.observed_at, message_id=self.observation_id, is_at_me=self.is_at_me,
            identity_quality=self.identity_quality, source_sequence=self.sequence,
            timestamp_source=self.timestamp_source,
            raw_message={
                "transport": self.transport, "binding_id": self.binding_id,
                "capture_session_id": self.capture_session_id, "capture_epoch": self.capture_epoch,
                "id_source": self.id_source, "source": self.source, "confidence": self.confidence,
                "self_state": "incoming", "mention_kind": self.mention_kind,
                "group_identity_quality": "manual_session_binding",
            },
        )


@dataclass(frozen=True)
class TrackingResult:
    status: str
    events: tuple[VisualEvent, ...] = ()
    reason: str = ""
    visible_tracks: int = 0
    rejected: int = 0
