"""Korean/English translation and exclusive bottom-caption regressions."""
import asyncio, os, unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from live_translation import (TranslationSettings, translate_audio, consume_segments,
                              CaptionWindow, TranslationController, MAX_SEGMENT_AGE)
from behavior_probes import AppCase

app=QApplication.instance() or QApplication([])

class LanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_recognition_keeps_korean_and_english_and_chinese_translation(self):
        client=Mock();client.audio.transcriptions.create=AsyncMock(return_value=SimpleNamespace(text='금요일에 homework를 제출하세요.'))
        client.chat.completions.create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='请在星期五交作业。'))]))
        settings=TranslationSettings(api_key='offline')
        original,translated=await translate_audio(client,settings,b'\0\0',16000)
        params=client.audio.transcriptions.create.call_args.kwargs
        self.assertNotIn('language',params);self.assertNotIn('prompt',params)
        request=client.chat.completions.create.call_args.kwargs
        self.assertIn('简体中文',request['messages'][0]['content']);self.assertIn('否定',request['messages'][0]['content'])
        self.assertEqual((original,translated),('금요일에 homework를 제출하세요.','请在星期五交作业。'))
    async def test_fixed_languages_and_bounded_previous_context(self):
        client=Mock();client.audio.transcriptions.create=AsyncMock(return_value=SimpleNamespace(text='새 문장'))
        client.chat.completions.create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='新句子'))]))
        for lang in ('ko','en'):
            await translate_audio(client,TranslationSettings(source_language=lang),b'\0\0',16000,[('old'*300,'旧'*900),('previous'*200,'前文'*500)])
            params=client.audio.transcriptions.create.call_args.kwargs
            self.assertEqual(params['language'],lang);self.assertLess(len(params['prompt']),400)
            messages=client.chat.completions.create.call_args.kwargs['messages']
            self.assertEqual(messages[-1]['content'],'새 문장');self.assertEqual(len(messages),6)
            self.assertLessEqual(len(messages[1]['content']),600)

class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def run_queue(self,items,answer):
        pending=asyncio.Queue()
        for item in items:pending.put_nowait(item)
        captions=[];notices=[];stopped=False
        def caption(*pair):
            nonlocal stopped
            captions.append(pair)
            if pending.empty():stopped=True
        with patch('live_translation.monotonic',return_value=100),patch('live_translation.translate_audio',side_effect=answer) as translate:
            await asyncio.wait_for(consume_segments(Mock(),TranslationSettings(),pending,16000,lambda:stopped,caption,notices.append),2)
            contexts=[call.args[4] for call in translate.call_args_list]
        return captions,notices,contexts
    async def test_expired_segment_is_not_sent(self):
        async def answer(*args):return 'new','新'
        captions,notices,contexts=await self.run_queue([(b'old',70,1),(b'new',99,2)],answer)
        self.assertEqual(captions,[('new','新')]);self.assertEqual(len(contexts),1);self.assertTrue(notices)
    async def test_dropped_segment_resets_context(self):
        seen=[]
        async def answer(client,settings,pcm,rate,context):
            seen.append(list(context));return pcm.decode(),pcm.decode()
        await self.run_queue([(b'one',95,1),(b'two',97,2),(b'four',99,4)],answer)
        self.assertEqual(seen,[[],[('one','one')],[]])
    async def test_transient_failure_does_not_stop_next_sentence(self):
        async def answer(client,settings,pcm,rate,context):
            if pcm==b'bad':raise TimeoutError()
            return 'next','下一句'
        captions,notices,_=await self.run_queue([(b'bad',98,1),(b'good',99,2)],answer)
        self.assertEqual(captions,[('next','下一句')]);self.assertTrue(notices)

class CaptionTests(unittest.TestCase):
    def test_recent_translations_remain_in_fixed_bottom_window(self):
        window=CaptionWindow();window.append_caption('한국어','中文翻译')
        area=app.primaryScreen().availableGeometry()
        self.assertLessEqual(abs(window.geometry().center().x()-area.center().x()),1)
        self.assertGreater(window.y(),area.center().y())
        self.assertTrue(window.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(window.testAttribute(Qt.WA_ShowWithoutActivating))
        before=window.geometry()
        window.append_caption('English','英语的新译文')
        self.assertIn('中文翻译',window.text.toPlainText());self.assertIn('English',window.text.toPlainText())
        self.assertEqual(window.geometry(),before);window.close();window.deleteLater()

class ExplanationTests(AppCase):
    def test_typed_learning_answer_uses_dialogue_only(self):
        self.app.control._set_mode('learning')
        with patch('voice_pipeline.respond_to_voice',return_value='只在对话框显示的解释。'):
            self.app._on_chat_submitted('解释这句话');self.wait_idle()
        self.assertIn('只在对话框显示的解释。',self.app.control.history.toPlainText())
        self.assertEqual(self.show_text.call_count,1)
        self.assertFalse(hasattr(self.app,'subtitle'));self.assertFalse(self.app.translation.window.isVisible())
    def test_screen_explanation_does_not_overwrite_translation(self):
        self.app.control._set_mode('learning')
        self.app.translation.window.append_caption('안녕하세요','你好')
        self.manual()
        self.assertIn('안녕하세요',self.app.translation.window.text.toPlainText());self.assertIn('你好',self.app.translation.window.text.toPlainText())
        self.assertIn(self.ask.return_value,self.app.control.history.toPlainText())
        self.assertFalse(hasattr(self.app,'subtitle'))

if __name__=='__main__':unittest.main()
