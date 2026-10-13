from dataclasses import replace
from pathlib import Path
import json
import tempfile
import time
import unittest

from integrations.wechat.visual.journal import VisualJournal
from integrations.wechat.visual.send import SendLedger,SendReply
from test_wechat_visual_service import observation


class JournalTests(unittest.TestCase):
    def test_bounded_target_only_local_evidence_and_unknown_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);journal=VisualJournal(root/'events.sqlite3',limit=2)
            events=[observation('@Ativia 本地测试原文',i+1).to_chat_event() for i in range(3)]
            for event in events:journal.event(event)
            with journal.connect() as db:
                rows=db.execute('SELECT payload FROM events ORDER BY observed').fetchall()
            self.assertEqual(len(rows),2);self.assertIn('本地测试原文',rows[-1][0])
            with self.assertRaisesRegex(ValueError,'wrong_group'):journal.event(replace(events[-1],conversation_id='other-group'))
            event=events[-1]
            task=SendReply('reply','模拟回复',event.message_id,event.message_id,'manual-test-binding',event.conversation_id,'model',time.time(),1)
            preview=dict(reply_id=task.reply_id,source_event_id=event.message_id,reply=task.text)
            journal.preview(preview);journal.result(task,'SEND_UNCERTAIN','unconfirmed')
            journal.preview(preview)
            with journal.connect() as db:self.assertEqual(db.execute('SELECT status FROM replies').fetchone()[0],'SEND_UNCERTAIN')
            ledger=SendLedger(root/'send.sqlite3');ledger.reserve(task);ledger.submit(task.reply_id)
            restarted=VisualJournal(journal.path,limit=2);restarted.reconcile(ledger.path)
            with restarted.connect() as db:self.assertEqual(db.execute('SELECT status FROM replies').fetchone()[0],'SEND_UNCERTAIN')
            restarted.wall=lambda:time.time()+8*86400;restarted.prune()
            with restarted.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM replies').fetchone()[0],0)


if __name__=='__main__':unittest.main()
