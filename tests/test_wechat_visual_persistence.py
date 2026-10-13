"""Real local Memory backend, synthetic events/model/sends, isolated databases."""
from dataclasses import replace
import time
import threading
import unittest

from integrations.wechat.visual.send import SendReply
from memory.gate import MemoryGate
from memory.memory_candidate import MemoryCandidateEvaluator
import test_wechat_visual_service as fixture


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisualServiceTests();self.f.setUp()
        self.f.session.close()
        self.s=self.f.make_session(model_enabled=True,continuous_model=True)
        self.f.session=self.s;self.s.bind('manual-test-binding','capture-test',0)
    def tearDown(self):self.f.tearDown()
    def flush(self):
        self.s.tick(now=time.monotonic()+40,wall=time.time()+40)
        for future in tuple(self.s.service.pending_db):future.result(3)
        self.s.service.finish_db()
    def task(self):
        p=self.s.preview
        return SendReply(p['reply_id'],p['reply'],p['source_event_id'],p['source_track_id'],p['source_binding_id'],
                         p['conversation_id'],p['model_request_id'],p['generated_at'],1)
    def test_group_plan_survives_restart_private_other_group_and_uncertain_person_do_not_recall(self):
        self.assertTrue(self.f.deliver(fixture.observation('@Ativia 我们下周一起去聚餐',1)))
        self.flush();memory=self.s.runtime.memory
        self.assertEqual(self.s.service.counts['memory_written'],1)
        private=replace(MemoryGate().judge('记住我喜欢聚餐'),content='PRIVATE_CANARY 聚餐')
        memory.change('save',private).result(3)
        seed=MemoryCandidateEvaluator().evaluate(fixture.observation('我们下周一起去聚餐',9).to_chat_event())
        memory.change('save',replace(seed,content='OTHER_GROUP_CANARY 聚餐',group_id='other-group')).result(3)
        memory.change('save',replace(seed,content='UNCERTAIN_PERSON_CANARY 聚餐',subject_id=fixture.observation().speaker_session_id)).result(3)
        original=memory.path;self.s.close()
        self.s=self.f.make_session(model_enabled=True,continuous_model=True);self.f.session=self.s
        self.s.bind('manual-test-binding','capture-after-restart',0)
        self.assertEqual(original,self.s.runtime.memory.path)
        self.assertTrue(self.f.deliver(fixture.observation('@Ativia 下周聚餐怎么安排？',2,capture='capture-after-restart')))
        context=self.f.contexts[-1]
        self.assertIn('下周一起去聚餐',context['memory'])
        for canary in ('PRIVATE_CANARY','OTHER_GROUP_CANARY','UNCERTAIN_PERSON_CANARY'):
            self.assertNotIn(canary,str(context))
        self.assertFalse(context['is_owner'])
        self.assertNotEqual(context['group_messages'][-1]['identity_quality'],'stable_id')
        self.assertEqual(self.s.snapshot()['memory'],'persistent_group')
    def test_uncertain_person_fact_is_not_persisted_or_promoted_to_owner(self):
        self.assertTrue(self.f.deliver(fixture.observation('@Ativia 我不吃香菜')))
        self.flush()
        self.assertEqual(self.s.service.counts['memory_written'],0)
        self.assertEqual(self.s.runtime.memory.snapshot,())
        self.assertEqual(self.s.service.people,{})
    def test_pause_drops_new_candidates_model_and_uncommitted_memory(self):
        self.assertTrue(self.s.receive(fixture.observation('我们下周一起去聚餐')))
        self.f.gate.transition('PAUSING');self.f.gate.transition('PAUSED')
        self.assertFalse(self.s.receive(fixture.observation('@Ativia 我们一起吃饭',2)))
        self.flush()
        self.assertEqual(self.f.model.call_count,0)
        self.assertEqual(self.s.runtime.memory.snapshot,())
    def test_only_confirmed_current_provenance_enters_sent_context_once(self):
        self.f.deliver();task=self.task();service=self.s.service
        self.assertFalse(any(e.sender_id=='ativia' for e in next(iter(service.groups.values())).buffer.rows))
        self.assertTrue(service.visual_delivery(task,'SEND_CONFIRMED','',time.time()))
        self.assertFalse(service.visual_delivery(task,'SEND_CONFIRMED','',time.time()))
        sent=[e for e in next(iter(service.groups.values())).buffer.rows if e.sender_id=='ativia']
        self.assertEqual(len(sent),1);self.assertEqual(sent[0].context_row()['delivery_status'],'confirmed')
        self.assertEqual(service.counts['sent'],1);self.assertEqual(service.pending_relationships,{})
        self.f.send.assert_not_called()
    def test_unknown_preview_never_becomes_something_already_said(self):
        self.f.deliver();task=self.task();service=self.s.service
        self.assertFalse(service.visual_delivery(task,'SEND_UNCERTAIN','own_bubble_not_confirmed',time.time()))
        self.assertFalse(service.visual_delivery(task,'SEND_CONFIRMED','late_duplicate',time.time()))
        self.assertEqual(service.counts['sent'],0)
        self.assertFalse(any(e.sender_id=='ativia' for e in next(iter(service.groups.values())).buffer.rows))
    def test_binding_change_discards_old_confirmed_result_from_current_context(self):
        self.f.deliver();task=self.task();self.s.bind('new-binding','new-capture',1)
        self.assertFalse(self.s.service.visual_delivery(task,'SEND_CONFIRMED','',time.time()))
        self.assertEqual(self.s.service.counts['sent'],0)
    def test_new_messages_do_not_replace_an_active_trigger_or_two_distinct_identical_questions(self):
        entered=threading.Event();release=threading.Event()
        def model(*args,**kwargs):
            entered.set();release.wait(3);return '模拟回复'
        self.f.model.side_effect=model
        now=time.monotonic();wall=time.time()
        first=fixture.observation('@Ativia 相同问题',1)
        self.s.receive(first,now=now,wall=wall);self.s.tick(now=now+2,wall=wall+2)
        self.assertTrue(entered.wait(2));job=self.s.service.job
        try:
            self.assertTrue(self.s.receive(fixture.observation('@Ativia 相同问题',2),now=now+2,wall=wall+2))
            self.assertTrue(self.s.receive(fixture.observation('普通消息',3),now=now+2,wall=wall+2))
            self.assertTrue(job.ticket.valid())
        finally:release.set()
        job.future.result(3);self.s.tick(now=now+4,wall=wall+4)
        first_reply=self.s.preview['source_event_id']
        next_job=self.s.service.job;self.assertIsNotNone(next_job)
        next_job.future.result(3);self.s.tick(now=now+5,wall=wall+5)
        self.assertNotEqual(first_reply,self.s.preview['source_event_id'])
        self.assertEqual(self.f.model.call_count,2);self.assertEqual(self.s.service.counts['ignored'],1)
        self.assertFalse(self.s.receive(first,now=now+5,wall=wall+5))
        self.s.tick(now=now+6,wall=wall+6);self.assertEqual(self.f.model.call_count,2)
    def test_expired_queued_trigger_never_starts_model_after_delay(self):
        now=time.monotonic();wall=time.time()
        self.s.receive(fixture.observation(),now=now,wall=wall)
        self.s.tick(now=now+31,wall=wall+31)
        self.assertEqual(self.f.model.call_count,0);self.assertIsNone(self.s.service.job)
    def test_rebind_invalidates_already_queued_async_memory_write(self):
        entered=threading.Event();release=threading.Event()
        def occupy():entered.set();release.wait(3)
        blocker=self.s.runtime.memory.submit(occupy)
        self.assertTrue(entered.wait(2))
        try:
            self.s.receive(fixture.observation('我们下周一起去聚餐'))
            self.s.tick(now=time.monotonic()+40,wall=time.time()+40)
            pending=tuple(self.s.service.pending_db);self.assertTrue(pending)
            self.s.bind('new-binding','new-capture',1)
        finally:release.set()
        blocker.result(3)
        for future in pending:future.result(3)
        self.assertEqual(self.s.runtime.memory.snapshot,())


if __name__=='__main__':unittest.main()
