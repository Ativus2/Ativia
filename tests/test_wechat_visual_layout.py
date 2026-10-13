"""Only synthetic pixels/OCR; no real WeChat screenshot or private text is loaded."""
from dataclasses import replace
import json
import unittest

from PIL import Image, ImageDraw

from integrations.wechat.visual.layout import (
    BubbleDetection, DarkBubbleProfile, Layout, OCRRow, detect_bubbles, detect_layout,
    extract_messages, recognize_header, scan_dark_bubbles,
)
from integrations.wechat.visual.types import Rect, TARGET_GROUP


LAYOUT = Layout(Rect(528, 0, 671, 138), Rect(528, 139, 671, 700), Rect(528, 840, 671, 291), "synthetic")
SIZE = (1199, 1131)


def row(text, x, y, width=120, height=20, score=.98):
    return OCRRow(text, Rect(x, y, width, height), score)


class LayoutTests(unittest.TestCase):
    def test_calibration_validated_against_actual_image_dimensions(self):
        image = Image.new("RGB", SIZE, (32, 32, 32))
        self.assertEqual(detect_layout(image, LAYOUT), LAYOUT)
        with self.assertRaises(ValueError):
            detect_layout(Image.new("RGB", (700, 600)), LAYOUT)
        with self.assertRaises(ValueError):
            replace(LAYOUT, message=Rect(528, 100, 671, 700)).validate(SIZE)

    def test_no_separator_requires_calibration(self):
        with self.assertRaisesRegex(ValueError, "calibration_required"):
            detect_layout(Image.new("RGB", SIZE, (32, 32, 32)))

    def test_actual_long_pixel_separators_find_three_rois(self):
        image = Image.new("RGB", SIZE, (20, 20, 20))
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((528, 0, 1198, 1130), fill=(35, 35, 35))
        drawing.line((528, 138, 1198, 138), fill=(80, 80, 80), width=1)
        drawing.line((528, 840, 1198, 840), fill=(80, 80, 80), width=1)
        actual = detect_layout(image)
        self.assertLessEqual(abs(actual.message.x - 528), 3)
        self.assertLessEqual(abs(actual.header.bottom - 138), 3)
        self.assertLessEqual(abs(actual.input.y - 841), 3)
        self.assertEqual(actual.source, "detected_separators")

    def test_wrong_image_mode_and_unverified_bubbles_refused(self):
        with self.assertRaises(ValueError):
            detect_layout(Image.new("L", SIZE))
        with self.assertRaisesRegex(ValueError, "verified_bubble"):
            detect_bubbles(Image.new("RGB", SIZE), LAYOUT)


