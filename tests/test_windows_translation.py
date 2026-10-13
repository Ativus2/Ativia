import asyncio
from dataclasses import replace
import sys
from pathlib import Path
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from translation.captions import CaptionAssembler,Caption,normalized,split_sentences
from translation.pipeline import TranslationScheduler,TranslationProvider,ContextBuffer

class AssemblyTests(unittest.TestCase):
    def test_every_sentence_in_one_update_survives(self):
        a=CaptionAssembler(baseline=False)
        rows=a.feed('첫 문장입니다. 두 번째 문장입니다. Third sentence.',10)+a.tick(10.3)
        finals=[r.source for r in rows if r.state=='FINAL']
        self.assertEqual(finals,['첫 문장입니다.','두 번째 문장입니다.','Third sentence.'])
    def test_repeated_dialogue_occurrences_have_different_ids(self):
        a=CaptionAssembler(baseline=False);a.feed('Yes.',10)
        rows=a.feed('Yes. Yes.',11)+a.tick(11.3)
        self.assertEqual(len({r.id for r in rows if r.source=='Yes.'}),2)
        a.feed('',12); rows=a.feed('Yes.',13)
        self.assertEqual(rows[0].id,3)
    def test_punctuation_does_not_change_version_or_repeat_final(self):
        a=CaptionAssembler(baseline=False); a.feed('안녕하세요',0);a.tick(2)
        c=a.window[0];a.update(c,'TRANSLATED','你好')
        events=a.feed('안녕하세요.',3)+a.tick(5)
        self.assertEqual(a.window[0].version,1)
        self.assertEqual(a.window[0].translation,'你好')
        self.assertNotIn('FINAL',[e.state for e in events])
    def test_revisions_invalidate_old_results(self):
        a=CaptionAssembler(baseline=False);a.feed('Submit by Thursday.',0);old=a.window[0].snapshot()
        a.feed('Submit by Friday.',1)
        self.assertFalse(a.current(old));self.assertIsNone(a.update(old,'TRANSLATED','错译'))
    def test_rollover_keeps_sentence_ids(self):
        a=CaptionAssembler(baseline=False);a.feed('First sentence. Second sentence.',0)
        second=a.window[1].id
        a.feed('Second sentence. Third sentence.',1)
        self.assertEqual(a.window[0].id,second);self.assertEqual(a.window[1].id,3)
    def test_resume_skips_existing_partial_and_complete_captions(self):
        a=CaptionAssembler();a.feed('Old sentence. unfinished',0)
        rows=a.feed('Old sentence. unfinished old tail. New sentence.',1)+a.tick(3)
        self.assertEqual([r.source for r in rows if r.state=='FINAL'],['New sentence.'])
    def test_silence_finalizes_partial_but_not_each_character(self):
        a=CaptionAssembler(baseline=False);rows=[]
        for i,text in enumerate(['This','This is','This is still','This is still changing']):rows+=a.feed(text,i*.1)
        self.assertFalse(any(r.state=='FINAL' for r in rows))
        self.assertTrue(any(r.state=='FINAL' for r in a.tick(2)))
    def test_decimals_and_abbreviations_are_not_split(self):
        self.assertEqual(split_sentences('Dr. Kim paid 3.5 dollars. Next.'),['Dr. Kim paid 3.5 dollars.','Next.'])
        self.assertNotEqual(normalized('1.5'),normalized('15'))
    def test_long_running_state_is_bounded(self):
        a=CaptionAssembler(baseline=False)
        for i in range(1200):a.feed('Sentence number %d.'%i,i*2);a.feed('',i*2+1)
        self.assertLessEqual(len(a.records),96)
        self.assertTrue(all(len(p.encode('utf-8'))<=1000 for p in split_sentences('가'*5000+'.')))

