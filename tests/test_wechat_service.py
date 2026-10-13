import tempfile,time,threading,unittest,json
from pathlib import Path
from dataclasses import replace,asdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from companion_runtime import CompanionRuntime
from assistant_pause import PauseGate
from performance.scheduler import TaskScheduler
from integrations.wechat.config import WechatConfig
from integrations.wechat.service import WechatService
from integrations.wechat.identity import group_id
from tests_support_wechat import make_event
from runtime_settings import RuntimeSettings
from memory.gate import MemoryGate
from memory.memory_candidate import MemoryCandidateEvaluator

class FakeLog:
    def __init__(self):self.rows=[]
    def emit(self,stage,**fields):self.rows.append((stage,fields))
    def text(self,*args):pass
    def close(self):pass
class FakeAdapter:
    def __init__(self,*args):self.active=True;self.sends=[];self.packets=[];self.last_seen=time.monotonic()
    def alive(self):return self.active
    def start(self):self.active=True
    def stop(self):self.active=False
    def close(self):self.stop()
    def receive_event(self):rows=self.packets[:];self.packets.clear();return rows
    def send_message(self,*args):self.sends.append(args)
class FakeCore:
    def __init__(self):self.calls=[];self.block=None;self.entered=threading.Event()
    def handle(self,event,events,**kw):
        self.calls.append((event,events,kw));self.entered.set()
        if self.block:self.block.wait(3)
        return '收到啦'

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.runtime=CompanionRuntime(Path(self.temp.name)/'companion.json');self.runtime.memory.ready.wait(3)
        self.gate=PauseGate();self.scheduler=TaskScheduler();self.core=FakeCore();self.adapter=FakeAdapter();self.log=FakeLog()
        # Existing mocked acknowledgement tests exercise explicitly enabled sending.
        cfg=WechatConfig(enabled=True,dry_run=False,allowed_groups=('g',),owner_names=('owner',),memory_enabled=True,candidate_delay_seconds=5)
        self.service=WechatService(self.runtime,self.gate,self.scheduler,RuntimeSettings(),cfg,log=self.log,core=self.core)
        self.service.executor=ThreadPoolExecutor(max_workers=1);self.service.adapter=self.adapter;self.service.connected=True
        self.now=time.monotonic();self.wall=time.time();self.seq=0
    def tearDown(self):
        if self.core.block:self.core.block.set()
        self.service.close();self.service.executor.shutdown(wait=True,cancel_futures=True)
        self.runtime.close();self.runtime.memory.close_future.result(4);self.temp.cleanup()
    def receive(self,text,person='A',offset=0,group='g',**kw):
        self.seq+=1
        raw=dict(attr='friend',type='text',sender=person,content=text,id=str(self.seq),sequence=self.seq);raw.update(kw)
        self.service.receive(group,raw,self.now+offset,self.wall+offset)
    def start(self,offset=3):self.service.start_reply(self.now+offset,self.wall+offset)
    def finish(self):
        if self.service.job:self.service.job.future.result(4)
        self.service.finish_reply(self.now+4,self.wall+4)
    def ack(self,status='sent'):
        request=self.adapter.sends[-1][2];self.service.sent_result(dict(request_id=request,status=status),self.wall+4)
    def flush(self):self.runtime.memory.submit(lambda:None).result(4);self.service.finish_db()
    def test_twenty_messages_debounce_one_call_and_exact_latest_sequence(self):
        for i in range(20):self.receive('@Ativia 你觉得呢 '+str(i),offset=i*.02)
        self.start(.4);self.assertEqual(self.service.counts['model_calls'],0)
        self.start(2);self.finish();self.assertEqual(len(self.core.calls),1);self.assertEqual(len(self.core.calls[0][1]),20)
        self.assertEqual(len(self.adapter.sends),1);self.assertEqual(self.adapter.sends[0][3],20)
        self.ack();self.assertEqual(self.service.counts['sent'],1)
    def test_stale_reply_discarded_then_new_batch_preserves_original_speaker(self):
        self.core.block=threading.Event();self.receive('@Ativia 你觉得呢','A');self.start();self.assertTrue(self.core.entered.wait(2))
        self.receive('我补充一下','B',offset=3.2);self.core.block.set();self.finish();self.assertFalse(self.adapter.sends)
        self.start(5);self.finish();self.assertEqual(len(self.adapter.sends),1)
        self.assertEqual(self.core.calls[-1][0].sender_name,'A');self.assertEqual(len(self.core.calls[-1][1]),2)
    def test_pause_blocks_model_send_and_new_candidates(self):
        self.receive('@Ativia 我不吃香菜');self.gate.transition('PAUSING');self.start();self.receive('@Ativia 怎么样')
        self.assertFalse(self.adapter.alive());self.assertFalse(self.core.calls);self.assertFalse(self.adapter.sends)
        self.service.tick(self.now+5,self.wall+5);self.assertFalse(self.service.candidates.pending)
        self.assertTrue(self.service.quiescent());self.assertFalse(self.runtime.memory.snapshot)
    def test_pause_discards_already_running_model(self):
        self.core.block=threading.Event();self.receive('@Ativia 你好');self.start();self.assertTrue(self.core.entered.wait(2))
        self.gate.transition('PAUSING');self.core.block.set();self.finish();self.assertFalse(self.adapter.sends)
    def test_queued_consolidation_cannot_write_after_pause(self):
        unblock=threading.Event();entered=threading.Event()
        def block():entered.set();unblock.wait(3)
        self.runtime.memory.submit(block);self.assertTrue(entered.wait(2))
        try:
            self.receive('我不吃香菜');self.service.consolidate(self.now+6)
            self.gate.transition('PAUSING');self.assertFalse(self.service.quiescent())
        finally:unblock.set()
        self.flush();self.assertFalse(self.runtime.memory.snapshot);self.assertTrue(self.service.quiescent())
    def test_private_canary_and_other_group_never_reach_model(self):
        from conversation_core import AtiviaConversation
        from vision import build_text_request
        memory=self.runtime.memory
        memory.change('save',replace(MemoryGate().judge('记住我喜欢秘密'),content='PRIVATE_CANARY 香菜旅行')).result(3)
        memory.change('save',MemoryCandidateEvaluator().evaluate(make_event('我喜欢 CROSS_GROUP_CANARY 香菜旅行','owner:local','other'))).result(3)
        self.service.core=AtiviaConversation(self.runtime,RuntimeSettings());calls=[]
        def respond(text,recent,screen,settings,*args,companion_context,**kw):
            calls.append(build_text_request(text,recent,screen,settings,companion_context=companion_context));return 'hello'
        with patch('vision.respond_to_voice',side_effect=respond):
            self.receive('@Ativia 私人旅行秘密是什么，我吃香菜吗','owner');self.start();self.finish()
        # Canary words exist only in the two disallowed database rows, with matching retrieval terms.
        system=json.dumps(calls[0]['messages'],ensure_ascii=False)
        self.assertNotIn('PRIVATE_CANARY',system);self.assertNotIn('CROSS_GROUP_CANARY',system)
    def test_memory_delete_invalidates_inflight_reply(self):
        self.core.block=threading.Event();self.receive('@Ativia hello');self.start();self.assertTrue(self.core.entered.wait(2))
        self.runtime.memory.change('delete','nonexistent').result(3);self.core.block.set();self.finish();self.assertFalse(self.adapter.sends)
    def test_unknown_send_is_not_retried_or_counted_successful(self):
        self.receive('@Ativia hello');self.start();self.finish();self.ack('unknown');self.start(8);self.finish()
        self.assertEqual(len(self.adapter.sends),1);self.assertEqual(self.service.counts['sent'],0)
    def test_members_have_independent_relationships_and_owner_uses_existing(self):
        baseline=self.runtime.relationship.conversation_frequency
        for person in ('A','B','owner'):
            self.receive('@Ativia hello',person,offset=10*self.seq);self.start(10*self.seq+2);self.finish();self.ack()
            self.service.consolidate(self.now+40);self.flush()
        self.assertEqual(self.runtime.relationship.conversation_frequency,baseline+1)
        relations=self.runtime.memory.store.relationships();self.assertEqual(len(relations),3)
        for person in self.runtime.memory.people_snapshot:
            if person['person_id']!='owner:local':self.assertEqual(relations[person['person_id']]['conversation_frequency'],1)
    def test_no_group_relationship_persistence_when_memory_disabled(self):
        self.service.config=replace(self.service.config,memory_enabled=False)
        old=asdict(self.runtime.relationship);self.receive('@Ativia hello','owner');self.start();self.finish();self.ack();self.flush()
        self.assertEqual(old,asdict(self.runtime.relationship));self.assertFalse(self.runtime.memory.store.relationships())
    def test_alias_direct_trigger_survives_debounce_without_second_random_draw(self):
        event=make_event('以后叫你小A','A',group_id('g'));candidate=MemoryCandidateEvaluator().evaluate(event)
        self.runtime.memory.change('save',candidate).result(3)
        self.receive('小A 你人呢');self.start();self.finish();self.assertEqual(len(self.adapter.sends),1)
    def test_disabled_does_not_start_adapter_or_worker(self):
        service=WechatService(self.runtime,self.gate,self.scheduler,RuntimeSettings(),replace(self.service.config,enabled=False),adapter_factory=lambda c:self.fail('must not connect'),log=FakeLog())
        service.start();self.assertIsNone(service.thread);self.assertIsNone(service.executor);service.close();self.assertTrue(service.closed_finished())
    def test_dead_listener_keeps_actionable_error_and_does_not_send(self):
        self.adapter.active=False;self.adapter.packets=[dict(stage='error',status='logged_in_window_missing'),dict(stage='disconnected')]
        self.service.tick(self.now,self.wall);self.assertEqual(self.service.status,'logged_in_window_missing');self.assertFalse(self.service.connected)
    def test_malformed_relationship_values_cannot_break_current_member(self):
        self.runtime.memory.relationship_snapshot={'A':dict(familiarity='bad',conversation_frequency=-5,playfulness_tolerance=float('nan'))}
        state=self.service.relationship('A');self.assertEqual(state.conversation_frequency,0);self.assertIsInstance(state.familiarity,float)
    def test_whitelist_and_self_ignore_do_not_call_model_or_memory(self):
        self.receive('@Ativia 我喜欢香菜',group='other');self.receive('@Ativia 我喜欢香菜',attr='self')
        self.start();self.assertFalse(self.core.calls);self.assertFalse(self.service.candidates.pending)
    def test_main_user_input_cancels_background_wechat_ticket(self):
        self.core.block=threading.Event();self.receive('@Ativia hello');self.start();self.assertTrue(self.core.entered.wait(2))
        self.scheduler.admit('text',replace=True);self.core.block.set();self.finish();self.assertFalse(self.adapter.sends)

if __name__=='__main__':unittest.main()