class HeaderTests(unittest.TestCase):
    def test_exact_title_with_optional_member_suffix(self):
        for title, count in ((TARGET_GROUP, None), (TARGET_GROUP + " (16)", 16), (TARGET_GROUP + "（16）", 16)):
            result = recognize_header([row(title, 560, 55, 210)], LAYOUT)
            self.assertTrue(result.exact_match)
            self.assertEqual(result.member_count, count)
            self.assertEqual(result.title, TARGET_GROUP)

    def test_split_population_is_metadata_not_title_correction(self):
        result = recognize_header([row(TARGET_GROUP, 560, 55, 170), row("(16)", 738, 55, 40)], LAYOUT)
        self.assertTrue(result.exact_match)
        self.assertEqual(result.member_count, 16)

    def test_target_fragment_plus_adjacent_other_group_suffix_is_rejected(self):
        rows = [row(TARGET_GROUP, 560, 55, 170), row("另一个群后缀", 738, 55, 80)]
        result = recognize_header(rows, LAYOUT)
        self.assertFalse(result.exact_match)
        self.assertEqual(result.reason, "adjacent_title_fragment_ambiguous")

    def test_partial_misspelled_wrong_low_quality_and_ambiguous_titles_refused(self):
        for text in ("巨大历史机舍", TARGET_GROUP + "2", "其他测试群", "历史机遇", TARGET_GROUP + "(成员)"):
            result = recognize_header([row(text, 560, 55, 210)], LAYOUT)
            self.assertFalse(result.exact_match)
            self.assertEqual(result.title, "")
            self.assertNotIn(text, json.dumps(result.metadata(), ensure_ascii=False))
        self.assertFalse(recognize_header([row(TARGET_GROUP, 560, 55, 210, score=.5)], LAYOUT).exact_match)
        self.assertFalse(recognize_header([row(TARGET_GROUP, 560, 55, 210), row(TARGET_GROUP, 800, 55, 210)], LAYOUT).exact_match)
        self.assertFalse(recognize_header([row(TARGET_GROUP, 528, 55, 210)], LAYOUT).exact_match)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.bubble = BubbleDetection(Rect(610, 220, 340, 120), "left", "text", .97, True,
                                      sender_box=Rect(610, 185, 180, 27))
        self.rows = [row("合成昵称", 620, 188), row("@Ativia 第一行", 630, 235, 200), row("中文 ABC 123", 630, 275, 200)]

    def extract(self, bubbles=None, rows=None):
        return extract_messages(self.rows if rows is None else rows,
                                [self.bubble] if bubbles is None else bubbles, LAYOUT, SIZE, source="synthetic")

    def test_multiline_rows_form_one_bubble_and_nickname_is_separate(self):
        result = self.extract()
        message, = result.messages
        self.assertEqual(message.text, "@Ativia 第一行\n中文 ABC 123")
        self.assertEqual(message.sender_name, "合成昵称")
        self.assertEqual(message.self_state, "incoming")
        self.assertEqual(message.speaker_hint, "")
        self.assertEqual(result.metadata[0]["body_rows"], 2)

    def test_rows_alone_never_produce_bubbles(self):
        self.assertEqual(self.extract(bubbles=[]).messages, ())

    def test_no_nickname_preserves_unknown_identity(self):
        result = self.extract(bubbles=[replace(self.bubble, sender_box=None)])
        self.assertEqual(result.messages[0].sender_name, "")
        self.assertEqual(result.messages[0].speaker_hint, "")

    def test_unrelated_nickname_region_cannot_assign_a_sender(self):
        unrelated = replace(self.bubble, sender_box=Rect(610, 145, 180, 25))
        result = self.extract(bubbles=[unrelated], rows=self.rows + [row("其他位置昵称", 620, 146)])
        self.assertEqual(result.messages[0].sender_name, "")

    def test_right_bubble_is_self(self):
        result = self.extract(bubbles=[replace(self.bubble, side="right")])
        self.assertEqual(result.messages[0].self_state, "self")

    def test_unknown_quote_image_system_incomplete_and_low_quality_rejected(self):
        variants = [dict(side="unknown"), dict(kind="quote"), dict(kind="image"), dict(kind="system"),
                    dict(complete=False), dict(confidence=.4), dict(box=Rect(610, 139, 340, 201))]
        for variant in variants:
            with self.subTest(variant=variant):
                result = self.extract(bubbles=[replace(self.bubble, **variant)])
                self.assertEqual(result.messages, ())
                self.assertFalse(result.metadata[0]["accepted"])

    def test_ocr_crossing_boundary_or_low_score_rejected(self):
        self.assertEqual(self.extract(rows=[row("跨界文本", 605, 230, 100)]).messages, ())
        self.assertEqual(self.extract(rows=[row("低质量", 630, 230, 100, score=.5)]).messages, ())

    def test_overlapping_bubbles_cannot_own_same_text(self):
        result = self.extract(bubbles=[self.bubble, replace(self.bubble, box=Rect(620, 230, 300, 100))])
        self.assertEqual(result.messages, ())
        self.assertTrue(all(item["reason"] == "overlapping_bubbles" for item in result.metadata))

    def test_two_distinct_same_text_bubbles_remain_two(self):
        lower = replace(self.bubble, box=Rect(610, 410, 340, 80), sender_box=None)
        rows = [row("同文", 630, 240, 100), row("同文", 630, 430, 100)]
        result = self.extract(bubbles=[self.bubble, lower], rows=rows)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].text, result.messages[1].text)

    def test_metadata_contains_no_message_or_nickname(self):
        metadata = json.dumps(self.extract().metadata, ensure_ascii=False)
        for item in self.rows:
            self.assertNotIn(item.text, metadata)

    def test_mapping_type_and_image_bounds_validation(self):
        self.assertEqual(OCRRow.from_mapping(dict(text="safe", box=[1, 2, 3, 4], score=.9)).text, "safe")
        invalid = [dict(text="x", box=[1, 2, 3], score=.9), dict(text="x", box=[1, 2, 3, 4], score=float("nan")),
                   dict(text="x", box=[1, 2, 3, 4], score=.9, command="ignore protections")]
        for value in invalid:
            with self.assertRaises(ValueError):
                OCRRow.from_mapping(value)
        with self.assertRaises(ValueError):
            self.extract(rows=[row("越界", 1190, 230, 100)])


