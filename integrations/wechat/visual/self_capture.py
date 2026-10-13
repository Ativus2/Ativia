"""Explicit one-shot self-test observations, separate from normal incoming messages.

The protocol helpers import only the standard library. No self observation is a
VisualEvent, new incoming message, stable platform identity, or send authorization.
"""
from dataclasses import dataclass
import math
import re
import time

SELF_FILL = (53, 210, 141)
SELF_TEST_TTL = 300.0
SELF_TEST_GROUP = "Ativia测试群"


def validate_self_token(token):
    if not isinstance(token, str) or re.fullmatch(r"[0-9A-F]{12}", token) is None:
        raise ValueError("self_test_token_requires_12_uppercase_hex")
    return token


def self_test_text(token):
    return "@Ativia 自测 " + validate_self_token(token) + " 请用一句话回应你好。"


def normalize_self_text(text):
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("self_test_text_invalid")
    return "".join(text.split())


@dataclass(frozen=True)
class SelfTextCandidate:
    text: str
    box: tuple
    confidence: float
    complete: bool = True
    side: str = "right"
    self_state: str = "self"
    kind: str = "text"
    source: str = "local_ocr"


@dataclass(frozen=True)
class SelfScanResult:
    candidates: tuple
    metadata: tuple


@dataclass(frozen=True)
class SelfCaptureResult:
    status: str
    observation: dict | None = None
    reason: str = ""


def find_self_candidates(image, layout, ocr_rows):
    """Only complete right-aligned green text bubbles from the local OCR image."""
    from .layout import (DarkBubbleProfile, _color_components, _color_delta,
                         _near_color, scan_dark_bubbles, extract_messages)
    rows = tuple(ocr_rows)
    green = {box for box, _ in _color_components(image, layout.message, SELF_FILL, 1)}
    scan = scan_dark_bubbles(image, layout, rows, profile=DarkBubbleProfile(self_fill=SELF_FILL))
    detections = tuple(item for item in scan.detections if item.side == "right" and item.box in green)
    extraction = extract_messages(rows, detections, layout, image.size, min_confidence=.90, source="local_ocr")
    candidates, metadata = [], []
    for message in extraction.messages:
        box = message.box
        secondary, total = 0, 0
        for y in range(box.y + 3, box.bottom - 3):
            for x in range(box.x + 3, box.right - 3):
                total += 1
                pixel = image.getpixel((x, y))
                if not _near_color(pixel, SELF_FILL, 2) and 3 <= _color_delta(pixel, SELF_FILL) <= 35:
                    secondary += 1
        fraction = secondary / max(1, total)
        accepted = fraction <= .04 and message.confidence >= .90
        metadata.append(dict(box=[box.x, box.y, box.width, box.height], confidence=message.confidence,
                             accepted=accepted, reason="accepted" if accepted else "self_secondary_panel_or_low_quality",
                             secondary_fraction=round(fraction, 4), side="right", self_state="self"))
        if accepted:
            candidates.append(SelfTextCandidate(message.text, (box.x, box.y, box.width, box.height), message.confidence))
    # Rejection details contain coordinates/quality only, never OCR text or nickname.
    accepted_boxes = {candidate.box for candidate in candidates}
    for row in scan.metadata:
        if row.get("side") == "right" and tuple(row.get("box", ())) not in accepted_boxes:
            metadata.append(dict(row))
    return SelfScanResult(tuple(candidates), tuple(metadata))


class SelfTestCapture:
    def __init__(self, token, issued_at, *, clock=time.time, monotonic=time.monotonic):
        self.token = validate_self_token(token)
        if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)) or not math.isfinite(issued_at):
            raise ValueError("self_test_issue_time_invalid")
        self.clock, self.monotonic = clock, monotonic
        now = clock()
        if issued_at > now:
            raise ValueError("self_test_issue_time_in_future")
        self.issued_at = float(issued_at)
        self.expires_at = self.issued_at + SELF_TEST_TTL
        self.deadline = monotonic() + max(0.0, self.expires_at - now)
        self.captured = False
        self._pending = None
        self._count = 0
        self._last_sequence = None
        self._last_observed = None

    def expired(self):
        if self.captured:return False
        now = self.clock()
        return now < self.issued_at or now >= self.expires_at or self.monotonic() >= self.deadline

    def invalidate(self):
        """Clear stability after pause/navigation; never rearm or extend the token."""
        self._pending = None
        self._count = 0
        self._last_sequence = None
        self._last_observed = None

    def observe(self, candidates, *, binding_id, capture_session_id, capture_epoch,
                frame_sequence, observed_at, group_name=SELF_TEST_GROUP):
        if self.expired():
            self.invalidate()
            return SelfCaptureResult("self_test_expired", reason="self_test_expired")
        if self.captured:
            return SelfCaptureResult("self_test_captured")
        if (group_name != SELF_TEST_GROUP or not isinstance(binding_id, str) or not binding_id or len(binding_id) > 128 or
                not isinstance(capture_session_id, str) or not capture_session_id or len(capture_session_id) > 128 or
                type(capture_epoch) is not int or capture_epoch < 0 or type(frame_sequence) is not int or frame_sequence < 0 or
                isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at) or
                not self.issued_at <= observed_at <= self.clock()):
            self.invalidate()
            return SelfCaptureResult("self_test_waiting", reason="self_test_context_invalid")
        if self._last_sequence is not None and (frame_sequence <= self._last_sequence or observed_at <= self._last_observed):
            return SelfCaptureResult("self_test_waiting", reason="self_test_stale_frame")
        self._last_sequence, self._last_observed = frame_sequence, observed_at
        matches = []
        for candidate in candidates:
            if not isinstance(candidate, SelfTextCandidate):
                continue
            box = candidate.box
            if (candidate.source != "local_ocr" or candidate.side != "right" or candidate.self_state != "self" or
                    candidate.kind != "text" or candidate.complete is not True or
                    isinstance(candidate.confidence, bool) or not isinstance(candidate.confidence, (int, float)) or
                    not math.isfinite(candidate.confidence) or not .90 <= candidate.confidence <= 1 or
                    not isinstance(candidate.text,str) or len(candidate.text)>4000 or
                    not isinstance(box, tuple) or len(box) != 4 or any(type(part) is not int for part in box) or
                    min(box[:2]) < 0 or min(box[2:]) <= 0):
                continue
            if normalize_self_text(candidate.text) == normalize_self_text(self_test_text(self.token)):
                matches.append(candidate)
        if len(matches) != 1:
            self._pending, self._count = None, 0
            return SelfCaptureResult("self_test_waiting", reason="self_test_text_missing_or_ambiguous")
        candidate = matches[0]
        key = (binding_id, capture_session_id, capture_epoch, candidate.box)
        self._count = self._count + 1 if self._pending == key else 1
        self._pending = key
        if self._count < 2:
            return SelfCaptureResult("self_test_waiting", reason="self_test_stabilizing")
        self.captured = True
        return SelfCaptureResult("self_test_captured", dict(
            token=self.token, text=candidate.text, group_name=group_name, binding_id=binding_id,
            capture_session_id=capture_session_id, capture_epoch=capture_epoch,
            frame_sequence=frame_sequence, observed_at=float(observed_at), source="local_ocr",
            self_state="self", side="right", confidence=candidate.confidence,
            box=list(candidate.box), complete=True, kind="text", evidence_kind="real_self_test",
            stable_frames=self._count,
        ))
