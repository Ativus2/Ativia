"""Synthetic fixture border tests, not real-window input evidence."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from integrations.wechat.visual.safety import SafetyError
from integrations.wechat.visual.test_actions import recognize_blue_input, read_fixture_json


class FixtureStateReadTests(unittest.TestCase):
    def test_transient_windows_replace_denial_only_retries_read(self):
        with patch.object(Path, "read_text", side_effect=[PermissionError("replace"), '{"nonce":"owned"}']) as reader:
            with patch("integrations.wechat.visual.test_actions.time.sleep") as sleeper:
                self.assertEqual(read_fixture_json(Path("owned-fixture.json")), {"nonce": "owned"})
        self.assertEqual(reader.call_count, 2)
        sleeper.assert_called_once_with(.015)

    def test_persistent_denial_has_small_hard_retry_limit(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")) as reader:
            with patch("integrations.wechat.visual.test_actions.time.sleep") as sleeper:
                with self.assertRaises(PermissionError):
                    read_fixture_json(Path("owned-fixture.json"))
        self.assertEqual(reader.call_count, 8)
        self.assertEqual(sleeper.call_count, 7)


@unittest.skipUnless(importlib.util.find_spec("PIL") and importlib.util.find_spec("numpy"), "fixture image dependencies unavailable")
class FixtureBorderTests(unittest.TestCase):
    def test_complete_border_is_detected_at_two_physical_scales(self):
        from PIL import Image, ImageDraw
        for scale in (1, 2):
            with self.subTest(scale=scale):
                image = Image.new("RGB", (500*scale, 400*scale), "white")
                rect = (20*scale, 80*scale, 480*scale, 300*scale)
                ImageDraw.Draw(image).rectangle((rect[0], rect[1], rect[2]-1, rect[3]-1), outline="#3069ca", width=2*scale)
                self.assertEqual(recognize_blue_input(image), rect)

    def test_missing_border_is_rejected(self):
        from PIL import Image
        with self.assertRaises(SafetyError):
            recognize_blue_input(Image.new("RGB", (800, 600), "white"))

    def test_sparse_blue_glyph_pixels_do_not_expand_border(self):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 100, 789, 499), outline="#3069ca", width=3)
        draw.point((2, 8), fill="#3069ca")
        draw.point((795, 590), fill="#3069ca")
        self.assertEqual(recognize_blue_input(image), (10, 100, 790, 500))

    def test_incomplete_border_is_rejected(self):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 100, 789, 499), outline="#3069ca", width=2)
        draw.rectangle((200, 90, 600, 110), fill="white")
        with self.assertRaises(SafetyError):
            recognize_blue_input(image)


if __name__ == "__main__":
    unittest.main()
