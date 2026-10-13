"""Offline acceptance checks: decoration, protected content, routing and settings."""
from dataclasses import replace
import math
import os
from pathlib import Path
import random
import tempfile
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import audio
from local_tts_client import LocalTTSError
from runtime_settings import RuntimeSettings, SettingsStore
from speech_quirks import apply_speech_quirk
from tts_fixtures import audio_file
from behavior_probes import AppCase


class SpeechQuirkTests(unittest.TestCase):
    def test_sentence_endings_and_whitespace(self):
        cases = {
            "这波配合真不错。": "这波配合真不错喵。",
            "准备好了吗？": "准备好了吗喵？",
            "我们赢了！": "我们赢了喵！",
            "慢慢来～": "慢慢来喵～",
            "我在这里": "我在这里喵",
            " 好的  \r\n来吧！\n": " 好的喵  \r\n来吧喵！\n",
            "你好?好呀!": "你好喵?好呀喵!",
            "我等你……": "我等你喵……",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(apply_speech_quirk(source, probability=1), expected)

    def test_only_complete_chinese_sentences_are_decorated(self):
        source = "Hello! 你好。안녕하세요! 好的，"
        self.assertEqual(apply_speech_quirk(source, probability=1), "Hello! 你好喵。안녕하세요! 好的，")
        for foreign in ("今日も頑張りましょう。", "오늘은 北京에 있어요."):
            self.assertEqual(apply_speech_quirk(foreign, probability=1), foreign)

    def test_existing_variants_are_not_duplicated(self):
        for source in ("你好喵", "你好喵。", "好呀喵～", "可以喵？", "来了喵！", "喵喵~", "你好喵！再来一局。"):
            with self.subTest(source=source):
                once = apply_speech_quirk(source, probability=1)
                self.assertEqual(apply_speech_quirk(once, probability=1), once)
                self.assertNotIn("喵喵", once.replace("喵喵~", ""))

    def test_switch_zero_probability_and_invalid_values_do_not_draw(self):
        draw = Mock(side_effect=AssertionError("Must not draw"))
        for value in (0, -1, 2, math.nan, math.inf, "0.8", True):
            self.assertEqual(apply_speech_quirk("你好。", probability=value, random_value=draw), "你好。")
        self.assertEqual(apply_speech_quirk("你好。", enabled=False, random_value=draw), "你好。")

    def test_probability_boundary_is_per_eligible_sentence(self):
        draw = Mock(side_effect=[0.7999, 0.8, 0.1])
        self.assertEqual(apply_speech_quirk("好呀。Hello!再来。走吧喵！慢慢来～", random_value=draw),
                         "好呀喵。Hello!再来。走吧喵！慢慢来喵～")
        self.assertEqual(draw.call_count, 3)

    def test_default_probability_is_about_eighty_percent(self):
        output = apply_speech_quirk("再来一局。" * 1000, random_value=random.Random(74).random)
        self.assertTrue(750 <= output.count("喵") <= 850)

    def test_protected_outputs_remain_byte_for_byte_unchanged(self):
        cases = [
            "看看这个链接 https://example.com/中文?q=1。再来吧。",
            "打开 www.example.com。", "网址 example.com。",
            "发到 me@example.com。", "打开 C:\\资料\\笔记.txt。",
            "打开 /home/user/笔记。", "打开 ../笔记。", "运行 ./start。",
            "放在 \\\\server\\资料。", "打开 配置.json。", "保存到 %APPDATA%。",
            "先看这里。\n```python\nprint('你好')\n```\n完成了。",
            "试试 `你好`。", "print('你好')", "say 你好 --voice local",
            "python app.py 中文", "git status 中文", "cd 资料", "Get-Content 中文.txt",
            '{"message":"你好。"}', "message: 你好。", "标题：你好。",
            "<message>你好。</message>", "你好。\n- 项目一。\n- 项目二。",
            "1. 你好。\n2. 再见。", "一、你好。\n二、再见。",
            "姓名,年龄\n小明,十八", "姓名\t年龄\n小明\t十八",
            "    你好。\n    再见。", "答案是 x = 2。", "运行命令行后再来。",
            "请输出 JSON。", '她说：“你好！”', "'你好。'",
        ]
        draw = Mock(side_effect=AssertionError("Protected text must not draw"))
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(apply_speech_quirk(source, random_value=draw), source)

    def test_serious_replies_and_context_are_preserved(self):
        for text in ("这件事需要严肃对待。", "请立即就医。", "我知道你很难过。我会陪着你。", "这可能是诈骗。"):
            self.assertEqual(apply_speech_quirk(text, probability=1), text)
        for context in ("认真回答，别开玩笑。", "我父亲去世了。", "我家人住院了。", "请逐字朗读", "This is a medical question."):
            self.assertEqual(apply_speech_quirk("我在这里陪着你。", context=context, probability=1), "我在这里陪着你。")
        self.assertEqual(apply_speech_quirk("我在这里。", serious=True, probability=1), "我在这里。")

    def test_shared_routing_decorates_once_including_fallback(self):
        settings = replace(RuntimeSettings(), voice_backend="local", speech_quirk_probability=.8)
        fallback = audio_file(self)
        with patch("speech_quirks.random.random", return_value=.2) as draw, \
                patch("local_tts_client.synthesize_local", side_effect=LocalTTSError("unavailable")) as local, \
                patch("audio._synthesize_openai_speech", return_value=fallback) as old:
            self.assertEqual(audio.synthesize_speech("你好。", settings), fallback)
            self.assertEqual(local.call_args.args[0], "你好喵。")
            self.assertEqual(old.call_args.args[0], "你好喵。")
            draw.assert_called_once()

    def test_both_backends_honor_disabled_and_serious(self):
        for backend in ("openai", "local"):
            settings = replace(RuntimeSettings(), voice_backend=backend, speech_quirk_probability=1)
            with patch("local_tts_client.synthesize_local", return_value=audio_file(self)) as local, \
                    patch("audio._synthesize_openai_speech", return_value=audio_file(self)) as old:
                selected = local if backend == "local" else old
                audio.synthesize_speech("你好。", settings)
                self.assertEqual(selected.call_args.args[0], "你好喵。")
                audio.synthesize_speech("你好。", replace(settings, speech_quirk_enabled=False))
                self.assertEqual(selected.call_args.args[0], "你好。")
                audio.synthesize_speech("你好。", settings, serious=True)
                self.assertEqual(selected.call_args.args[0], "你好。")
                audio.synthesize_speech("你好。", settings, context="不要开玩笑")
                self.assertEqual(selected.call_args.args[0], "你好。")

    def test_settings_backward_compatibility_validation_and_persistence(self):
        legacy = RuntimeSettings.parse({"voice_backend": "local", "voice_speed": 1.2})
        self.assertTrue(legacy.speech_quirk_enabled)
        self.assertEqual(legacy.speech_quirk_probability, .8)
        for raw, expected in ((-1, 0), (2, 1), (math.nan, .8), (True, .8), ("0.4", .8)):
            self.assertEqual(RuntimeSettings.parse({"speech_quirk_probability": raw}).speech_quirk_probability, expected)
        self.assertTrue(RuntimeSettings.parse({"speech_quirk_enabled": "false"}).speech_quirk_enabled)
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            custom = replace(legacy, speech_quirk_enabled=False, speech_quirk_probability=.35)
            store.save(custom)
            self.assertEqual(store.load(), custom)


class SpeechQuirkUIAndContextTests(AppCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])

    def test_settings_controls_and_preview_use_unsaved_values(self):
        from voice_settings import VoiceSettingsDialog
        dialog = VoiceSettingsDialog(RuntimeSettings())
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.quirk_probability.value(), 80)
        dialog.quirk_probability.setValue(35)
        seen = []
        dialog.preview_requested.connect(seen.append)
        dialog.preview_button.click()
        self.assertEqual(seen[0].speech_quirk_probability, .35)
        dialog.speech_quirk.setChecked(False)
        self.assertFalse(dialog.quirk_probability.isEnabled())
        self.assertFalse(dialog.current().speech_quirk_enabled)
        self.assertEqual(dialog.current().speech_quirk_probability, .35)
        dialog.speech_quirk.setChecked(True)
        self.assertTrue(dialog.quirk_probability.isEnabled())

    def test_observation_passes_formality_and_keeps_original_subtitle(self):
        from overlay import ObserveWorker
        self.app.control._on_speak_clicked()
        for mode, context, recent, spoken in (("learning", "看看这里", "", "我在这里。"),
                                              ("entertainment", "不要开玩笑", "", "我在这里。"),
                                              ("entertainment", "接着说", "我家人住院了。", "我在这里。"),
                                              ("entertainment", "再来一局", "", "我在这里喵。")):
            with self.subTest(mode=mode, context=context), \
                    patch("overlay.comment_on_screenshot", return_value="我在这里。"), \
                    patch.object(self.app._speech, 'submit') as submit:
                worker = ObserveWorker(b"image", None, None, 16000, False, mode, True, user_command=context)
                worker.settings = replace(RuntimeSettings(), speech_quirk_probability=1)
                worker.recent_context = recent
                shown = []
                worker.finished_result.connect(lambda text, path: shown.append(text))
                worker.run()
                self.app._worker = worker
                self.app._on_comment(shown[0], '')
                self.app._worker = None
                job = submit.call_args.args[0]
                self.assertEqual(apply_speech_quirk(job.text, serious=job.serious, context=job.context, probability=1), spoken)
                self.assertEqual(shown, ["我在这里。"])

    def test_voice_input_passes_formality_and_keeps_original_text(self):
        from voice_pipeline import VoiceWorker
        self.app.control._on_speak_clicked()
        for mode, context, recent, spoken in (("learning", "看看这里", "", "我在这里。"),
                                              ("entertainment", "我很难过", "", "我在这里。"),
                                              ("entertainment", "接着说", "我家人住院了。", "我在这里。"),
                                              ("entertainment", "再来一局", "", "我在这里喵。")):
            with self.subTest(mode=mode, context=context), \
                    patch("voice_pipeline.transcribe_speech", return_value=context), \
                    patch("voice_pipeline.respond_to_voice", return_value="我在这里。"), \
                    patch.object(self.app._speech, 'submit') as submit:
                utterance = SimpleNamespace(samples=None, speech_end=monotonic(), detected_at=monotonic())
                worker = VoiceWorker(utterance, replace(RuntimeSettings(), speech_quirk_probability=1), recent, "", True, mode)
                shown = []
                worker.text_ready.connect(shown.append)
                worker.run()
                self.app._voice_worker = worker
                self.app._on_voice_text(worker, shown[0])
                self.app._voice_worker = None
                job = submit.call_args.args[0]
                self.assertEqual(apply_speech_quirk(job.text, serious=job.serious, context=job.context, probability=1), spoken)
                self.assertEqual(shown, ["我在这里。"])


if __name__ == "__main__":
    unittest.main()
