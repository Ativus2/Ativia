import json,sqlite3,tempfile,time,unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from memory.store import MemoryStore,SCHEMA
from memory.gate import MemoryGate,Candidate
from memory.scope import RecallScope
from memory.retrieval import retrieve
from memory.memory_candidate import MemoryCandidateEvaluator,CandidateQueue
from memory.service import MemoryService
from tests_support_wechat import make_event
from chat_events import OWNER_ID

class WechatMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.path=Path(self.directory.name)/'memory.sqlite3';self.store=MemoryStore(self.path);self.evaluator=MemoryCandidateEvaluator()
    def tearDown(self):self.directory.cleanup()
    def save(self,text,person='A',group='g',timestamp=None):
        event=make_event(text,person,group,timestamp=timestamp);candidate=self.evaluator.evaluate(event)
        self.assertIsNotNone(candidate);return self.store.save(candidate)
    def recall(self,text,person='A',group='g'):
        return retrieve(tuple(self.store.rows()),text,budget=4000,scope=RecallScope.group(group,(person,)))
    def test_subjects_are_never_owner_and_conflicts_are_scoped(self):
        a=self.save('我不吃香菜','A');b=self.save('我喜欢香菜','B')
        rows=self.store.rows();self.assertEqual({r['subject_id'] for r in rows},{'A','B'})
        self.assertEqual(len(rows),2);self.assertNotIn(OWNER_ID,{r['subject_id'] for r in rows})
        result=self.recall('你觉得我点香菜牛肉怎么样','A')
        self.assertIn('我不吃香菜',result.text);self.assertNotIn('我喜欢香菜',result.text);self.assertIn('"subject_id":"A"',result.text)
        self.assertEqual(retrieve(tuple(rows),'香菜').text,'')
    def test_private_and_other_groups_excluded_before_prompt_ranking(self):
        secret=replace(MemoryGate().judge('记住我喜欢秘密旅行'),content='PRIVATE_CANARY 旅行秘密')
        self.store.save(secret);self.save('周六一起去弘大吃烤肉','A','g');self.save('周六一起去看电影','A','other')
        scope=RecallScope.group('g',('A',OWNER_ID))
        result=retrieve(tuple(self.store.rows()),'秘密 旅行 烤肉 电影 周六',budget=4000,scope=scope)
        self.assertNotIn('PRIVATE_CANARY',result.text);self.assertNotIn('电影',result.text);self.assertIn('烤肉',result.text)
    def test_public_preferences_are_available_without_exposing_private(self):
        c=replace(MemoryGate().judge('以后回复简短一点'),visibility='public');self.store.save(c)
        self.assertIn('简短',self.recall('你好').text)
    def test_conflicting_fact_updates_and_keeps_bounded_history(self):
        first=self.save('我不喜欢恐怖游戏',timestamp=100)
        second=self.save('我最近突然很喜欢恐怖游戏',timestamp=200)
        self.assertEqual(first,second);self.assertEqual(len(self.store.rows()),1)
        self.assertIn('很喜欢',self.store.rows()[0]['content']);self.assertIn('不喜欢',self.store.revisions(first)[0]['content'])
        self.save('我不喜欢恐怖游戏',timestamp=50)
        self.assertIn('很喜欢',self.store.rows()[0]['content'])
        self.store.delete(first);self.assertEqual(self.store.revisions(first),[])
    def test_group_plan_survives_restart_and_has_provenance(self):
        identity=self.save('周六一起去弘大吃烤肉','A','g')
        row=MemoryStore(self.path).rows(group_id='g')[0]
        for key in ('speaker_id','subject_id','group_id','source_timestamp','source_message_ids','visibility'):self.assertTrue(row[key])
        self.assertEqual(row['subject_id'],'g');self.assertIn('烤肉',self.recall('周六干嘛').text)
        self.assertEqual(self.store.rows(subject_id='A'),[])
    def test_candidates_ignore_trivial_ambiguous_sensitive_and_quoted(self):
        for text in ('哈哈哈哈','早','草','下课了','我想死','小明说我喜欢 Apex','听说我最近失恋了','我的密码是123456','我不吃香菜吗？'):
            with self.subTest(text=text):self.assertIsNone(self.evaluator.evaluate(make_event(text)))
    def test_fragment_consolidation_never_uses_another_speaker(self):
        earlier=make_event('我下个月去东京交换了','A',timestamp=100)
        correct=make_event('一年','A',timestamp=110)
        c=self.evaluator.evaluate(correct,(earlier,));self.assertIn('东京',c.content);self.assertIn('一年',c.content)
        self.assertIsNone(self.evaluator.evaluate(make_event('一年','B',timestamp=110),(earlier,)))
    def test_queue_batches_and_discards_previous_pause_epoch(self):
        queue=CandidateQueue(20);one=self.evaluator.evaluate(make_event('我不吃香菜'))
        queue.add(one,1,0);queue.add(replace(one,content='我喜欢香菜'),2,0)
        self.assertEqual(queue.take_due(10,0),());self.assertEqual(len(queue.take_due(22,0)),1)
        queue.add(one,30,0);self.assertEqual(queue.take_due(100,1),());self.assertFalse(queue.pending)
    def test_schema2_migrates_as_private_and_rolls_back_on_failure(self):
        path=Path(self.directory.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT NOT NULL,created_at REAL NOT NULL)')
            db.execute("INSERT INTO memories VALUES('old','PRIVATE_CANARY',100)");db.execute('PRAGMA user_version=2')
        with patch.object(MemoryStore,'_verify',side_effect=ValueError('injected')):
            with self.assertRaises(ValueError):MemoryStore(path)
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
        migrated=MemoryStore(path);row=migrated.rows()[0]
        self.assertTrue(migrated.backup.exists());self.assertEqual(row['visibility'],'private');self.assertEqual(row['subject_id'],OWNER_ID)
        self.assertEqual(retrieve(tuple(migrated.rows()),'PRIVATE_CANARY',scope=RecallScope.group('g',(OWNER_ID,))).text,'')
    def test_disabled_or_paused_consolidation_cannot_write(self):
        service=MemoryService(self.path);service.ready.wait(3)
        try:
            c=self.evaluator.evaluate(make_event('我不吃香菜'))
            service.consolidate((c,),guard=lambda:False).result(3);self.assertEqual(service.snapshot,())
            service.paused=True;service.consolidate((c,)).result(3);self.assertEqual(service.snapshot,())
            service.paused=False;service.enabled=False;service.consolidate((c,)).result(3);self.assertEqual(service.snapshot,())
        finally:service.close();service.close_future.result(3)
