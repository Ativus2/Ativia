import json,sqlite3,tempfile,time,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from memory.gate import MemoryGate,Candidate
from memory.store import MemoryStore,SCHEMA
from memory.service import MemoryService
from memory.retrieval import retrieve,token_upper_bound
from companion_runtime import CompanionRuntime

SAVE_CASES=[
 '以后我玩熟悉的游戏时，不要主动教我怎么玩。','我偏好简短的回答。','我不喜欢每句话都追问。',
 '以后课堂模式不要朗读。','请记住我喜欢桌宠形式的界面。','我叫小林。','我熟悉 Helldivers 2。',
 '我习惯使用笔记本内置麦克风。','我偏好较年轻的音色。','我喜欢用中文看翻译。',
 '记住我喜欢原文和译文一起显示。','以后不要在我专注时主动说话。','我不喜欢客服式的措辞。',
 '我偏好把聊天窗口收起来。','我擅长 Python。','我不擅长韩语听力。','记住今天我完成了字幕测试。',
 '最近在玩死亡搁浅。','这周我在测试 Ativia 字幕。','I prefer quiet company.'
]
IGNORE_CASES=[
 '我去倒杯水','谢谢','嗯','好的','今天的天气不错','帮我算 1+1','现在几点','哈哈哈','打开窗口',
 '这句话是什么意思？','我喜欢什么？','他说我喜欢教程','例如我喜欢游戏','如果我喜欢这个游戏呢',
 '这张图真好看','我喜欢这个笑话','帮我翻译：我喜欢游戏','不要记住这条消息',
 'API Key: sk-test-placeholder','密码是123456'
]
def flush(service):
    future=service.submit(lambda:None)
    if future:future.result(3)
def stop(service):
    service.close();service.close_future.result(3)

class MemoryGateTests(unittest.TestCase):
    def test_twenty_explicit_and_twenty_trivial_cases(self):
        gate=MemoryGate()
        for text in SAVE_CASES:
            with self.subTest(text=text):self.assertIn(gate.judge(text).classification,('TEMPORARY','LONG_TERM','PERMANENT'))
        for text in IGNORE_CASES:
            with self.subTest(text=text):self.assertEqual(gate.judge(text).classification,'IGNORE')
    def test_session_and_temporary_are_not_permanent(self):
        gate=MemoryGate()
        self.assertEqual(gate.judge('今天主要在测试字幕').classification,'SESSION')
        c=gate.judge('这几天我在玩死亡搁浅',now=100)
        self.assertEqual(c.expires_at,100+14*86400)
    def test_full_conversation_is_not_copied(self):
        c=MemoryGate().judge('我去喝水。以后玩游戏不要主动教我。今天的天气还可以。')
        self.assertNotIn('喝水',c.content);self.assertNotIn('天气',c.content)

