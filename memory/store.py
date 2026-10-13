"""Transactional local memory, isolated from trace databases and API settings."""
from contextlib import closing
from dataclasses import asdict
import json,sqlite3,time
from pathlib import Path
from uuid import uuid4

SCHEMA=3
COLUMNS={
 'layer':"TEXT NOT NULL DEFAULT 'semantic'", 'classification':"TEXT NOT NULL DEFAULT 'LONG_TERM'",
 'type':"TEXT NOT NULL DEFAULT 'preference'",'confidence':"REAL NOT NULL DEFAULT 0.95",
 'importance':"REAL NOT NULL DEFAULT 0.7",'last_used':"REAL",'usage_count':"INTEGER NOT NULL DEFAULT 0",
 'source':"TEXT NOT NULL DEFAULT 'explicit_user'",'mutable':"INTEGER NOT NULL DEFAULT 1",
 'pinned':"INTEGER NOT NULL DEFAULT 0",'expires_at':"REAL",'mode':"TEXT NOT NULL DEFAULT 'ANY'",
 'application':"TEXT NOT NULL DEFAULT ''",'fact_key':"TEXT NOT NULL DEFAULT ''",
 'updated_at':"REAL NOT NULL DEFAULT 0",
 'subject_id':"TEXT NOT NULL DEFAULT 'owner:local'",'subject_name':"TEXT NOT NULL DEFAULT ''",
 'visibility':"TEXT NOT NULL DEFAULT 'private'",'group_id':"TEXT NOT NULL DEFAULT ''",
 'speaker_id':"TEXT NOT NULL DEFAULT 'owner:local'",'source_timestamp':"REAL",
 'conversation_type':"TEXT NOT NULL DEFAULT 'desktop'",'source_message_ids':"TEXT NOT NULL DEFAULT '[]'",
 'identity_quality':"TEXT NOT NULL DEFAULT 'local_owner'"
}