class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.a=CaptionAssembler(baseline=False);self.out=[];self.notices=[]
        self.provider=Mock();self.provider.translate=AsyncMock(return_value='译文')
        self.s=TranslationScheduler(self.a,self.provider,self.out.append,self.notices.append)
        self.task=asyncio.create_task(self.s.run())
    async def asyncTearDown(self):
        self.s.close();self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
    def feed(self,text):
        now=monotonic();self.s.accept(self.a.feed(text,now));self.s.accept(self.a.tick(now+.3))
    async def test_slow_api_does_not_block_capture_and_queue_is_bounded(self):
        gate=asyncio.Event()
        async def slow(*args):await gate.wait();return '译文'
        self.provider.translate.side_effect=slow
        self.feed('First sentence.');await asyncio.sleep(.01)
        for i in range(10):self.feed('Different line %d.'%i)
        self.assertLessEqual(len(self.s.pending),3);self.assertTrue(self.a.next_id>=2)
        gate.set();await asyncio.sleep(.03)
    async def test_cancel_does_not_emit_empty_or_late_translation(self):
        entered=asyncio.Event()
        async def slow(*args):entered.set();await asyncio.sleep(60)
        self.provider.translate.side_effect=slow
        self.feed('Korean lesson.');await entered.wait();self.s.close();await asyncio.sleep(.02)
        self.assertFalse(any(c.state=='TRANSLATED' for c in self.out))
    async def test_repeat_final_and_punctuation_one_request(self):
        self.feed('Hello world.');await asyncio.sleep(.03)
        self.feed('Hello world!');await asyncio.sleep(.03)
        self.assertEqual(self.s.metrics['requests'],1)
    async def test_new_occurrence_of_same_words_is_translated(self):
        self.feed('Hello.');await asyncio.sleep(.02)
        self.feed('Hello. Hello.');await asyncio.sleep(.02)
        self.assertEqual(self.s.metrics['requests'],2)
    async def test_revised_inflight_result_is_discarded(self):
        async def answer(c,context):
            if 'Thursday' in c.source:await asyncio.sleep(60)
            return '星期五'
        self.provider.translate.side_effect=answer
        self.feed('Submit by Thursday.');await asyncio.sleep(.01)
        self.feed('Submit by Friday.')
        deadline=monotonic()+1
        while not any(c.state=='TRANSLATED' for c in self.out) and monotonic()<deadline:await asyncio.sleep(.01)
        translations=[c for c in self.out if c.state=='TRANSLATED']
        self.assertEqual(len(translations),1);self.assertIn('Friday',translations[0].source)
    async def test_context_never_contains_current_or_future_or_exceeds_budget(self):
        context=ContextBuffer()
        for i in range(20):context.add(Caption(i,'原文'*100,translation='译文'*100))
        messages=context.messages(19)
        self.assertLessEqual(len(messages),6)
        self.assertLessEqual(sum(len(m['content'].encode()) for m in messages),3000)
    async def test_translation_only_request_has_no_audio_or_persona(self):
        from live_translation import TranslationSettings
        client=Mock();client.chat.completions.create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='星期五提交'))]))
        p=TranslationProvider(client,TranslationSettings(api_key='only-translation'))
        await p.translate(Caption(1,'금요일에 제출하세요.'),ContextBuffer())
        params=client.chat.completions.create.call_args.kwargs
        self.assertEqual(params['messages'][-1]['content'],'금요일에 제출하세요.')
        self.assertNotIn('Ativia',str(params));client.audio.transcriptions.create.assert_not_called()

if __name__=='__main__':unittest.main()
class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_source_close_is_safe_and_closes_stdin(self):
        from translation.windows_source import WindowsLiveCaptionsBackend
        source=WindowsLiveCaptionsBackend()
        process=Mock();process.stdin.wait_closed=AsyncMock();process.wait=AsyncMock(return_value=0)
        source.process=process
        await asyncio.gather(source.close(),source.close())
        process.stdin.close.assert_called_once();process.wait.assert_awaited_once();process.kill.assert_not_called()
    async def test_hung_bridge_only_terminates_owned_helper(self):
        from translation.windows_source import WindowsLiveCaptionsBackend
        from unittest.mock import patch
        source=WindowsLiveCaptionsBackend();process=Mock();process.stdin.wait_closed=AsyncMock();process.wait=AsyncMock(return_value=0);source.process=process
        async def timeout_then_wait(awaitable,seconds):
            await awaitable
            raise asyncio.TimeoutError()
        with patch('translation.windows_source.asyncio.wait_for',side_effect=timeout_then_wait),patch.object(source,'_restore_after_hang') as restore:
            await source.close()
        process.kill.assert_called_once();restore.assert_called_once()

class UserActionTests(unittest.TestCase):
    def test_caption_enters_assistant_only_when_user_chooses_it(self):
        import os,tempfile
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        from assistant_pause import PauseGate
        from controls import ControlBar
        from live_translation import TranslationController,TranslationStore
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            control=ControlBar(text_only=True);gate=PauseGate()
            controller=TranslationController(control,gate,store=TranslationStore(Path(directory)/'translation.json'))
            control.chat_input.setText('我的草稿')
            before=control.history.toPlainText();controller.window.upsert(Caption(1,'안녕하세요',state='TRANSLATED',translation='你好'))
            self.assertEqual(control.history.toPlainText(),before)
            self.assertEqual(control.chat_input.text(),'我的草稿')
            controller._ask('안녕하세요','你好')
            self.assertIn('我的草稿',control.chat_input.text());self.assertIn('안녕하세요',control.chat_input.text())
            self.assertEqual(control.history.toPlainText(),before)
            controller.window.close();control.close();controller.window.deleteLater();control.deleteLater();app.processEvents()
