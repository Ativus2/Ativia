"""Manual, session-only group binding after consecutive exact header observations."""
from __future__ import annotations

from dataclasses import dataclass
import uuid

from .types import TARGET_GROUP, VisualFrame, WindowIdentity


def _frame_key(frame):
    return (frame.capture_session_id, frame.epoch, frame.window, frame.image_width,
            frame.image_height, frame.header_text, frame.context_token)


def _valid(frame, target):
    return (frame.capture_ok and not frame.minimized and not frame.paused and
            frame.header_complete and frame.header_text == target and frame.layout_stable)


@dataclass(frozen=True)
class GroupBinding:
    binding_id: str
    group_name: str
    capture_session_id: str
    epoch: int
    window: WindowIdentity
    image_width: int
    image_height: int
    context_token: str
    confirmed_at: float
    identity_quality: str = "manual_session_binding"

    def __post_init__(self):
        if (self.group_name != TARGET_GROUP or self.identity_quality != "manual_session_binding" or
                not isinstance(self.binding_id, str) or not 1 <= len(self.binding_id) <= 128 or
                not isinstance(self.window, WindowIdentity)):
            raise ValueError("invalid_group_binding")

    def matches(self, frame: VisualFrame):
        return (_valid(frame, self.group_name) and
                (frame.capture_session_id, frame.epoch, frame.window, frame.image_width,
                 frame.image_height, frame.context_token) ==
                (self.capture_session_id, self.epoch, self.window, self.image_width,
                 self.image_height, self.context_token))


@dataclass(frozen=True)
class BindingObservation:
    status: str
    consecutive_headers: int
    binding: GroupBinding | None = None
    reason: str = ""


class BindingManager:
    def __init__(self, target=TARGET_GROUP, stable_frames=3):
        if target != TARGET_GROUP:
            raise ValueError("only_the_authorized_group_is_supported")
        if isinstance(stable_frames, bool) or not isinstance(stable_frames, int) or stable_frames < 2:
            raise ValueError("stable_frames")
        self.target = target
        self.stable_frames = stable_frames
        self.binding = None
        self._key = None
        self._count = 0
        self._last_frame = None

    def invalidate(self, reason="explicit_invalidation"):
        self.binding = None
        self._key = None
        self._count = 0
        self._last_frame = None
        return BindingObservation("UNBOUND", 0, reason=reason)

    def observe(self, frame: VisualFrame):
        if not isinstance(frame, VisualFrame):
            raise TypeError("VisualFrame_required")
        if not _valid(frame, self.target):
            return self.invalidate("title_capture_layout_or_pause_invalid")
        key = _frame_key(frame)
        if self.binding is not None and not self.binding.matches(frame):
            self.invalidate("binding_context_changed")
        if self._last_frame is not None and key == self._key and (
                frame.frame_sequence <= self._last_frame.frame_sequence or frame.observed_at <= self._last_frame.observed_at):
            return BindingObservation("STALE_FRAME", self._count, self.binding, "non_increasing_frame")
        if key != self._key:
            self.binding = None
            self._key = key
            self._count = 1
        else:
            self._count = min(self._count + 1, self.stable_frames)
        self._last_frame = frame
        status = "GROUP_BOUND" if self.binding is not None else (
            "READY_FOR_CONFIRMATION" if self._count >= self.stable_frames else "WAITING_TITLE")
        return BindingObservation(status, self._count, self.binding)

    def confirm(self, frame: VisualFrame):
        """Call only after a human confirms the local preview of this current frame.

        The title is not a platform group ID. Equal-title navigation that the
        recognizer cannot detect remains a V1 limitation and requires re-confirmation.
        """
        if self._last_frame != frame or self._key != _frame_key(frame) or self._count < self.stable_frames or not _valid(frame, self.target):
            raise ValueError("fresh_stable_header_and_human_confirmation_required")
        self.binding = GroupBinding(
            uuid.uuid4().hex, self.target, frame.capture_session_id, frame.epoch,
            frame.window, frame.image_width, frame.image_height, frame.context_token, frame.observed_at,
        )
        return self.binding