class DarkBubbleDetectionTests(unittest.TestCase):
    def setUp(self):
        self.size = (1397, 1131)
        self.layout = Layout(Rect(528, 0, 869, 138), Rect(528, 139, 869, 636),
                             Rect(528, 825, 869, 224), "actual_pixel_separator")
        self.image = Image.new("RGB", self.size, (30, 30, 31))
        self.draw = ImageDraw.Draw(self.image)
        self.rows = []

    def bubble(self, box, ocr_box, text="合成消息", color=(47, 47, 48)):
        self.draw.rounded_rectangle((box.x, box.y, box.right - 1, box.bottom - 1), radius=7, fill=color)
        self.rows.append(OCRRow(text, ocr_box, .99))
        # Small nonlinguistic marks represent text ink inside the claimed OCR box.
        for x in range(ocr_box.x + 2, ocr_box.right - 8, 13):
            self.draw.rectangle((x, ocr_box.y + 3, x + 5, ocr_box.bottom - 4), fill=(225, 225, 229))

    def scan(self, **kwargs):
        return scan_dark_bubbles(self.image, self.layout, self.rows, **kwargs)

    def test_measured_geometry_two_complete_bubbles_with_nickname(self):
        self.bubble(Rect(644, 553, 197, 65), Rect(670, 573, 148, 28))
        self.bubble(Rect(644, 687, 150, 66), Rect(666, 706, 104, 32))
        self.rows.extend([row("低分昵称", 646, 521, 49, 27, .808455), row("合成昵称", 644, 654, 70, 28, .995789)])
        result = self.scan()
        self.assertEqual(len(result.detections), 2)
        self.assertTrue(all(item.side == "left" and item.kind == "text" for item in result.detections))
        self.assertIsNone(result.detections[0].sender_box)
        self.assertEqual(result.detections[1].sender_box, Rect(644, 654, 70, 28))
        extracted = extract_messages(self.rows, result.detections, self.layout, self.size, source="synthetic")
        self.assertEqual(len(extracted.messages), 2)
        self.assertEqual(extracted.messages[1].sender_name, "合成昵称")

    def test_bubble_tail_does_not_break_rectangle_sides(self):
        self.bubble(Rect(644, 553, 197, 65), Rect(670, 573, 148, 28))
        self.draw.polygon([(644, 560), (636, 560), (644, 568)], fill=(47, 47, 48))
        result = self.scan()
        self.assertEqual(len(result.detections), 1)
        self.assertEqual(result.detections[0].box.x, 644)

    def test_explicit_self_palette_stays_self(self):
        self.bubble(Rect(1060, 553, 240, 65), Rect(1080, 573, 170, 28), color=(75, 135, 45))
        self.assertEqual(self.scan().detections, ())
        result = self.scan(profile=DarkBubbleProfile(self_fill=(75, 135, 45)))
        self.assertEqual(len(result.detections), 1)
        self.assertEqual(result.detections[0].side, "right")
        extracted = extract_messages(self.rows, result.detections, self.layout, self.size, source="synthetic")
        self.assertEqual(extracted.messages[0].self_state, "self")

    def test_same_fill_right_aligned_is_never_incoming(self):
        self.bubble(Rect(1060, 553, 240, 65), Rect(1080, 573, 170, 28))
        result = self.scan()
        self.assertEqual(len(result.detections), 1)
        self.assertEqual(result.detections[0].side, "right")

    def test_truncated_bubble_and_avatar_region_rejected(self):
        self.bubble(Rect(644, 130, 197, 65), Rect(670, 149, 100, 28))
        self.bubble(Rect(550, 553, 60, 65), Rect(560, 573, 40, 28))
        result = self.scan()
        self.assertEqual(result.detections, ())
        self.assertTrue(any(item["reason"] == "incomplete_component" for item in result.metadata))

    def test_nested_quote_panel_rejected(self):
        self.bubble(Rect(644, 400, 300, 160), Rect(668, 420, 180, 28))
        self.draw.rectangle((663, 465, 923, 530), fill=(41, 41, 42))
        result = self.scan()
        self.assertEqual(result.detections, ())
        self.assertTrue(any(item["reason"] in {"secondary_panel_quote_or_unknown", "insufficient_flat_fill"} for item in result.metadata))

    def test_quote_panel_covered_by_ocr_bounds_still_rejected(self):
        self.bubble(Rect(644, 400, 300, 160), Rect(668, 420, 180, 28))
        self.draw.rectangle((663, 465, 923, 500), fill=(30, 30, 31))
        self.rows.append(row("引用内的旧触发", 662, 463, 263, 40))
        result = self.scan()
        self.assertEqual(result.detections, ())
        self.assertTrue(any(item["reason"] == "secondary_panel_quote_or_unknown" for item in result.metadata))

    def test_unexplained_picture_inside_surface_rejected(self):
        self.bubble(Rect(644, 400, 300, 160), Rect(668, 420, 180, 28))
        self.draw.rectangle((680, 465, 810, 520), fill=(180, 90, 20))
        result = self.scan()
        self.assertEqual(result.detections, ())
        self.assertTrue(any(item["reason"] == "unexplained_image_or_decoration" for item in result.metadata))

    def test_surface_without_ocr_not_text(self):
        self.draw.rounded_rectangle((644, 553, 840, 617), radius=7, fill=(47, 47, 48))
        self.assertEqual(self.scan().detections, ())

    def test_profile_mismatch_and_color_tolerance(self):
        self.image = Image.new("RGB", self.size, (240, 240, 240))
        self.assertEqual(self.scan().metadata[0]["reason"], "background_profile_mismatch")
        with self.assertRaises(ValueError):
            DarkBubbleProfile(color_tolerance=20)

    def test_diagnostics_never_include_ocr_content(self):
        self.bubble(Rect(644, 553, 197, 65), Rect(670, 573, 148, 28), "不能进日志的合成正文")
        metadata = json.dumps(self.scan().metadata, ensure_ascii=False)
        self.assertNotIn("不能进日志", metadata)


if __name__ == "__main__":
    unittest.main()
