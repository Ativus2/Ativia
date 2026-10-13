"""Bounded local target-group evidence. Never used as model history or send queue.

The taskbook explicitly requires source text for acceptance. General status/logs
stay metadata-only; this SQLite file remains private runtime data on this PC.
"""
import json
from pathlib import Path
import sqlite3
import time
from ..identity import group_id
from .types import TARGET_GROUP


class VisualJournal:
    def __init__(self,path,*,limit=300,wall=time.time):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.limit=limit;self.wall=wall
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,observed REAL NOT NULL,payload TEXT NOT NULL,decision TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS replies(reply_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,updated REAL NOT NULL);')
        self.prune()
    def connect(self):
        # Context manager closes as well as commits; no open GUI-thread handles.
        from contextlib import contextmanager
        @contextmanager
        def opened():
            db=sqlite3.connect(self.path,timeout=2)
            try:
                with db:yield db
            finally:db.close()
        return opened()
    def event(self,event):
        if event.conversation_id!=group_id(TARGET_GROUP):raise ValueError('journal_wrong_group')
        raw=event.raw_message
        payload=dict(channel='wechat_visual',group=TARGET_GROUP,conversation_id=event.conversation_id,
            sender_id=event.sender_id,sender_name=event.sender_name,identity_quality=event.identity_quality,
            text=event.text,observed_at=event.timestamp,message_id=event.message_id,new_message=True,
            binding_id=raw.get('binding_id'),capture_session_id=raw.get('capture_session_id'),capture_epoch=raw.get('capture_epoch'))
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(event.message_id,event.timestamp,json.dumps(payload,ensure_ascii=False),
                'visual_explicit_at' if event.is_at_me else 'ignored_no_explicit_at'))
        self.prune()
    def preview(self,preview):
        with self.connect() as db:
            # Do not let repeated UI polling overwrite a terminal result.
            db.execute('INSERT OR IGNORE INTO replies SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM events WHERE event_id=?)',
                (preview['reply_id'],preview['source_event_id'],preview['reply'],'GENERATED_NOT_SENT','',self.wall(),preview['source_event_id']))
    def result(self,task,status,reason):
        with self.connect() as db:
            db.execute('UPDATE replies SET status=?,reason=?,updated=? WHERE reply_id=? AND event_id=?',
                (status,reason,self.wall(),task.reply_id,task.source_event_id))
    def reconcile(self,ledger):
        ledger=Path(ledger)
        if not ledger.is_file():return
        source=sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            with self.connect() as db:
                for reply_id, in db.execute('SELECT reply_id FROM replies').fetchall():
                    row=source.execute('SELECT status FROM replies WHERE reply_id=?',(reply_id,)).fetchone()
                    if row:db.execute('UPDATE replies SET status=?,reason=?,updated=? WHERE reply_id=?',
                        ('SEND_UNCERTAIN' if row[0]=='SUBMIT_INTENT' else row[0],'restored_from_send_ledger',self.wall(),reply_id))
        finally:source.close()
    def prune(self):
        with self.connect() as db:
            db.execute('DELETE FROM events WHERE observed<?',(self.wall()-7*86400,))
            db.execute('DELETE FROM events WHERE event_id NOT IN(SELECT event_id FROM events ORDER BY observed DESC,rowid DESC LIMIT ?)',(self.limit,))
            db.execute('DELETE FROM replies WHERE event_id NOT IN(SELECT event_id FROM events)')
