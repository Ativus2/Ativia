"""Conservative visible-sequence tracking, with deliberate gaps instead of replay.

Only a suffix of the previous visible sequence may map to the new frame's prefix.
Matching uses ordered overlap, text similarity, bubble shape and common displacement.
There is no offline history recovery or platform-level exactly-once guarantee.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
from statistics import median

from .binding import GroupBinding
from .types import TrackingResult, VisualEvent, VisualFrame, VisualMessage


@dataclass
class _Track:
    sequence: int
    observation_id: str
    message: VisualMessage
    first_observed: float
    stable_count: int = 0
    baseline: bool = False
    delivered: bool = False


class VisualMessageTracker:
    def __init__(self, *, stable_frames=2, min_confidence=0.80, ledger_limit=2048,
                 geometry_tolerance=6, max_new_per_frame=8):
        if not isinstance(stable_frames, int) or isinstance(stable_frames, bool) or stable_frames < 2:
            raise ValueError("stable_frames")
        if (isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)) or not 0.5 <= min_confidence <= 1.0 or
                isinstance(ledger_limit, bool) or not isinstance(ledger_limit, int) or ledger_limit < 256):
            raise ValueError("confidence_or_ledger_limit")
        if (isinstance(geometry_tolerance, bool) or not isinstance(geometry_tolerance, int) or not 0 <= geometry_tolerance <= 20 or
                isinstance(max_new_per_frame, bool) or not isinstance(max_new_per_frame, int) or not 1 <= max_new_per_frame <= 32):
            raise ValueError("geometry_or_new_message_limit")
        self.stable_frames = stable_frames
        self.min_confidence = min_confidence
        self.ledger_limit = ledger_limit
        self.geometry_tolerance = geometry_tolerance
        self.max_new_per_frame = max_new_per_frame
        self._session = None
        self._sequence = 0
        self._tracks = []
        self._last_frame = None
        self._binding_id = None
        self._delivered = OrderedDict()
        self._needs_baseline = True

    @property
    def ledger_size(self):
        return len(self._delivered)

    def reset(self, reason="reset"):
        self._tracks = []
        self._last_frame = None
        self._binding_id = None
        self._needs_baseline = True
        return TrackingResult("RESET", reason=reason)

    def _new_track(self, message, frame, *, baseline=False):
        self._sequence += 1
        return _Track(self._sequence, f"{frame.capture_session_id}:t{self._sequence:08d}",
                      message, frame.observed_at, int(self._eligible(message)), baseline)

    def _eligible(self, message):
        return (message.complete and message.confidence >= self.min_confidence and
                message.side == "left" and message.self_state == "incoming" and
                message.kind in {"text", "quote"} and bool(message.body.strip()) and
                (message.kind != "quote" or message.quote_separated))

    def _compatible(self, old, new):
        if (old.side, old.kind, old.self_state) != (new.side, new.kind, new.self_state):
            return False
        if old.speaker_hint and new.speaker_hint and old.speaker_hint != new.speaker_hint:
            return False
        a, b = old.box, new.box
        if abs(a.x - b.x) > self.geometry_tolerance:
            return False
        size_fraction = 0.25 if old.complete and new.complete else 1.0
        if abs(a.width - b.width) > max(10, a.width * size_fraction):
            return False
        if abs(a.height - b.height) > max(10, a.height * size_fraction):
            return False
        if old.text == new.text:
            return True
        # Short strings still tolerate one OCR character changing at the same bubble.
        if not old.text or not new.text:
            return not old.complete or not new.complete
        return SequenceMatcher(None, old.text, new.text, autojunk=False).ratio() >= 0.5

    def _overlap(self, messages):
        previous = self._tracks
        candidates = []
        for start in range(len(previous)):
            count = len(previous) - start
            if count > len(messages):
                continue
            pairs = list(zip(previous[start:], messages[:count]))
            if not all(self._compatible(track.message, message) for track, message in pairs):
                continue
            shifts = [message.box.y - track.message.box.y for track, message in pairs]
            displacement = median(shifts)
            if any(abs(shift - displacement) > self.geometry_tolerance for shift in shifts):
                continue
            added = len(messages) - count
            if displacement > self.geometry_tolerance:
                continue  # Moving old content down means history/navigation may be visible.
            if not added and abs(displacement) > self.geometry_tolerance:
                continue
            if count < min(2, len(previous)):
                continue
            if added > self.max_new_per_frame:
                continue
            exact = sum(track.message.text == message.text for track, message in pairs)
            if (added or abs(displacement) > self.geometry_tolerance) and not exact:
                continue  # A moving sequence needs at least one actual textual anchor.
            # Prefer real textual anchors to a longer run of merely similar OCR.
            # Otherwise "message 001/002/003" scrolling to "002/003/004" would
            # incorrectly keep all three tracks and miss the new last bubble.
            candidates.append((exact, count, start))
        best = max(candidates, default=None)
        return None if best is None else best[1:]

    def _event(self, track, frame, binding):
        message = track.message
        # No hint means a unique per-observation provisional speaker, not a shared Unknown.
        hint = message.speaker_hint or track.observation_id
        digest = hashlib.sha256((frame.capture_session_id + "\0" + binding.binding_id + "\0" + hint).encode()).hexdigest()[:32]
        return VisualEvent(
            observation_id=track.observation_id, binding_id=binding.binding_id,
            group_name=binding.group_name, speaker_session_id="wechat:visual:person:" + digest,
            sender_name=message.sender_name or "未知发言人", text=message.body.strip(),
            observed_at=track.first_observed, capture_session_id=frame.capture_session_id,
            capture_epoch=frame.epoch, sequence=track.sequence, confidence=message.confidence,
            source=message.source,
            identity_quality="visual_session" if message.speaker_hint else "visual_observation",
        )

    def _gap(self, reason):
        self.reset(reason)
        return TrackingResult("GAP", reason=reason)

    def process(self, frame: VisualFrame, binding: GroupBinding | None):
        if not isinstance(frame, VisualFrame):
            raise TypeError("VisualFrame_required")
        if frame.paused:
            self.reset("paused")
            return TrackingResult("PAUSED", reason="paused")
        if not frame.capture_ok or frame.minimized:
            return self._gap("capture_failed_or_minimized")
        if binding is None or not binding.matches(frame):
            self.reset("binding_invalid")
            return TrackingResult("GROUP_UNBOUND", reason="binding_invalid")
        if frame.scroll_detected or not frame.at_bottom or not frame.layout_stable:
            return self._gap("scroll_bottom_or_layout_uncertain")
        if self._session != frame.capture_session_id:
            self.reset("capture_session_changed")
            self._session = frame.capture_session_id
            self._sequence = 0
            self._delivered.clear()
        if self._last_frame is not None and self._binding_id == binding.binding_id:
            if frame.frame_sequence <= self._last_frame.frame_sequence or frame.observed_at <= self._last_frame.observed_at:
                return TrackingResult("STALE_FRAME", reason="non_increasing_frame", visible_tracks=len(self._tracks))
            if frame.epoch != self._last_frame.epoch:
                self.reset("epoch_changed")
        if self._binding_id != binding.binding_id:
            self.reset("binding_changed")
        if self._needs_baseline:
            self._tracks = [self._new_track(message, frame, baseline=True) for message in frame.messages]
            self._last_frame = frame
            self._binding_id = binding.binding_id
            self._needs_baseline = False
            return TrackingResult("BASELINE_COMPLETE", visible_tracks=len(self._tracks))
        if not self._tracks and self._last_frame.empty_chat_confirmed:
            if len(frame.messages) > self.max_new_per_frame:
                return self._gap("empty_chat_burst")
            self._tracks = [self._new_track(message, frame) for message in frame.messages]
        elif not self._tracks and not frame.messages:
            pass
        else:
            overlap = self._overlap(frame.messages)
            if overlap is None:
                return self._gap("insufficient_ordered_overlap")
            count, start = overlap
            tracks = self._tracks[start:]
            for track, message in zip(tracks, frame.messages[:count]):
                if self._eligible(message):
                    track.stable_count = track.stable_count + 1 if message.body == track.message.body and self._eligible(track.message) else 1
                else:
                    track.stable_count = 0
                track.message = message
            tracks.extend(self._new_track(message, frame) for message in frame.messages[count:])
            self._tracks = tracks
        events = []
        rejected = 0
        for track in self._tracks:
            if track.baseline or track.delivered:
                continue
            if not self._eligible(track.message):
                rejected += 1
                continue
            if track.stable_count >= self.stable_frames:
                event = self._event(track, frame, binding)
                track.delivered = True
                self._delivered[event.observation_id] = None
                while len(self._delivered) > self.ledger_limit:
                    self._delivered.popitem(last=False)
                events.append(event)
        self._last_frame = frame
        self._binding_id = binding.binding_id
        return TrackingResult("LISTENER_READY", tuple(events), visible_tracks=len(self._tracks), rejected=rejected)