class MemoryStore:
    def __init__(self,path):
        self.path=Path(path);self.backup=None
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._migrate()

    def connection(self):
        db=sqlite3.connect(self.path,timeout=.2)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        return db

    def _verify(self,db):
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('memory_integrity_check_failed')
        if not set(COLUMNS).issubset({r[1] for r in db.execute('PRAGMA table_info(memories)')}):
            raise ValueError('memory_schema_incomplete')

    def _migrate(self):
        with closing(self.connection()) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version>SCHEMA:raise ValueError('memory_schema_newer_than_app')
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
            if exists and version<SCHEMA:
                self.backup=self.path.with_name(self.path.name+'.backup-'+uuid4().hex+'.sqlite3')
                with closing(sqlite3.connect(self.backup)) as backup:db.backup(backup)
            try:
                db.execute('BEGIN IMMEDIATE')
                if not exists:
                    db.execute('CREATE TABLE memories (id TEXT PRIMARY KEY,content TEXT NOT NULL,created_at REAL NOT NULL)')
                present={r[1] for r in db.execute('PRAGMA table_info(memories)')}
                if not {'id','content','created_at'}.issubset(present):raise ValueError('unsupported_legacy_memory_schema')
                for name,kind in COLUMNS.items():
                    if name not in present:db.execute('ALTER TABLE memories ADD COLUMN '+name+' '+kind)
                db.execute('CREATE INDEX IF NOT EXISTS memory_fact ON memories(fact_key)')
                db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS memory_scope ON memories(visibility,group_id,subject_id,fact_key)')
                db.execute('CREATE TABLE IF NOT EXISTS memory_revisions (id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL, revised_at REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS people (person_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,platform TEXT NOT NULL,quality TEXT NOT NULL,aliases TEXT NOT NULL,first_seen_at REAL NOT NULL,last_seen_at REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS relationships (person_id TEXT PRIMARY KEY,state TEXT NOT NULL,updated_at REAL NOT NULL)')
                self._verify(db)
                db.execute('PRAGMA user_version='+str(SCHEMA));db.commit()
            except BaseException:
                db.rollback()  # Original DB remains at old schema/data; backup kept.
                raise

    def rows(self,*,subject_id=None,group_id=None,visibility=None,scope=None,limit=1000):
        clauses=[];params=[]
        if scope is not None:
            subjects=tuple(dict.fromkeys(scope.subjects))
            if not subjects or (scope.channel=='group' and not scope.group_id):return []
            clauses.append('subject_id IN ('+','.join('?' for _ in subjects)+')');params.extend(subjects)
            if scope.channel=='group':
                clauses.append("(visibility='public' OR (visibility='group' AND group_id=?))");params.append(scope.group_id)
            else:
                clauses.append("(visibility='public' OR (visibility='private' AND group_id=''))")
        for key,value in (('subject_id',subject_id),('group_id',group_id),('visibility',visibility)):
            if value is not None:clauses.append(key+'=?');params.append(value)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with closing(self.connection()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM memories'+where+
                " ORDER BY CASE WHEN subject_id='owner:local' AND visibility='private' THEN 0 ELSE 1 END,updated_at DESC,created_at DESC LIMIT ?",params+[max(1,min(1000,int(limit)))])]

    def save(self,candidate,guard=None):
        now=time.time();row=asdict(candidate)
        if row['classification'] in ('IGNORE','SESSION') or (guard and not guard()):return None
        if row['visibility'] not in ('private','group','public') or not row['subject_id']:raise ValueError('invalid_memory_scope')
        if row['visibility']=='group' and not row['group_id']:raise ValueError('missing_memory_group')
        row.update(id=uuid4().hex,created_at=now,updated_at=now,last_used=None,usage_count=0,pinned=0)
        row['source_timestamp']=row['source_timestamp'] if row['source_timestamp'] is not None else now
        row['mutable']=int(row['mutable']);row['source_message_ids']=json.dumps(list(row['source_message_ids'])[-8:])
        with closing(self.connection()) as db,db:
            if guard and not guard():return None
            existing=db.execute('SELECT * FROM memories WHERE fact_key=? AND fact_key!=? AND subject_id=? AND group_id=? AND visibility=?',
                (row['fact_key'],'',row['subject_id'],row['group_id'],row['visibility'])).fetchone()
            if existing:
                if row['source_timestamp']<(existing['source_timestamp'] or existing['updated_at']):return existing['id']
                row.update(id=existing['id'],created_at=existing['created_at'],pinned=existing['pinned'],
                           last_used=existing['last_used'],usage_count=existing['usage_count'])
                if existing['content']!=row['content']:
                    db.execute('INSERT INTO memory_revisions(memory_id,content,source,revised_at) VALUES(?,?,?,?)',
                        (row['id'],existing['content'],existing['source'],now))
                    db.execute('DELETE FROM memory_revisions WHERE memory_id=? AND id NOT IN (SELECT id FROM memory_revisions WHERE memory_id=? ORDER BY id DESC LIMIT 5)',(row['id'],row['id']))
                db.execute('DELETE FROM memories WHERE id=?',(row['id'],))
            names=list(row)
            db.execute('INSERT INTO memories ('+','.join(names)+') VALUES ('+','.join('?' for _ in names)+')',[row[k] for k in names])
            db.execute("DELETE FROM memories WHERE pinned=0 AND layer='episodic' AND group_id=? AND id NOT IN "
                       "(SELECT id FROM memories WHERE layer='episodic' AND group_id=? ORDER BY created_at DESC LIMIT 200)",(row['group_id'],row['group_id']))
            db.execute('DELETE FROM memory_revisions WHERE memory_id NOT IN (SELECT id FROM memories)')
            if guard and not guard():db.rollback();return None
        return row['id']

    def revisions(self,identity):
        with closing(self.connection()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id DESC',(identity,))]

    def people(self):
        with closing(self.connection()) as db:return [dict(r) for r in db.execute('SELECT * FROM people ORDER BY last_seen_at DESC LIMIT 1000')]

    def remember_person(self,identity,now=None,guard=None):
        if guard and not guard():return
        now=time.time() if now is None else now
        with closing(self.connection()) as db,db:
            old=db.execute('SELECT aliases,display_name FROM people WHERE person_id=?',(identity.person_id,)).fetchone()
            aliases=set(identity.aliases)
            if old:aliases.update(json.loads(old['aliases']));aliases.add(old['display_name'])
            aliases.discard(identity.display_name)
            db.execute('INSERT INTO people VALUES(?,?,?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET display_name=excluded.display_name,quality=excluded.quality,aliases=excluded.aliases,last_seen_at=excluded.last_seen_at',
                (identity.person_id,identity.display_name,identity.platform,identity.quality,json.dumps(sorted(aliases)[-8:],ensure_ascii=False),now,now))
            if guard and not guard():db.rollback()

    def relationships(self):
        with closing(self.connection()) as db:
            return {r['person_id']:json.loads(r['state']) for r in db.execute('SELECT * FROM relationships')}

    def save_relationship(self,person_id,state,guard=None):
        if guard and not guard():return
        with closing(self.connection()) as db,db:
            db.execute('INSERT INTO relationships VALUES(?,?,?) ON CONFLICT(person_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at',
                (person_id,json.dumps(state),time.time()))
            if guard and not guard():db.rollback()

    def set_visibility(self,identity,visibility,group_id=''):
        if visibility not in ('private','public','group') or (visibility=='group' and not group_id):raise ValueError('invalid_visibility')
        with closing(self.connection()) as db,db:
            db.execute('UPDATE memories SET visibility=?,group_id=?,updated_at=? WHERE id=?',(visibility,group_id if visibility=='group' else '',time.time(),identity))

    def edit(self,identity,content):
        from .gate import sensitive,fact_key,domain
        text=content.strip()
        if not text or len(text)>400 or sensitive(text):raise ValueError('记忆内容为空、过长或含敏感凭据。')
        with closing(self.connection()) as db,db:
            old=db.execute('SELECT * FROM memories WHERE id=?',(identity,)).fetchone()
            if old is None:return
            key=fact_key(text)
            if old['conversation_type']=='group':
                from types import SimpleNamespace
                from .memory_candidate import MemoryCandidateEvaluator
                event=SimpleNamespace(text=text,timestamp=time.time(),sender_id=old['subject_id'],sender_name=old['subject_name'],
                    conversation_id=old['group_id'],message_id='local_edit',identity_quality=old['identity_quality'])
                candidate=MemoryCandidateEvaluator().evaluate(event)
                key=candidate.fact_key if candidate and candidate.subject_id==old['subject_id'] else ''
            db.execute('UPDATE memories SET content=?,fact_key=?,updated_at=?,source_timestamp=?,source=?,mode=?,application=? WHERE id=? AND mutable=1',
                (text,key,time.time(),time.time(),'explicit_user_edit',domain(text),'',identity))

    def delete(self,identity):
        with closing(self.connection()) as db,db:
            db.execute('DELETE FROM memories WHERE id=?',(identity,))
            db.execute('DELETE FROM memory_revisions WHERE memory_id=?',(identity,))

    def pin(self,identity,pinned):
        with closing(self.connection()) as db,db:db.execute('UPDATE memories SET pinned=?,updated_at=? WHERE id=?',(int(pinned),time.time(),identity))

    def used(self,identities):
        with closing(self.connection()) as db,db:
            db.executemany('UPDATE memories SET last_used=?,usage_count=usage_count+1 WHERE id=?',[(time.time(),i) for i in identities])
