import os,threading,time,unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from performance.scheduler import TaskScheduler
from performance.metrics import Metrics
from sentence_chunks import SentenceChunks
from echo_reference import EchoReference
from screen_events import classify_change
from behavior_probes import AppCase
from speech_activity import Utterance
from tts_events import _safe_details

class SchedulerTests(unittest.TestCase):
    def test_partial_latency_basis_and_unavailable_are_explicit(self):
        from performance.panel import asr_partial_latency
        self.assertEqual(asr_partial_latency({'provider':'windows','asr_latency_ms':None}),'—')
        self.assertEqual(asr_partial_latency({'metadata':{'first_speech_time':2,'first_partial_time':1}}),'—')
        value=asr_partial_latency({'metadata':{'first_speech_time':1,'first_partial_time':1.25}})
        self.assertIn('250.0 ms',value);self.assertIn('estimated speech start',value)

    def test_user_input_cancels_low_priority_without_waiting(self):
        s=TaskScheduler();called=[]
        old=s.admit('vision',cancel=lambda:called.append(True));s.start(old)
        new=s.admit('voice',replace=True);s.start(new)
        self.assertEqual(called,[True]);self.assertFalse(old.valid());self.assertTrue(new.valid())
        self.assertEqual(s.snapshot()['active'][0]['priority'],0)
        self.assertEqual(len(s.snapshot()['cancelled']),1)
    def test_timeout_and_result_guard(self):
        s=TaskScheduler();called=[]
        old=s.admit('text',timeout=-1,cancel=lambda:called.append(True))
        s.expire();self.assertFalse(old.valid());self.assertEqual(called,[True])
    def test_cancel_does_not_touch_independent_asr_or_durable_memory(self):
        s=TaskScheduler();asr=s.admit('asr');memory=s.admit('memory')
        s.admit('text',replace=True)
        self.assertTrue(asr.valid());self.assertTrue(memory.valid())
    def test_chunker_preserves_text_and_does_not_send_each_delta(self):
        c=SentenceChunks();parts=[]
        for delta in ('你好','，我','在。','继续','下一句','。'):parts.extend(c.feed(delta))
        parts.extend(c.feed('',final=True))
        self.assertEqual([t for _,t in parts],['你好，我在。','继续下一句。'])
    def test_log_fields_cannot_accept_private_payloads(self):
        safe=_safe_details(dict(prompt='secret chat',audio_data='base64',authorization='Bearer private',
            api_key='sk-secret',reason='HTTP error Authorization: Bearer sk-secret',duration_ms=10))
        self.assertNotIn('secret',str(safe));self.assertEqual(safe['duration_ms'],10)
    def test_linear_echo_is_removed_user_speech_is_preserved(self):
        rng=np.random.default_rng(21);reference=rng.normal(0,.1,12000).astype(np.float32)
        echo=reference[3200:4800]*.5
        out=EchoReference().filter(echo,reference)
        self.assertLess(np.sqrt(np.mean(out*out)),1e-5)
        user=rng.normal(0,.08,1600).astype(np.float32)
        result=EchoReference().filter(user,reference)
        np.testing.assert_array_equal(user,result)
    def test_slow_native_audio_close_is_off_gui_and_awaitable(self):
        from speech_playback import SpeechPlayer
        from unittest.mock import Mock
        player=SpeechPlayer();release=threading.Event();entered=threading.Event()
        stream=Mock();stream.abort.side_effect=lambda:(entered.set(),release.wait(2))
        player._stream=stream;player._expecting_end=True;player._reply_id='unit-slow-close'
        began=time.perf_counter();player.stop()
        try:
            self.assertLess(time.perf_counter()-began,.1)
            self.assertTrue(entered.wait(1));self.assertTrue(player.has_cleanup())
        finally:
            release.set();player.wait_released(3)
        self.assertFalse(player.has_cleanup());stream.close.assert_called_once()

    def test_vision_gates_small_change_and_class(self):
        meta=dict(window=7,owned=False)
        self.assertEqual(classify_change(0,1,meta,meta,'GAME'),'none')
        self.assertEqual(classify_change(0,65535,meta,meta,'GAME'),'large_visual_change')
        self.assertEqual(classify_change(None,65535,{},meta,'CLASS'),'none')

class ForegroundTests(AppCase):
    def pump(self,predicate,seconds=3):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:self.qt.processEvents();time.sleep(.005)
        self.assertTrue(predicate())
    def test_new_text_while_agent_blocked_is_delivered_and_old_cannot_overwrite(self):
        entered=threading.Event();release=threading.Event()
        def reply(spoken,*a,**k):
            if spoken=='old':entered.set();release.wait(3);return '过时的回复'
            return '当前的新回复'
        with patch('voice_pipeline.respond_to_voice',side_effect=reply):
            self.app._on_chat_submitted('old');self.pump(entered.is_set)
            began=time.monotonic()
            self.app.control.chat_input.setText('new');self.app.control.chat_send_button.click()
            try:
                self.pump(lambda:self.app.control._last_reply=='当前的新回复')
                self.assertLess(time.monotonic()-began,.5)
                self.assertFalse(release.is_set())
            finally:
                release.set();self.pump(lambda:not self.app._retired_voice)
        self.assertEqual(self.app.control._last_reply,'当前的新回复')
    def test_delta_and_sentence_are_delivered_before_final(self):
        started=threading.Event();release=threading.Event()
        def reply(spoken,*a,**kw):
            kw['on_delta']('我在。');started.set();release.wait(3)
            kw['on_delta']('还有下一句。');return '我在。还有下一句。'
        chunks=[]
        self.app.control._on_speak_clicked()
        with patch('voice_pipeline.respond_to_voice',side_effect=reply),patch.object(self.app._speech,'submit',side_effect=lambda job:chunks.append(job)):
            self.app._on_chat_submitted('test')
            try:
                self.pump(lambda:bool(chunks))
                self.assertEqual(self.app.control._last_reply,'我在。')
                self.assertEqual(chunks[0].text,'我在。')
                self.assertFalse(release.is_set())
            finally:release.set();self.wait_idle()
        self.assertEqual(self.app.control._last_reply,'我在。还有下一句。')
        self.assertEqual(len(chunks),2)
        self.assertEqual(chunks[0].parent_id,chunks[1].parent_id)
    def test_new_voice_stops_tts_immediately(self):
        from test_real_use_voice import syllables
        self.app._settings=__import__('dataclasses').replace(self.app._settings,echo_guard=False)
        self.mic.segmented_input=True;self.mic.dropped_chunks=0
        self.mic.read_pending.return_value=[(syllables(),time.monotonic())]
        with patch.object(self.app._speech,'cancel') as cancel:
            self.app._poll_microphone()
            self.assertIn('new_user_input',[c.args[0] for c in cancel.call_args_list])
        self.mic.read_pending.return_value=[]
    def test_slow_capture_is_not_on_qt_thread_and_pause_blocks_result(self):
        entered=threading.Event();release=threading.Event();threads=[]
        old=self.capture.return_value
        def capture():
            threads.append(threading.get_ident());entered.set();release.wait(2);return old
        self.capture.side_effect=capture;self.app._auto=True;self.app._start_auto_observe()
        try:
            self.pump(entered.is_set)
            self.assertNotEqual(threads[0],threading.get_ident())
            self.app._pause_all()
        finally:
            release.set();self.pump(lambda:self.app._capture_probe is None and self.app._resource_change is None)
        self.ask.assert_not_called()

if __name__=='__main__':unittest.main()

