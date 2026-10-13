"""Conservative V1 ROI/header/bubble extraction from local image observations.

An OCR line is never a message. A verified bubble boundary must own every body row.
All diagnostic metadata is text-free; callers must not log OCRRow or VisualMessage.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
import math
import re

from .types import Rect, TARGET_GROUP, VisualMessage


def _contains(outer, inner):
    return (inner.x >= outer.x and inner.y >= outer.y and
            inner.right <= outer.right and inner.bottom <= outer.bottom)


def _intersects(a, b):
    return min(a.right, b.right) > max(a.x, b.x) and min(a.bottom, b.bottom) > max(a.y, b.y)


def _finite_score(value):
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value) and 0 <= value <= 1


@dataclass(frozen=True)
class Layout:
    header: Rect
    message: Rect
    input: Rect
    source: str = "calibrated"

    def validate(self, image_size):
        width, height = image_size
        if (isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or
                not isinstance(height, int) or width < 100 or height < 100):
            raise ValueError("image_size")
        if self.source not in {"calibrated", "actual_pixel_separator", "detected_separators", "synthetic"}:
            raise ValueError("layout_source")
        boxes = (self.header, self.message, self.input)
        if any(not isinstance(box, Rect) or not box.inside(width, height) for box in boxes):
            raise ValueError("layout_bounds")
        if not (self.header.bottom <= self.message.y and self.message.bottom <= self.input.y):
            raise ValueError("layout_vertical_order")
        if any(_intersects(a, b) for index, a in enumerate(boxes) for b in boxes[index + 1:]):
            raise ValueError("layout_overlap")
        if self.message.height < 30 or self.message.width < 80:
            raise ValueError("message_roi_too_small")
        return self

    def metadata(self):
        return {name: [getattr(self, name).x, getattr(self, name).y,
                       getattr(self, name).width, getattr(self, name).height]
                for name in ("header", "message", "input")} | {"source": self.source}


@dataclass(frozen=True)
class OCRRow:
    text: str
    box: Rect
    score: float

    def __post_init__(self):
        if not isinstance(self.text, str) or len(self.text) > 4000 or not isinstance(self.box, Rect) or not _finite_score(self.score):
            raise ValueError("invalid_ocr_row")

    @classmethod
    def from_mapping(cls, row):
        if not isinstance(row, dict) or set(row) - {"text", "box", "score"}:
            raise ValueError("ocr_row_schema")
        box = row.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError("ocr_box")
        return cls(row.get("text"), Rect(*box), row.get("score"))


@dataclass(frozen=True)
class HeaderRecognition:
    exact_match: bool
    complete: bool
    member_count: int | None
    confidence: float
    reason: str
    # This is only populated with the authorized constant, never a wrong-group title.
    title: str = ""

    def metadata(self):
        return dict(exact_match=self.exact_match, complete=self.complete,
                    member_count=self.member_count, confidence=self.confidence, reason=self.reason)


def recognize_header(rows, layout, *, min_confidence=.85):
    """Accept only the exact target, with an optional separate trailing member count."""
    if not _finite_score(min_confidence):
        raise ValueError("min_confidence")
    rows = tuple(rows)
    if len(rows) > 2048:
        raise ValueError("too_many_ocr_rows")
    pattern = re.compile(re.escape(TARGET_GROUP) + r"(?:\s*[（(]([0-9]{1,5})[）)])?")
    matches = []
    for row in rows:
        if not isinstance(row, OCRRow):
            raise ValueError("OCRRow_required")
        if not _contains(layout.header, row.box):
            continue
        match = pattern.fullmatch(row.text.strip())
        if match and row.score >= min_confidence:
            # Text touching a crop boundary could be truncated even if OCR returned a match.
            complete = (row.box.x > layout.header.x and row.box.y > layout.header.y and
                        row.box.right < layout.header.right and row.box.bottom < layout.header.bottom)
            members = int(match.group(1)) if match.group(1) is not None else None
            matches.append((row, complete, members))
    if len(matches) != 1:
        return HeaderRecognition(False, False, None, 0.0, "header_missing_or_ambiguous")
    row, complete, members = matches[0]
    if not complete:
        return HeaderRecognition(False, False, None, row.score, "header_crop_boundary")
    for candidate in rows:
        if candidate is row or not _contains(layout.header, candidate.box) or not candidate.text.strip():
            continue
        vertical_overlap = min(row.box.bottom, candidate.box.bottom) - max(row.box.y, candidate.box.y)
        adjacent_right = candidate.box.x >= row.box.right and candidate.box.x - row.box.right <= max(row.box.height * 3, 24)
        adjacent_left = candidate.box.right <= row.box.x and row.box.x - candidate.box.right <= max(row.box.height * 2, 16)
        count_suffix = adjacent_right and re.fullmatch(r"[（(]([0-9]{1,5})[）)]", candidate.text.strip())
        if vertical_overlap >= min(row.box.height, candidate.box.height) * .5 and (adjacent_right or adjacent_left) and not count_suffix:
            return HeaderRecognition(False, False, None, min(row.score, candidate.score), "adjacent_title_fragment_ambiguous")
    # Some local OCR providers split the population suffix into its own row.
    if members is None:
        count_rows = []
        for candidate in rows:
            if not _contains(layout.header, candidate.box) or candidate.score < min_confidence:
                continue
            count = re.fullmatch(r"[（(]([0-9]{1,5})[）)]", candidate.text.strip())
            if count and candidate.box.x >= row.box.right and candidate.box.x - row.box.right <= max(row.box.height * 3, 24) and abs(candidate.box.y - row.box.y) <= row.box.height:
                count_rows.append(int(count.group(1)))
        if len(count_rows) == 1:
            members = count_rows[0]
        elif len(count_rows) > 1:
            return HeaderRecognition(False, False, None, row.score, "member_suffix_ambiguous")
    return HeaderRecognition(True, True, members, row.score, "exact_title", TARGET_GROUP)


@dataclass(frozen=True)
class BubbleDetection:
    box: Rect
    side: str
    kind: str
    confidence: float
    complete: bool
    sender_box: Rect | None = None
    speaker_hint: str = ""

    def __post_init__(self):
        if (not isinstance(self.box, Rect) or self.side not in {"left", "right", "unknown"} or
                self.kind not in {"text", "quote", "image", "system", "unknown"} or
                not _finite_score(self.confidence) or type(self.complete) is not bool or
                (self.sender_box is not None and not isinstance(self.sender_box, Rect)) or
                not isinstance(self.speaker_hint, str) or len(self.speaker_hint) > 256):
            raise ValueError("invalid_bubble_detection")


@dataclass(frozen=True)
class ExtractionResult:
    messages: tuple[VisualMessage, ...]
    metadata: tuple[dict, ...]


def _body_text(rows):
    """Reassemble OCR segments into geometric lines within one established bubble."""
    lines = []
    for row in sorted(rows, key=lambda value: (value.box.y, value.box.x)):
        if lines and abs(row.box.y - median(item.box.y for item in lines[-1])) <= max(3, row.box.height * .45):
            lines[-1].append(row)
        else:
            lines.append([row])
    return "\n".join(" ".join(item.text.strip() for item in sorted(line, key=lambda value: value.box.x) if item.text.strip()) for line in lines).strip()


def extract_messages(rows, bubbles, layout, image_size, *, min_confidence=.80, source="local_ocr"):
    """Map complete text bubbles to observations; reject unsupported and ambiguous blocks."""
    layout.validate(image_size)
    if not _finite_score(min_confidence):
        raise ValueError("min_confidence")
    rows = tuple(rows)
    bubbles = tuple(bubbles)
    if len(rows) > 2048 or len(bubbles) > 256 or any(not isinstance(row, OCRRow) or not row.box.inside(*image_size) for row in rows):
        raise ValueError("ocr_rows_or_bounds")
    if any(not isinstance(bubble, BubbleDetection) for bubble in bubbles):
        raise ValueError("BubbleDetection_required")
    messages, metadata = [], []
    for index, bubble in enumerate(bubbles):
        reason = "accepted"
        body_rows = []
        if not _contains(layout.message, bubble.box) or not bubble.box.inside(*image_size):
            reason = "outside_message_roi"
        elif not bubble.complete or bubble.box.y <= layout.message.y or bubble.box.bottom >= layout.message.bottom:
            reason = "incomplete_bubble"
        elif bubble.side == "unknown" or bubble.kind != "text":
            reason = "unsupported_or_ambiguous_bubble"
        elif bubble.confidence < min_confidence:
            reason = "low_bubble_confidence"
        elif any(index != other_index and _intersects(bubble.box, other.box) for other_index, other in enumerate(bubbles)):
            reason = "overlapping_bubbles"
        else:
            touching = [row for row in rows if row.text.strip() and _intersects(bubble.box, row.box)]
            if any(not _contains(bubble.box, row.box) for row in touching):
                reason = "ocr_crosses_bubble_boundary"
            elif not touching:
                reason = "no_text_in_bubble"
            elif any(row.score < min_confidence for row in touching):
                reason = "low_body_confidence"
            else:
                body_rows = touching
        sender = ""
        if reason == "accepted":
            text = _body_text(body_rows)
            if not text or len(text) > 4000:
                reason = "body_length"
            else:
                # Missing or ambiguous nickname is permitted as a unique temporary person.
                if (bubble.side == "left" and bubble.sender_box is not None and _contains(layout.message, bubble.sender_box) and
                        bubble.sender_box.bottom <= bubble.box.y and
                        bubble.box.y - bubble.sender_box.bottom <= max(12, bubble.sender_box.height) and
                        abs(bubble.sender_box.x - bubble.box.x) <= max(12, bubble.sender_box.height)):
                    names = [row for row in rows if _contains(bubble.sender_box, row.box) and row.score >= .85 and row.text.strip()]
                    if len(names) == 1 and len(names[0].text.strip()) <= 128:
                        sender = names[0].text.strip()
                messages.append(VisualMessage(
                    text=text, box=bubble.box, sender_name=sender,
                    speaker_hint=bubble.speaker_hint, side=bubble.side, kind="text",
                    confidence=min([bubble.confidence] + [row.score for row in body_rows]),
                    complete=True, self_state="self" if bubble.side == "right" else "incoming", source=source,
                ))
        metadata.append(dict(box=[bubble.box.x, bubble.box.y, bubble.box.width, bubble.box.height],
                             side=bubble.side, kind=bubble.kind, confidence=bubble.confidence,
                             accepted=reason == "accepted", reason=reason,
                             body_rows=len(body_rows), sender_visible=bool(sender)))
    return ExtractionResult(tuple(sorted(messages, key=lambda value: value.box.y)), tuple(metadata))


def _rgb_image(image):
    if getattr(image, "mode", None) != "RGB" or not hasattr(image, "getpixel"):
        raise ValueError("PIL_RGB_image_required")
    width, height = image.size
    if width < 300 or height < 300 or width > 16384 or height > 16384:
        raise ValueError("unsupported_image_size")
    return width, height


def _color_delta(a, b):
    return sum(abs(left - right) for left, right in zip(a, b)) / 3


def detect_layout(image, calibration=None):
    """Locate actual long separators; failure requests calibration instead of guessing.

    Search ranges describe the current three-column desktop layout, not universal DPI
    support. The selected coordinates still come from pixels, never a fixed 1280x720.
    """
    width, height = _rgb_image(image)
    if calibration is not None:
        if not isinstance(calibration, Layout):
            raise ValueError("Layout_calibration_required")
        return calibration.validate(image.size)
    ys = range(int(height * .25), int(height * .8), max(1, height // 45))
    vertical = []
    for x in range(int(width * .35), int(width * .65)):
        changes = [_color_delta(image.getpixel((x - 2, y)), image.getpixel((x + 2, y))) for y in ys]
        coverage = sum(value >= 5 for value in changes) / max(1, len(changes))
        if coverage >= .8:
            vertical.append((coverage, median(changes), x))
    if not vertical:
        raise ValueError("chat_column_separator_not_found_calibration_required")
    _, _, left = max(vertical)
    xs = range(left + max(8, width // 100), width - max(8, width // 100), max(1, (width - left) // 50))
    def horizontal(start, end):
        candidates = []
        for y in range(int(height * start), int(height * end)):
            changes = [_color_delta(image.getpixel((x, y - 2)), image.getpixel((x, y + 2))) for x in xs]
            # Thin separator lines may have equal-color regions on both sides.
            lines = [max(_color_delta(image.getpixel((x, y)), image.getpixel((x, y - 2))),
                         _color_delta(image.getpixel((x, y)), image.getpixel((x, y + 2)))) for x in xs]
            values = [max(a, b) for a, b in zip(changes, lines)]
            coverage = sum(value >= 5 for value in values) / max(1, len(values))
            if coverage >= .8:
                candidates.append((coverage, median(values), y))
        if not candidates:
            raise ValueError("horizontal_separator_not_found_calibration_required")
        return max(candidates)[2]
    header_bottom = horizontal(.07, .23)
    input_top = horizontal(.56, .9)
    return Layout(Rect(left, 0, width - left, header_bottom),
                  Rect(left, header_bottom + 1, width - left, input_top - header_bottom - 1),
                  Rect(left, input_top + 1, width - left, height - input_top - 1),
                  "detected_separators").validate(image.size)


def detect_bubbles(image, layout, *, calibrated_detections=None):
    """Require externally verified boundaries until the real-image detector is validated.

    Exposing this explicit limitation is safer than labeling arbitrary OCR/image blocks
    as chat bubbles. A screenshot recognizer can supply BubbleDetection objects here.
    """
    _rgb_image(image)
    layout.validate(image.size)
    if calibrated_detections is None:
        raise ValueError("verified_bubble_boundaries_required")
    detections = tuple(calibrated_detections)
    if len(detections) > 256 or any(not isinstance(item, BubbleDetection) or not _contains(layout.message, item.box) for item in detections):
        raise ValueError("calibrated_bubble_bounds")
    return detections


@dataclass(frozen=True)
class DarkBubbleProfile:
    """Local profile measured on dark WeChat at DPI 168, not a universal theme model."""
    background: tuple[int, int, int] = (30, 30, 31)
    incoming_fill: tuple[int, int, int] = (47, 47, 48)
    self_fill: tuple[int, int, int] | None = None
    color_tolerance: int = 1
    dpi: int = 168
    min_fill_ratio: float = .60
    profile_id: str = "wechat-dark-dpi168-local-v1"

    def __post_init__(self):
        for color in (self.background, self.incoming_fill, self.self_fill):
            if color is not None and (not isinstance(color, tuple) or len(color) != 3 or any(type(part) is not int or not 0 <= part <= 255 for part in color)):
                raise ValueError("profile_color")
        if type(self.color_tolerance) is not int or not 0 <= self.color_tolerance <= 2 or type(self.dpi) is not int or not 72 <= self.dpi <= 384:
            raise ValueError("profile_tolerance_or_dpi")
        if not _finite_score(self.min_fill_ratio) or self.min_fill_ratio < .5:
            raise ValueError("profile_fill_ratio")


@dataclass(frozen=True)
class BubbleScanResult:
    detections: tuple[BubbleDetection, ...]
    metadata: tuple[dict, ...]
    profile_id: str


def _near_color(pixel, color, tolerance):
    return max(abs(pixel[index] - color[index]) for index in range(3)) <= tolerance


def _expanded_contains(box, x, y, padding=3):
    return box.x - padding <= x < box.right + padding and box.y - padding <= y < box.bottom + padding


def _color_components(image, roi, color, tolerance):
    """Four-connected fill pixels. Text holes remain within the same outer component."""
    pixels = image.crop((roi.x, roi.y, roi.right, roi.bottom)).tobytes()
    width, height = roi.width, roi.height
    if width * height > 4_000_000:
        raise ValueError("message_roi_exceeds_local_component_budget")
    mask = bytearray(width * height)
    r, g, b = color
    for index in range(width * height):
        offset = index * 3
        if abs(pixels[offset] - r) <= tolerance and abs(pixels[offset + 1] - g) <= tolerance and abs(pixels[offset + 2] - b) <= tolerance:
            mask[index] = 1
    components = []
    for seed in range(len(mask)):
        if not mask[seed]:
            continue
        mask[seed] = 0
        pending = [seed]
        count, min_x, min_y, max_x, max_y = 0, width, height, 0, 0
        row_edges = {}
        while pending:
            index = pending.pop()
            y, x = divmod(index, width)
            count += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            prior = row_edges.get(y)
            row_edges[y] = (x, x) if prior is None else (min(prior[0], x), max(prior[1], x))
            for candidate in ((index - 1 if x else -1), (index + 1 if x + 1 < width else -1),
                              (index - width if y else -1), (index + width if y + 1 < height else -1)):
                if candidate >= 0 and mask[candidate]:
                    mask[candidate] = 0
                    pending.append(candidate)
        if count >= 100:
            # Use the central-height sides to omit a small speech-bubble tail.
            central = [edge for y, edge in row_edges.items() if min_y + (max_y - min_y) * .2 <= y <= max_y - (max_y - min_y) * .2]
            if central:
                min_x, max_x = int(median(edge[0] for edge in central)), int(median(edge[1] for edge in central))
            components.append((Rect(roi.x + min_x, roi.y + min_y, max_x - min_x + 1, max_y - min_y + 1), count))
    return components


def scan_dark_bubbles(image, layout, ocr_rows, *, profile=None):
    """Detect only complete, OCR-explained text surfaces under an explicit local palette.

    Images, nested quote panels, truncated bubbles, wrong alignments and unexplained
    visual content are rejected. A picture perfectly imitating text-bubble pixels is
    not distinguishable by these local visual features; real sample validation remains
    necessary. Unknown self colors are never guessed or treated as incoming.
    """
    _rgb_image(image)
    layout.validate(image.size)
    profile = DarkBubbleProfile() if profile is None else profile
    if not isinstance(profile, DarkBubbleProfile):
        raise ValueError("DarkBubbleProfile_required")
    rows = tuple(ocr_rows)
    if len(rows) > 2048 or any(not isinstance(row, OCRRow) or not row.box.inside(*image.size) for row in rows):
        raise ValueError("ocr_rows_or_bounds")
    roi = layout.message
    scale = profile.dpi / 168
    # Verify a background border rather than accepting the palette on a light theme.
    samples = [image.getpixel((roi.x + 4, y)) for y in range(roi.y + 4, roi.bottom - 4, max(1, roi.height // 32))]
    if not samples or sum(_near_color(pixel, profile.background, 2) for pixel in samples) / len(samples) < .8:
        return BubbleScanResult((), (dict(reason="background_profile_mismatch", accepted=False),), profile.profile_id)
    detections, diagnostics = [], []
    colors = [(profile.incoming_fill, "incoming_palette")]
    if profile.self_fill is not None and profile.self_fill != profile.incoming_fill:
        colors.append((profile.self_fill, "self_palette"))
    for color, palette_role in colors:
        for box, count in _color_components(image, roi, color, profile.color_tolerance):
            fill_ratio = min(1.0, count / (box.width * box.height))
            reason, side = "accepted", "unknown"
            left_margin, right_margin = box.x - roi.x, roi.right - box.right
            left_aligned = 65 * scale <= left_margin <= 165 * scale
            right_aligned = 55 * scale <= right_margin <= 165 * scale
            if palette_role == "self_palette":
                side = "right" if right_aligned else "unknown"
            elif left_aligned and not right_aligned:
                side = "left"
            elif right_aligned and not left_aligned:
                side = "right"
            complete = (box.x > roi.x + 2 and box.right < roi.right - 2 and box.y > roi.y + 2 and box.bottom < roi.bottom - 2)
            if not complete:
                reason = "incomplete_component"
            elif not (32 * scale <= box.width <= roi.width * .86 and 24 * scale <= box.height <= min(420 * scale, roi.height * .7)):
                reason = "component_dimensions"
            elif side == "unknown":
                reason = "alignment_ambiguous"
            elif fill_ratio < profile.min_fill_ratio:
                reason = "insufficient_flat_fill"
            touching = [row for row in rows if row.text.strip() and _intersects(box, row.box)]
            if reason == "accepted" and (not touching or any(not _contains(box, row.box) or row.score < .80 for row in touching)):
                reason = "body_ocr_missing_low_quality_or_crossing"
            edge_ratio, unexplained_fraction, secondary_fraction = 0.0, 0.0, 0.0
            if reason == "accepted":
                inset = max(2, round(3 * scale))
                xs = range(box.x + box.width // 5, box.right - box.width // 5, max(1, box.width // 24))
                ys = range(box.y + box.height // 5, box.bottom - box.height // 5, max(1, box.height // 20))
                edge_points = ([(x, box.y + inset) for x in xs] + [(x, box.bottom - inset - 1) for x in xs] +
                               [(box.x + inset, y) for y in ys] + [(box.right - inset - 1, y) for y in ys])
                edge_ratio = sum(_near_color(image.getpixel(point), color, profile.color_tolerance + 1) for point in edge_points) / max(1, len(edge_points))
                if edge_ratio < .85:
                    reason = "open_or_irregular_surface"
            if reason == "accepted":
                unexplained, secondary, total = 0, 0, 0
                for y in range(box.y + 2, box.bottom - 2):
                    for x in range(box.x + 2, box.right - 2):
                        total += 1
                        pixel = image.getpixel((x, y))
                        if _near_color(pixel, color, profile.color_tolerance + 1):
                            continue
                        explained = any(_expanded_contains(row.box, x, y) for row in touching)
                        delta = _color_delta(pixel, color)
                        # A nested gray quote panel remains a panel even where OCR
                        # words cover it. Count its surface independently of ink boxes.
                        if 3 <= delta <= 22 and max(pixel) - min(pixel) <= 4:
                            secondary += 1
                        if not explained and delta >= 3 and not _near_color(pixel, profile.background, 2):
                            unexplained += 1
                unexplained_fraction = unexplained / max(1, total)
                secondary_fraction = secondary / max(1, total)
                if secondary_fraction > .04:
                    reason = "secondary_panel_quote_or_unknown"
                elif unexplained_fraction > .025:
                    reason = "unexplained_image_or_decoration"
            sender_box = None
            if reason == "accepted" and side == "left":
                names = [row for row in rows if _contains(roi, row.box) and row.score >= .85 and row.text.strip() and
                         0 <= box.y - row.box.bottom <= max(12, row.box.height) and
                         abs(row.box.x - box.x) <= max(16, row.box.height) and len(row.text.strip()) <= 128]
                if len(names) == 1:
                    sender_box = names[0].box
            if reason == "accepted":
                confidence = min(.98, min(row.score for row in touching), .85 + .13 * min(fill_ratio, edge_ratio))
                detections.append(BubbleDetection(box, side, "text", confidence, True, sender_box))
            diagnostics.append(dict(box=[box.x, box.y, box.width, box.height], side=side,
                                    kind="text" if reason == "accepted" else "unknown", accepted=reason == "accepted",
                                    reason=reason, fill_ratio=round(fill_ratio, 4), edge_ratio=round(edge_ratio, 4),
                                    unexplained_fraction=round(unexplained_fraction, 4),
                                    secondary_fraction=round(secondary_fraction, 4), ocr_rows=len(touching)))
    return BubbleScanResult(tuple(sorted(detections, key=lambda item: item.box.y)), tuple(diagnostics), profile.profile_id)
