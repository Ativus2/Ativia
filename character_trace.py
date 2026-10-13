"""Passive, opt-in local collection. Never supplies data to character decisions."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

SCHEMA_VERSION = 1
MAX_TRACES = 1000
MAX_RECORD_BYTES = 128 * 1024
REASONS = {
    "presence": "在场感", "personality": "人格表达", "continuity": "上下文连续性",
    "timing": "发言／沉默时机", "entertainment": "有趣程度", "understanding": "屏幕理解",
    "usefulness": "回答实用性", "system_error": "程序错误", "other": "其他",
}
_active = ContextVar("character_trace", default=None)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "Ativia" / "character-trace.sqlite3"


@contextmanager
def capture_model(trace):
    token = _active.set(trace)
    try:
        yield
    finally:
        _active.reset(token)


def model_request(model, system_prompt, user_prompt, max_tokens):
    """Allowlisted text only: no API credentials, headers, images or audio."""
    trace = _active.get()
    if trace is not None:
        try:
            trace["model"] = dict(requested=model, system_prompt_sha256=sha256(system_prompt.encode()).hexdigest(),
                                  user_prompt=user_prompt, max_completion_tokens=max_tokens, usage=None)
        except Exception:
            pass


def model_response(response, text):
    # Observability must not turn a valid response into a product error.
    trace = _active.get()
    if trace is None:
        return
    try:
        trace["raw_response"] = text
        usage = getattr(response, "usage", None)
        counts = {key: getattr(usage, key, None) for key in
                  ("prompt_tokens", "completion_tokens", "total_tokens")}
        trace.setdefault("model", {}).update(
            returned=getattr(response, "model", None) if isinstance(getattr(response, "model", None), str) else None,
            usage={key: value if type(value) is int else None for key, value in counts.items()})
    except Exception:
        pass


def _bounded(value, path="", clipped=None):
    """Bound textual payloads; mark truncation so exports never imply completeness."""
    if clipped is None:
        clipped = []
    if isinstance(value, str):
        if len(value) > 12000:
            clipped.append(path)
        return value[:12000]
    if isinstance(value, dict):
        return {key: _bounded(item, f"{path}.{key}", clipped) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, f"{path}[{i}]", clipped) for i, item in enumerate(value)]
    return value


class TraceStore:
    def __init__(self, path=None, max_traces=MAX_TRACES):
        self.path = Path(path) if path is not None else default_path()
        self.max_traces = max(1, int(max_traces))

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.1)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            with db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS traces (
                        ordinal INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                        created TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                        preview TEXT NOT NULL, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS feedback (
                        revision INTEGER PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
                        created TEXT NOT NULL, label TEXT NOT NULL CHECK(label IN ('success','failure','unrated')),
                        reason TEXT NOT NULL, comment TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS feedback_trace ON feedback(trace_id, revision);
                """)
                yield db

    def add(self, trace):
        clipped = []
        row = _bounded(trace, clipped=clipped)
        row["truncated_fields"] = clipped
        body = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("记录超过本地大小上限，未保存")
        with self.connection() as db:
            db.execute("INSERT INTO traces(id,created,source,status,preview,body) VALUES (?,?,?,?,?,?)",
                       (row["id"], row["created_utc"], row["source"], row["delivery"]["status"],
                        (row["delivery"].get("response") or row["delivery"].get("error") or "（没有展示发言）")[:160], body))
            db.execute("DELETE FROM traces WHERE ordinal NOT IN (SELECT ordinal FROM traces ORDER BY ordinal DESC LIMIT ?)",
                       (self.max_traces,))

    def recent(self):
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(zip(("id", "created", "source", "status", "preview", "label"), row)) for row in db.execute(
                "SELECT t.id,t.created,t.source,t.status,t.preview,"
                "COALESCE((SELECT label FROM feedback WHERE trace_id=t.id ORDER BY revision DESC LIMIT 1),'unrated') "
                "FROM traces t ORDER BY ordinal DESC LIMIT ?", (self.max_traces,))]

    def get(self, trace_id):
        with self.connection() as db:
            row = db.execute("SELECT body FROM traces WHERE id=?", (trace_id,)).fetchone()
            if row is None:
                raise KeyError("记录不存在或已被清理")
            feedback = [dict(zip(("revision", "created_utc", "label", "reason", "comment"), item)) for item in
                        db.execute("SELECT revision,created,label,reason,comment FROM feedback WHERE trace_id=? ORDER BY revision",
                                   (trace_id,))]
        return dict(trace=json.loads(row[0]), feedback_history=feedback, feedback=feedback[-1] if feedback else None)

    def label(self, trace_id, label, reason="other", comment=""):
        if label not in {"success", "failure", "unrated"} or reason not in REASONS:
            raise ValueError("无效反馈类别")
        if not isinstance(comment, str) or len(comment) > 1000:
            raise ValueError("反馈说明最多 1000 字")
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM traces WHERE id=?", (trace_id,)).fetchone():
                raise KeyError("记录不存在或已被清理")
            db.execute("INSERT INTO feedback(trace_id,created,label,reason,comment) VALUES (?,?,?,?,?)",
                       (trace_id, utc_now(), label, reason, comment.strip()))
            db.execute("DELETE FROM feedback WHERE trace_id=? AND revision NOT IN "
                       "(SELECT revision FROM feedback WHERE trace_id=? ORDER BY revision DESC LIMIT 20)", (trace_id, trace_id))

    def clear(self):
        if not self.path.exists():
            return
        with self.connection() as db:
            db.execute("DELETE FROM traces")
        with closing(sqlite3.connect(self.path, timeout=0.1)) as db:
            db.execute("VACUUM")

    def export(self, parent):
        """One immutable snapshot. A manifest marks completion; never overwrite a prior export."""
        destination = Path(parent) / ("character-dataset-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
        destination.mkdir(parents=True, exist_ok=False)
        counts = dict(traces=0, success=0, failure=0, unrated=0)
        # Read in one SQLite transaction so labels/context belong to one snapshot.
        with self.connection() as db, (destination / "traces.jsonl").open("w", encoding="utf-8") as all_rows, \
                (destination / "success.jsonl").open("w", encoding="utf-8") as successes, \
                (destination / "failure.jsonl").open("w", encoding="utf-8") as failures:
            db.execute("BEGIN")
            for trace_id, body in db.execute("SELECT id,body FROM traces ORDER BY ordinal"):
                history = [dict(zip(("revision", "created_utc", "label", "reason", "comment"), item)) for item in
                           db.execute("SELECT revision,created,label,reason,comment FROM feedback WHERE trace_id=? ORDER BY revision", (trace_id,))]
                feedback = history[-1] if history else None
                label = feedback["label"] if feedback else "unrated"
                row = dict(schema_version=SCHEMA_VERSION, provenance="local_user_feedback",
                           trace=json.loads(body), feedback=feedback, feedback_history=history)
                line = json.dumps(row, ensure_ascii=False) + "\n"
                all_rows.write(line)
                if label == "success":
                    successes.write(line)
                elif label == "failure":
                    failures.write(line)
                counts["traces"] += 1
                counts[label] += 1
        manifest = dict(schema_version=SCHEMA_VERSION, exported_utc=utc_now(), complete=True, counts=counts,
                        labels="Explicit latest user feedback only; silence/errors/absence of feedback are not labels.",
                        context="session_id + previous_trace_id; older linked rows may be pruned; state_before is the actual retained context.",
                        files={p.name: sha256(p.read_bytes()).hexdigest() for p in destination.glob("*.jsonl")})
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination


class TraceCollector:
    """UI-thread persistence; workers only accumulate a bounded-lifetime text draft."""
    def __init__(self, store=None):
        self.store = store if store is not None else TraceStore()
        self.enabled = False
        self.session_id = uuid4().hex
        self.previous_id = None
        self.last_error = ""
        root = Path(__file__).parent
        try:
            self.fingerprints = {p.name: sha256(p.read_bytes()).hexdigest() for p in
                                 (root / name for name in ("character.py", "persona.py", "vision.py", "overlay.py", "audio.py",
                                                          "voice_pipeline.py", "speech_activity.py", "runtime_settings.py",
                                                          "companion_policy.py"))}
        except OSError:
            self.fingerprints = {"unavailable": True}

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.session_id = uuid4().hex
        self.previous_id = None

    def begin(self, source, mode, context, state):
        if not self.enabled:
            return None
        return dict(schema_version=SCHEMA_VERSION, id=uuid4().hex, session_id=self.session_id,
                    previous_trace_id=self.previous_id, created_utc=utc_now(), source=source, mode=mode,
                    production_sha256=self.fingerprints, context=context, state_before=state,
                    candidate=None, decision=None, delivery=dict(status="pending", response=""))

    def finish(self, trace):
        if trace is None or not self.enabled or trace["session_id"] != self.session_id:
            return False
        try:
            self.store.add(trace)
            self.previous_id = trace["id"]
            self.last_error = ""
            return True
        except Exception:
            self.last_error = "本地记录未保存（文件不可用或记录过大）；桌宠功能继续运行。"
            return False