class MemoryStoreTests(unittest.TestCase):
    def test_restart_edit_delete_pin_and_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'memory.sqlite3';s=MemoryStore(path);g=MemoryGate()
            identity=s.save(g.judge('以后玩游戏不要主动教我'))
            s.pin(identity,True);s.edit(identity,'以后我玩游戏时不要主动教学')
            row=MemoryStore(path).rows()[0];self.assertEqual(row['pinned'],1)
            self.assertEqual(row['source'],'explicit_user_edit')
            s.save(g.judge('以后玩游戏不要主动教我'))
            self.assertEqual(len(s.rows()),1)
            s.delete(identity);self.assertEqual(s.rows(),[])
    def test_migration_backs_up_and_retains_old_data(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'old.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,created_at REAL)')
                db.execute('INSERT INTO memories VALUES(?,?,?)',('old','我喜欢桌宠',100));db.execute('PRAGMA user_version=1')
            store=MemoryStore(path)
            self.assertTrue(store.backup.is_file());self.assertEqual(store.rows()[0]['content'],'我喜欢桌宠')
            with closing(sqlite3.connect(path)) as db, db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA)
    def test_failed_migration_rolls_back_without_deleting_db(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'old.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,created_at REAL)')
                db.execute("INSERT INTO memories VALUES('old','preference',100)");db.execute('PRAGMA user_version=1')
            with patch.object(MemoryStore,'_verify',side_effect=ValueError('injected')):
                with self.assertRaises(ValueError):MemoryStore(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT content FROM memories').fetchone()[0],'preference')
                self.assertEqual(len(db.execute('PRAGMA table_info(memories)').fetchall()),3)
            self.assertTrue(list(Path(d).glob('*.backup-*')))

class MemoryRetrievalTests(unittest.TestCase):
    def test_game_context_excludes_unrelated_class_and_python(self):
        with tempfile.TemporaryDirectory() as d:
            s=MemoryStore(Path(d)/'memory.sqlite3');g=MemoryGate()
            for text in ('我擅长 Python','我不擅长韩语听力','我偏好较年轻的音色','以后玩熟悉游戏不要主动教我'):
                s.save(g.judge(text))
            recall=retrieve(tuple(s.rows()),'这个任务怎么打？','GAME','helldivers2.exe')
            self.assertIn('游戏',recall.text)
            for unrelated in ('Python','韩语','音色'):self.assertNotIn(unrelated,recall.text)
    def test_expiry_decay_and_hard_context_bound(self):
        with tempfile.TemporaryDirectory() as d:
            s=MemoryStore(Path(d)/'memory.sqlite3');g=MemoryGate()
            s.save(g.judge('这周我在玩死亡搁浅',now=0))
            s.save(g.judge('以后玩游戏不要主动教我'))
            r=retrieve(tuple(s.rows()),'游戏','GAME',budget=400,now=time.time())
            self.assertNotIn('死亡搁浅',r.text);self.assertIn('不要主动教我',r.text)
            self.assertLessEqual(token_upper_bound(r.text),400)
    def test_persistence_reaches_actual_reply_context_after_restart(self):
        from vision import build_text_request
        from runtime_settings import RuntimeSettings
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'companion.json';a=CompanionRuntime(path)
            a.memory.ready.wait(2);a.remember_input('以后玩熟悉游戏不要主动教我');flush(a.memory);a.set_mode('GAME');a.close();a.memory.close_future.result(3)
            b=CompanionRuntime(path);b.memory.ready.wait(2)
            try:
                c=b.context('这个游戏任务怎么打')
                params=build_text_request('这个游戏任务怎么打','','[]',RuntimeSettings(),companion_context=c)
                self.assertIn('不要主动教我',params['messages'][1]['content'])
                self.assertTrue(c['decision']['allow_teaching'])  # A current explicit question still wins.
            finally:b.close();b.memory.close_future.result(3)
    def test_background_db_cannot_block_recall(self):
        with tempfile.TemporaryDirectory() as d:
            service=MemoryService(Path(d)/'memory.sqlite3');service.ready.wait(2)
            release=threading.Event();started=threading.Event()
            future=service.submit(lambda:(started.set(),release.wait(2)))
            started.wait(1)
            began=time.perf_counter();service.recall('游戏','GAME')
            self.assertLess(time.perf_counter()-began,.05)
            release.set();future.result(3);stop(service)
    def test_corrupt_memory_degrades_and_does_not_crash_reply_builder(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'memory.sqlite3';path.write_bytes(b'not a sqlite database')
            service=MemoryService(path);service.ready.wait(2)
            self.assertIsNotNone(service.error);self.assertEqual(service.recall('你好','NORMAL').text,'')
            stop(service)
    def test_close_flushes_accepted_preferences(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'memory.sqlite3';service=MemoryService(path)
            service.consider('记住我喜欢短回复');stop(service)
            self.assertEqual(len(MemoryStore(path).rows()),1)

if __name__=='__main__':unittest.main()

