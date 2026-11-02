"""Session notes: short summaries kept apart from raw transcripts.

A note says what a stretch of conversation was about and points back to its
exact source, so an agent can find the right conversation and then read the
transcript with ``history_show``. A summary is not a transcription, so notes
live in their own store (``notes.db``), never in the transcript index.

- **Append-only.** There is no update or delete path, the table refuses
  ``UPDATE``/``DELETE`` (triggers), files are created ``O_EXCL``, and a
  correction is a new note with ``corrects`` set.
- **Redacted** with ``redact.redact`` before a note is written anywhere, and
  again when the hub indexes it.
- **Sourced.** Exactly one of: a claude.ai conversation URL; a Duet channel
  id plus the message id that starts the span; a Claude Code session id plus
  machine (plus the job id for ``claude -p``). Anything else is refused.

Notes reach a hub as ``inbox/<tenant>/<machine>/notes/*.jsonl``: below the
directory the transcript reader looks at, and without the ``text`` field it
requires, so an older hub never mistakes one for a transcript.

See docs/design/session-notes.md.
"""
from __future__ import annotations

import datetime
import getpass
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.parse
import uuid
from collections import Counter
from pathlib import Path
from typing import Iterable

from . import config, embed, redact

FORMAT = 1
KINDS = ("claude-ai", "duet", "claude-code", "record")
RECORD_ORIGIN = "history_record"
RECORD_SURFACE = "claude-ai-chat"
_RECORD_SESSION = re.compile(r"^claude-ai-chat-[A-Za-z0-9-]{1,80}$")
_RECORD_ID = re.compile(r"^rec-[0-9a-f]{6,40}$")
NOTES_DIR = "notes"
PROCESSED = ".processed"
MODES = ("hybrid", "text", "vector")

MAX_TITLE = 200
MAX_SUMMARY = 8000
MAX_ITEMS = 30
MAX_ITEM_CHARS = 1000
MAX_FIELD = 200
EXCERPT_CHARS = 700
DEFAULT_IDLE_MINUTES = 5
SUMMARIZER_TIMEOUT = 300
MAX_SPAN_MESSAGES = 200
MAX_MESSAGE_CHARS = 2000
SCRUB_MARKER = "[REDACTED:scrubbed]"
DEFAULT_SUMMARIZER = "claude"
DEFAULT_MODEL = "haiku"
# The summarizer's own prompt starts with this, and the sweep skips any
# session that does, so a summary is never itself summarized.
SUMMARIZER_MARK = "[agent-history notes summarizer]"
SUMMARIZER_PROMPT = (
    SUMMARIZER_MARK + " Summarize the conversation span given as JSON on stdin "
    "(messages oldest first; secrets are already redacted). Cover what was "
    "actually discussed and done across the whole span, not just its first or "
    "last message. Reply with ONLY a JSON object, no prose and no code fence: "
    '{"title": "<one line, at most 120 characters>", "summary": "<3 to 6 '
    'sentences>", "decisions": ["<short>"], "findings": ["<short>"], '
    '"links": ["<URLs, PRs, issues or files mentioned>"]}. Use empty lists '
    "when there are none. Never include passwords, keys or tokens.")

NOTE_ID = re.compile(r"^note-[0-9a-f]{20}$")
JOB_ID = re.compile(r"^cj_\d{8}T\d{6}Z_[0-9a-f]{6}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CHAT_URL = re.compile(
    r"^https://claude\.ai/(?:chat|project/[0-9a-f-]+/chat)/([0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#].*)?$")
_TOKEN = re.compile(r"[^\W_]+")
_STOP = frozenset("a an and are as at be but by for from has have how i if in "
                  "is it of on or so that the this to was we what when where "
                  "which who why with you".split())


class NoteError(ValueError):
    """A note that cannot be accepted as written."""


# ------------------------------------------------------------------ building

def _clean(value: object, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise NoteError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise NoteError(f"{name} is required")
    if len(value) > limit:
        raise NoteError(f"{name}: at most {limit} characters")
    return value


def _items(name: str, values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise NoteError(f"{name} must be a list of strings")
    if len(values) > MAX_ITEMS:
        raise NoteError(f"{name}: at most {MAX_ITEMS} items")
    return [v for v in (_clean(v, name, MAX_ITEM_CHARS) for v in values) if v]


def make_source(*, chat_url: str | None = None, duet_channel_id: str | None = None,
                duet_message_id: str | None = None, session_id: str | None = None,
                machine: str | None = None, job_id: str | None = None) -> dict:
    """The one source a note points back to. Refuses none, several, or a
    malformed one: a note without a resolvable source is not accepted."""
    chat_url = (chat_url or "").strip()
    channel = (duet_channel_id or "").strip()
    message = (duet_message_id or "").strip()
    session = (session_id or "").strip()
    machine = (machine or "").strip()
    job = (job_id or "").strip()
    families = [bool(chat_url), bool(channel or message), bool(session or job)]
    if sum(families) != 1:
        raise NoteError("give exactly one source: a claude.ai chat_url, a Duet "
                        "channel id + message id, or a Claude Code session id")
    if chat_url:
        if not _CHAT_URL.match(chat_url) or len(chat_url) > 500:
            raise NoteError("chat_url must be a https://claude.ai/chat/<uuid> URL")
        return {"kind": "claude-ai", "url": chat_url}
    if channel or message:
        if not (channel and message):
            raise NoteError("a Duet source needs both the channel id and the "
                            "message id that starts the span")
        if not (_ID.match(channel) and _ID.match(message)):
            raise NoteError("Duet channel and message ids must be plain ids")
        return {"kind": "duet", "channel_id": channel, "message_id": message}
    if not session:
        raise NoteError("a Claude Code source needs its session id (and the job id "
                        "for a claude -p job)")
    if not _ID.match(session):
        raise NoteError("session_id must be a plain id")
    if not machine or not _MACHINE.match(machine) or len(machine) > MAX_FIELD:
        raise NoteError("a Claude Code source needs the machine it ran on")
    if job and not JOB_ID.match(job):
        raise NoteError("job_id must look like cj_<yyyymmddThhmmssZ>_<6 hex>")
    source = {"kind": "claude-code", "machine": machine, "session_id": session}
    if job:
        source["job_id"] = job
    return source


def record_source(client: str, session: str) -> dict:
    """The source of a history_record call that gave no link: the record's
    own chat session, which history_show resolves from the notes store.
    Only history_record and the migration build one."""
    if not (client and _MACHINE.match(client) and len(client) <= MAX_FIELD):
        raise NoteError("a record source needs the client machine")
    if not _RECORD_SESSION.match(session or ""):
        raise NoteError("a record source needs its claude-ai-chat-… session")
    return {"kind": "record", "client": client, "session": session}


def validate_source(source: object) -> dict:
    """Re-check a source read from the wire."""
    if not isinstance(source, dict) or source.get("kind") not in KINDS:
        raise NoteError("unknown source kind")
    kind = source["kind"]
    if kind == "record":
        return record_source(source.get("client"), source.get("session"))
    if kind == "claude-ai":
        return make_source(chat_url=source.get("url"))
    if kind == "duet":
        return make_source(duet_channel_id=source.get("channel_id"),
                           duet_message_id=source.get("message_id"))
    return make_source(session_id=source.get("session_id"),
                       machine=source.get("machine"), job_id=source.get("job_id"))


def show_target(source: dict, span: dict | None = None) -> dict | None:
    """What to pass to ``history_show`` to open the note's source span."""
    around = (span or {}).get("start") or None
    if source["kind"] == "claude-ai":
        return {"session": _CHAT_URL.match(source["url"]).group(1), "around": around}
    if source["kind"] == "claude-code":
        return {"session": source.get("job_id") or source["session_id"], "around": around}
    if source["kind"] == "record":
        return {"session": source["session"], "around": None}
    return None  # Duet transcripts are not indexed yet


def source_line(source: dict) -> str:
    if source["kind"] == "claude-ai":
        return f"claude.ai chat {source['url']}"
    if source["kind"] == "duet":
        return f"Duet channel {source['channel_id']} from message {source['message_id']}"
    if source["kind"] == "record":
        return f"history_record from {source['client']} ({source['session']})"
    line = f"Claude Code session {source['session_id']} on {source['machine']}"
    return line + (f", job {source['job_id']}" if source.get("job_id") else "")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(*, title: str, summary: str, source: dict, decisions=None, findings=None,
          links=None, span_start: str | None = None, span_end: str | None = None,
          author: str = "agent", corrects: str | None = None,
          counter: Counter | None = None, fallback: bool = False,
          record: dict | None = None, note_id: str | None = None,
          created_at: str | None = None) -> dict:
    """A validated, redacted note, ready for the wire. ``fallback`` marks a
    note built without a model summary (first prompt + last message)."""
    title = _clean(title, "title", MAX_TITLE, required=True)
    summary = _clean(summary, "summary", MAX_SUMMARY, required=True)
    corrects = _clean(corrects, "corrects", 40) or None
    if corrects and not NOTE_ID.match(corrects):
        raise NoteError("corrects must be a note id (note-…)")
    span = {"start": _clean(span_start, "span_start", 40) or None,
            "end": _clean(span_end, "span_end", 40) or None}
    note = {
        "v": FORMAT, "kind": "note",
        "note_id": note_id or "note-" + uuid.uuid4().hex[:20],
        "created_at": _clean(created_at, "created_at", 40) or _now(),
        "author": _clean(author, "author", 80) or "agent",
        "source": validate_source(source), "span": span,
        "title": title, "summary": summary,
        "decisions": _items("decisions", decisions),
        "findings": _items("findings", findings),
        "links": _items("links", links),
        "corrects": corrects,
        "fallback": bool(fallback),
    }
    if record is not None:
        note["origin"] = RECORD_ORIGIN
        note["record"] = _record_fields(record)
    elif note["source"]["kind"] == "record":
        raise NoteError("a record source is only for history_record")
    return redact_note(note, counter)


def _record_fields(record: dict) -> dict:
    """What a history_record note keeps of the record: its id, its chat
    session, its project and its exact searchable text."""
    record_id = str(record.get("record_id") or "")
    session = str(record.get("session") or "")
    if not _RECORD_ID.match(record_id) or not _RECORD_SESSION.match(session):
        raise NoteError("a record needs its rec-… id and claude-ai-chat-… session")
    return {"record_id": record_id, "session": session,
            "project": _clean(record.get("project"), "project", 200) or None,
            "text": _clean(record.get("text"), "record text", MAX_SUMMARY * 2, required=True)}


def record_note_id(record_id: str) -> str:
    """A record's note id, derived from its record id: however many times a
    record arrives (the MCP server, the diversion, the migration), it is one
    note."""
    return "note-" + hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:20]


def redact_note(note: dict, counter: Counter | None = None) -> dict:
    counter = counter if counter is not None else Counter()

    def clean(value):
        if isinstance(value, str):
            text, found = redact.redact(value)
            counter.update(found)
            return text
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value

    out = dict(note)
    for key in ("title", "summary", "decisions", "findings", "links", "author"):
        out[key] = clean(note.get(key))
    source = dict(note["source"])
    for key in ("channel_id", "message_id", "session_id", "machine"):
        if key in source:
            source[key] = clean(source[key])
    out["source"] = source
    if isinstance(note.get("record"), dict):
        out["record"] = {**note["record"], "text": clean(note["record"].get("text")),
                         "project": clean(note["record"].get("project"))}
    return out


def note_text(note: dict) -> str:
    """The searchable body of a note. A history_record note keeps the
    record's own text, so search and excerpts read exactly as before."""
    if note.get("origin") == RECORD_ORIGIN and isinstance(note.get("record"), dict):
        return note["record"]["text"]
    lines = [f"[note] {note['title']}", f"Source: {source_line(note['source'])}"]
    span = note.get("span") or {}
    if span.get("start") or span.get("end"):
        lines.append(f"Span: {span.get('start') or '?'} to {span.get('end') or '?'}")
    if note.get("fallback"):
        lines.append("Summary: fallback (first prompt and last message; the "
                     "model summary was unavailable)")
    lines += ["", note["summary"]]
    for heading, key in (("Decisions", "decisions"), ("Findings", "findings"),
                         ("Links", "links")):
        if note.get(key):
            lines += ["", f"{heading}:"] + [f"- {item}" for item in note[key]]
    if note.get("corrects"):
        lines += ["", f"Corrects {note['corrects']}"]
    return "\n".join(lines)


def parse(line: str) -> dict | None:
    """A wire line back to a note, or None if it is not a valid one."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != FORMAT or data.get("kind") != "note":
        return None
    try:
        if not NOTE_ID.match(str(data.get("note_id"))):
            return None
        span = data.get("span") if isinstance(data.get("span"), dict) else {}
        note = {
            "v": FORMAT, "kind": "note", "note_id": data["note_id"],
            "created_at": _clean(data.get("created_at"), "created_at", 40) or _now(),
            "author": _clean(data.get("author"), "author", 80) or "agent",
            "source": validate_source(data.get("source")),
            "span": {"start": _clean(span.get("start"), "span", 40) or None,
                     "end": _clean(span.get("end"), "span", 40) or None},
            "title": _clean(data.get("title"), "title", MAX_TITLE, required=True),
            "summary": _clean(data.get("summary"), "summary", MAX_SUMMARY, required=True),
            "decisions": _items("decisions", data.get("decisions")),
            "findings": _items("findings", data.get("findings")),
            "links": _items("links", data.get("links")),
            "corrects": _clean(data.get("corrects"), "corrects", 40) or None,
            "fallback": data.get("fallback") is True,
        }
        if data.get("origin") == RECORD_ORIGIN:
            note["origin"] = RECORD_ORIGIN
            note["record"] = _record_fields(data.get("record") or {})
            if note["note_id"] != record_note_id(note["record"]["record_id"]):
                return None
        elif note["source"]["kind"] == "record":
            return None
    except NoteError:
        return None
    return note


def write_file(directory: Path, note: dict) -> Path:
    """One immutable file per note: a hidden part file renamed into place."""
    directory.mkdir(parents=True, exist_ok=True)
    if not config.WINDOWS:
        directory.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{note['note_id']}.jsonl"
    final, part = directory / name, directory / f".{name}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(part, flags, 0o600), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(note, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if final.exists():
        part.unlink(missing_ok=True)
        raise NoteError("note id collision; retry")
    os.replace(part, final)
    return final


# --------------------------------------------------------------------- store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY,
    note_id      TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    machine      TEXT NOT NULL,
    author       TEXT,
    source_kind  TEXT NOT NULL,
    source       TEXT NOT NULL,
    show_session TEXT,
    show_around  TEXT,
    span_start   TEXT,
    span_end     TEXT,
    title        TEXT NOT NULL,
    text         TEXT NOT NULL,
    note         TEXT NOT NULL,
    corrects     TEXT,
    content_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_notes_show ON notes(show_session);
-- The one exception to append-only: `notes scrub` puts a note id here inside
-- its own transaction, rewrites that note's text, and takes it out again.
CREATE TABLE IF NOT EXISTS scrub_gate (note_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS notes_audit (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    note_id      TEXT NOT NULL,
    reason       TEXT NOT NULL,
    replacements INTEGER NOT NULL,
    fields       TEXT NOT NULL
);
DROP TRIGGER IF EXISTS notes_append_only_update;
CREATE TRIGGER notes_append_only_update BEFORE UPDATE ON notes
WHEN NOT EXISTS (SELECT 1 FROM scrub_gate WHERE note_id = OLD.note_id)
  OR NEW.note_id IS NOT OLD.note_id OR NEW.created_at IS NOT OLD.created_at
  OR NEW.machine IS NOT OLD.machine OR NEW.source IS NOT OLD.source
  OR NEW.source_kind IS NOT OLD.source_kind OR NEW.content_hash IS NOT OLD.content_hash
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS notes_append_only_delete BEFORE DELETE ON notes
BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS notes_audit_no_update BEFORE UPDATE ON notes_audit
BEGIN SELECT RAISE(ABORT, 'the audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS notes_audit_no_delete BEFORE DELETE ON notes_audit
BEGIN SELECT RAISE(ABORT, 'the audit trail is append-only'); END;
"""


def db_path() -> Path:
    return config.data_home() / "notes.db"


def _connect(path: Path) -> tuple[sqlite3.Connection, bool]:
    """A connection, with sqlite-vec loaded when this Python can."""
    from . import store

    try:
        return store._raw_connect(path), True
    except store.StoreError:
        return sqlite3.connect(path), False


def open_store(write: bool) -> tuple[sqlite3.Connection, bool]:
    """(connection, vectors usable). Read-only opens never create anything."""
    path = db_path()
    if not write:
        if not path.exists():
            db = sqlite3.connect(":memory:")
            db.executescript(_SCHEMA)
            return db, False
        db, vec = _connect(path)
    else:
        config.ensure_data_home()
        db, vec = _connect(path)
        config.secure_file(path)
        db.executescript(_SCHEMA)
        columns = {row[1] for row in db.execute("PRAGMA table_info(notes)")}
        for column in ("origin", "record_session", "project"):
            if column not in columns:
                db.execute(f"ALTER TABLE notes ADD COLUMN {column} TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_notes_record_session "
                   "ON notes(record_session)")
        db.commit()
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING "
                       "fts5(text, content='notes', content_rowid='id')")
        except sqlite3.OperationalError:
            pass
    if vec:
        vec = _vectors_ready(db, write)
    return db, vec


def _meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _vectors_ready(db: sqlite3.Connection, write: bool) -> bool:
    model, dim = _meta(db, "model"), _meta(db, "dimension")
    if model is None:
        if not write:
            return False
        try:
            dim = embed.probe_dimension()
        except embed.EmbedError:
            return False
        db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes "
                   f"USING vec0(embedding float[{int(dim)}])")
        db.execute("INSERT INTO meta(key, value) VALUES ('model', ?), ('dimension', ?)",
                   (config.model(), str(dim)))
        db.commit()
        return True
    return model == config.model()


def _fts(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name = 'notes_fts'").fetchone() is not None


def insert(db: sqlite3.Connection, note: dict, machine: str, vectors: bool) -> bool:
    """Store one note (redacted again here). False if it was already there.
    An embedder failure raises before anything is written; callers that must
    not wait for Ollama pass ``vectors=False``."""
    note = redact_note(note)
    text = note_text(note)
    digest = hashlib.sha256(json.dumps(
        [note["source"], note["span"], text], sort_keys=True).encode()).hexdigest()
    if db.execute("SELECT 1 FROM notes WHERE note_id = ? OR content_hash = ?",
                  (note["note_id"], digest)).fetchone():
        return False
    vector = embed.embed(text) if vectors else None  # before the write: a failure keeps nothing
    show = show_target(note["source"], note["span"]) or {}
    record = note.get("record") if note.get("origin") == RECORD_ORIGIN else None
    cursor = db.execute(
        "INSERT INTO notes (note_id, created_at, machine, author, source_kind, source, "
        "show_session, show_around, span_start, span_end, title, text, note, corrects, "
        "content_hash, origin, record_session, project) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (note["note_id"], note["created_at"], machine, note["author"],
         note["source"]["kind"], json.dumps(note["source"], sort_keys=True),
         show.get("session"), show.get("around"), note["span"]["start"],
         note["span"]["end"], note["title"], text,
         json.dumps(note, ensure_ascii=False, sort_keys=True), note["corrects"], digest,
         note.get("origin"), (record or {}).get("session"), (record or {}).get("project")))
    if _fts(db):
        db.execute("INSERT INTO notes_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, text))
    if vector is not None:
        db.execute("INSERT INTO vec_notes(rowid, embedding) VALUES (?, ?)",
                   (cursor.lastrowid, embed.pack(vector)))
    return True


_COLS = ("n.id, n.note_id, n.created_at, n.machine, n.author, n.source, "
         "n.show_session, n.show_around, n.span_start, n.span_end, n.title, n.text, "
         "n.corrects, n.origin, n.record_session, n.project")


def _row(row, score=None) -> dict:
    (_id, note_id, created, machine, author, source, show_session, show_around,
     start, end, title, text, corrects, origin, record_session, project) = row
    text = text or ""
    extra = ({"origin": origin, "record_session": record_session, "project": project,
              "text": text} if origin == RECORD_ORIGIN else {})
    return {
        **extra,
        "type": "note", "note_id": note_id, "created_at": created,
        "machine": machine, "author": author, "title": title,
        "source": json.loads(source),
        "span": {"start": start, "end": end},
        "show": ({"session": show_session, "around": show_around}
                 if show_session else None),
        "corrects": corrects, "score": score,
        "excerpt": text if len(text) <= EXCERPT_CHARS
        else text[:EXCERPT_CHARS].rstrip() + " …",
    }


def _text_hits(db, query: str, limit: int) -> list[int]:
    if not _fts(db):
        return []
    terms = _TOKEN.findall(query)
    if not terms:
        return []
    quote = lambda t: '"' + t.replace('"', '""') + '"'  # noqa: E731
    out: list[int] = []
    loose = [t for t in terms if t.lower() not in _STOP] or terms
    for match in (" ".join(map(quote, terms)), " OR ".join(map(quote, loose))):
        try:
            rows = db.execute("SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? "
                              "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        out += [r[0] for r in rows if r[0] not in out]
    return out[:limit]


def _vector_hits(db, query: str, limit: int) -> list[int]:
    vector = embed.pack(embed.embed(query))
    rows = db.execute("SELECT rowid FROM vec_notes WHERE embedding MATCH ? AND k = ? "
                      "ORDER BY distance", (vector, limit)).fetchall()
    return [r[0] for r in rows]


def search(query: str, *, k: int = 8, mode: str = "hybrid",
           records: bool | None = None, cwd: str | None = None,
           machine: str | None = None) -> list[dict]:
    """Notes only, best first. ``records``: True for history_record notes
    only, False for session notes only, None for both. ``cwd`` narrows
    records to a project substring, ``machine`` to the delivering machine."""
    if mode not in MODES:
        raise NoteError(f"mode must be one of {', '.join(MODES)}")
    query = (query or "").strip()
    if not query or not db_path().exists():
        return []
    db, vectors = open_store(write=False)
    try:
        pool = max(k * 5, 25)
        scores: dict[int, float] = {}
        lists = []
        if mode in ("hybrid", "text"):
            lists.append(_text_hits(db, query, pool))
        if mode in ("hybrid", "vector") and vectors:
            lists.append(_vector_hits(db, query, pool))
        for ranked in lists:
            for rank, rowid in enumerate(ranked):
                scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (60 + rank)
        out = []
        for rowid in sorted(scores, key=lambda r: -scores[r]):
            row = db.execute(f"SELECT {_COLS} FROM notes n WHERE n.id = ?", (rowid,)).fetchone()
            if not row:
                continue
            hit = _row(row, round(scores[rowid], 6))
            is_record = hit.get("origin") == RECORD_ORIGIN
            if records is not None and is_record != records:
                continue
            if cwd and cwd not in (hit.get("project") or ""):
                continue
            if machine and hit["machine"] != machine:
                continue
            out.append(hit)
            if len(out) >= k:
                break
        return out
    finally:
        db.close()


def records_for_session(session: str, *, limit: int = 50) -> list[dict]:
    """The history_record notes of one chat session, oldest first."""
    if not _RECORD_SESSION.match(session or "") or not db_path().exists():
        return []
    db, _ = open_store(write=False)
    try:
        try:
            rows = db.execute(f"SELECT {_COLS} FROM notes n WHERE n.record_session = ? "
                              "ORDER BY n.created_at, n.id", (session,)).fetchall()
        except sqlite3.OperationalError:  # a store from before records
            return []
        return [_row(r) for r in rows][-max(1, limit):]
    finally:
        db.close()


# ------------------------------------------------------- history_record notes

def _list_block(lines: list[str], heading: str) -> list[str]:
    try:
        start = lines.index(f"{heading}:") + 1
    except ValueError:
        return []
    out = []
    for line in lines[start:]:
        if not line.startswith("- "):
            break
        out.append(line[2:])
    return out


def from_legacy(text: str, *, session: str, machine: str, timestamp: str,
                origin: str) -> dict:
    """A note for a record written in the old format (a transcript line with
    agent claude-ai-chat, role summary, origin mcp:claude-ai-chat/rec-…)."""
    record_id = origin.rsplit("/", 1)[-1]
    lines = text.splitlines()
    first = lines[0] if lines else ""
    title = first.split("] ", 1)[1] if first.startswith("[claude.ai chat record] ") else first
    def field(label):
        return next((l.split(": ", 1)[1] for l in lines if l.startswith(f"{label}: ")), None)
    chat_url, project = field("Chat"), field("Project")
    try:
        summary_lines = lines[lines.index("Summary:") + 1:]
    except ValueError:
        summary_lines = lines[1:]
    summary = "\n".join(l for l in summary_lines if l and not l.endswith(":")
                        and not l.startswith("- ")).strip() or title
    try:
        source = make_source(chat_url=chat_url) if chat_url else record_source(machine, session)
    except NoteError:
        source = record_source(machine, session)
    return build(title=title[:MAX_TITLE] or "Chat record", summary=summary[:MAX_SUMMARY],
                 decisions=_list_block(lines, "Decisions")[:MAX_ITEMS],
                 findings=_list_block(lines, "Findings")[:MAX_ITEMS],
                 links=_list_block(lines, "Links")[:MAX_ITEMS],
                 source=source, author="history_record", note_id=record_note_id(record_id),
                 created_at=timestamp or None,
                 record={"record_id": record_id, "session": session, "project": project,
                         "text": text})


def legacy_record_line(data: dict) -> bool:
    """Whether a shipped/inbox line is a history_record in the old format."""
    return (isinstance(data, dict) and data.get("agent") == RECORD_SURFACE
            and data.get("role") == "summary"
            and str(data.get("origin") or "").startswith(f"mcp:{RECORD_SURFACE}/rec-"))


# ----------------------------------------------------------------- hub inbox

def _note_dirs() -> list[tuple[str, Path]]:
    root = config.inbox_dir()
    if not root or not root.is_dir():
        return []
    out = []
    for machine in sorted(root.iterdir()):
        notes = machine / NOTES_DIR
        if (_MACHINE.match(machine.name) and machine.is_dir() and not machine.is_symlink()
                and notes.is_dir() and not notes.is_symlink()):
            out.append((machine.name, notes))
    return out


def _pending_files() -> list[tuple[str, Path]]:
    out = []
    for machine, directory in _note_dirs():
        out += [(machine, f) for f in sorted(directory.glob("*.jsonl"))
                if f.is_file() and not f.is_symlink() and not f.name.startswith(".")]
    return out


def inbox_pending() -> bool:
    return bool(_pending_files())


def index_inbox() -> int:
    """Index notes waiting in the hub inbox; retire each file once committed.
    Returns 0, or 1 if a file had to wait (the embedder is down)."""
    files = _pending_files()
    if not files:
        return 0
    db, vectors = open_store(write=True)
    added = skipped = 0
    code = 0
    try:
        for machine, path in files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            try:
                for line in lines:
                    note = parse(line)
                    if note is None:
                        skipped += bool(line.strip())
                        continue
                    added += insert(db, note, machine, vectors)
                db.commit()
            except embed.EmbedError as exc:
                db.rollback()
                print(f"notes: embedding unavailable ({exc}); {path.name} waits",
                      file=sys.stderr)
                code = 1
                break
            _retire(machine, path)
    finally:
        db.close()
    print(f"notes: {added} new note(s)" + (f", {skipped} malformed line(s) skipped"
                                            if skipped else ""))
    return code


def _retire(machine: str, path: Path) -> None:
    root = config.inbox_dir()
    target = root / PROCESSED / machine / NOTES_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        destination = target / path.name
        n = 0
        while destination.exists():
            n += 1
            destination = target / f"{path.name}.{n}"
        os.replace(path, destination)
    except OSError as exc:
        print(f"notes: could not retire {machine}/{path.name}: {exc}", file=sys.stderr)


# --------------------------------------------------------------- local write

def outbox_dir() -> Path:
    return config.data_home() / "notes-outbox"


def save(note: dict) -> str:
    """Keep a note: queue it for the hub when one is configured (then ship
    the queue), else store it in this machine's notes.db. Returns where."""
    if config.hub_ingest():
        write_file(outbox_dir(), note)
        return "queued for the hub" if ship_outbox() == 0 else "queued (hub unreachable; kept)"
    db, vectors = open_store(write=True)
    try:
        insert(db, note, config.machine_id(), vectors)
        db.commit()
    finally:
        db.close()
    return "stored locally"


def ship_outbox() -> int:
    """Upload queued notes to ``<ingest>notes/`` through the ship transport."""
    from . import ship

    files = sorted(outbox_dir().glob("*.jsonl")) if outbox_dir().is_dir() else []
    if not files:
        return 0
    dest = config.hub_ingest()
    if not dest:
        return 1
    dest = dest + ("" if dest.endswith(("/", ":")) else "/") + NOTES_DIR + "/"
    try:
        ok = ship.upload(files, dest, config.hub_key())
    except Exception as exc:  # noqa: BLE001 - a transport must not lose notes
        print(f"notes: upload failed: {exc}", file=sys.stderr)
        ok = False
    if ok:
        for f in files:
            f.unlink(missing_ok=True)
    return 0 if ok else 1


# --------------------------------------------------------------------- sweep

_STATE = """
CREATE TABLE IF NOT EXISTS covered (
    session  TEXT PRIMARY KEY,
    last_ts  TEXT NOT NULL,
    note_id  TEXT
);
"""


def state_path() -> Path:
    return config.data_home() / "notes-state.db"


def _state(write: bool) -> sqlite3.Connection:
    path = state_path()
    if not write and not path.exists():
        db = sqlite3.connect(":memory:")
    elif not write:
        db = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
        return db
    else:
        config.ensure_data_home()
        db = sqlite3.connect(path)
        config.secure_file(path)
    db.executescript(_STATE)
    return db


def _parse_time(value: str) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _job_map() -> dict[str, str]:
    """Claude session id -> claude -p job id, from the job runner's
    directories when this machine has them (the same mapping ship-jobs uses:
    session ids on the job's transcript.jsonl lines and in job.json)."""
    raw = (config._env("AGENT_HISTORY_JOBS_DIR", "") or "").strip()
    root = Path(raw) if raw else Path("/var/lib/claude-jobs/jobs")
    out: dict[str, str] = {}
    try:
        dirs = [p for p in root.iterdir() if JOB_ID.match(p.name) and p.is_dir()]
    except OSError:
        return out
    for job in dirs:
        sids: list = []
        try:
            meta = json.loads((job / "job.json").read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                sids += [meta.get("session_id")] + list(meta.get("session_ids") or [])
        except (OSError, ValueError, TypeError):
            pass
        try:
            with (job / "transcript.jsonl").open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        sids.append(json.loads(line).get("session_id"))
                    except (ValueError, AttributeError):
                        continue
        except OSError:
            pass
        for sid in sids:
            if isinstance(sid, str) and sid:
                out.setdefault(sid, job.name)
    return out


def _sessions() -> dict[str, list]:
    """Claude Code messages on this machine, by session, oldest first."""
    from .adapters.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter()
    by_session: dict[str, list] = {}
    if not adapter.available():
        return by_session
    for record in adapter.records():
        if not record.session_id or record.session_id.startswith(("memory:", "doc:")):
            continue
        if record.role not in ("user", "assistant") or not record.timestamp:
            continue
        by_session.setdefault(record.session_id, []).append(record)
    for records in by_session.values():
        records.sort(key=lambda r: r.timestamp)
    return by_session


def last_message(span: dict) -> dict:
    """The fallback when the model summary fails: no model. Title from the
    first prompt, summary from the last assistant message."""
    messages = span["messages"]
    first = next((m["text"] for m in messages if m["role"] == "user"), "")
    last = next((m["text"] for m in reversed(messages) if m["role"] == "assistant"), "")
    title = " ".join(first.split())[:150] or "Untitled session"
    summary = last.strip()[:4000] or " ".join(first.split())[:4000]
    return {"title": title, "summary": summary or title}


def _child_env() -> dict:
    """The summarizer's environment: everything but ANTHROPIC_API_KEY, so a
    model call can only use the machine's Claude login (on claude-jobs, the
    subscription token), never the production API key."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


def _json_object(text: str) -> dict:
    """The JSON object in a model's reply, tolerating a code fence."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise NoteError("the model reply has no JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        raise NoteError("the model reply is not valid JSON") from None
    if not isinstance(value, dict):
        raise NoteError("the model reply is not a JSON object")
    return value


def claude_summary(span: dict) -> dict:
    """The default summarizer: one `claude -p` call with a small model."""
    binary = (config._env("AGENT_HISTORY_CLAUDE_BIN", "") or "").strip() or "claude"
    model = (config._env("AGENT_HISTORY_NOTES_MODEL", "") or "").strip() or DEFAULT_MODEL
    argv = [binary, "-p", SUMMARIZER_PROMPT, "--model", model, "--output-format", "json",
            "--max-turns", "1", "--no-session-persistence", "--tools", ""]
    done = subprocess.run(argv, input=json.dumps(span), text=True, capture_output=True,
                          timeout=SUMMARIZER_TIMEOUT, env=_child_env())
    if done.returncode != 0:
        raise NoteError(f"claude exited {done.returncode}: "
                        f"{(done.stderr or done.stdout or '').strip()[-200:]}")
    try:
        envelope = json.loads(done.stdout)
    except ValueError:
        raise NoteError("claude printed no JSON envelope") from None
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise NoteError(f"claude reported an error: {str(envelope)[:200]}")
    return _json_object(str(envelope.get("result") or ""))


def _summarize(command: str, span: dict) -> dict:
    if command == DEFAULT_SUMMARIZER:
        return claude_summary(span)
    # Windows parses a command line itself; POSIX shlex would eat backslashes.
    argv = command if config.WINDOWS else shlex.split(command)
    done = subprocess.run(argv, input=json.dumps(span), text=True,
                          capture_output=True, timeout=SUMMARIZER_TIMEOUT,
                          env=_child_env())
    if done.returncode != 0:
        raise NoteError(f"summarizer exited {done.returncode}: "
                        f"{(done.stderr or '').strip()[-200:]}")
    try:
        out = json.loads(done.stdout)
    except ValueError:
        raise NoteError("summarizer printed no JSON object") from None
    if not isinstance(out, dict):
        raise NoteError("summarizer printed no JSON object")
    return out


def sweep(*, idle_minutes: float = DEFAULT_IDLE_MINUTES, summarizer: str | None = None,
          session: str | None = None, now: datetime.datetime | None = None,
          dry_run: bool = False) -> int:
    """Write a note for each session that ended (``session`` given) or went
    idle, covering only what came after its last note."""
    summarizer = (summarizer or (config._env("AGENT_HISTORY_NOTES_SUMMARIZER", "") or "").strip()
                  or DEFAULT_SUMMARIZER)
    if summarizer == "last-message":
        print("notes sweep: last-message is only the fallback now; leave "
              "AGENT_HISTORY_NOTES_SUMMARIZER unset for the model summary", file=sys.stderr)
        return 2
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(minutes=idle_minutes)
    state = _state(write=not dry_run)
    jobs = _job_map()
    machine = config.machine_id()
    written = failed = 0
    try:
        for sid, records in _sessions().items():
            if session and sid != session:
                continue
            row = state.execute("SELECT last_ts FROM covered WHERE session = ?",
                                (sid,)).fetchone()
            covered = row[0] if row else ""
            if records[0].text.startswith(SUMMARIZER_MARK):
                continue  # a summarizer's own call, if it was ever persisted
            fresh = [r for r in records if r.timestamp > covered]
            if not fresh:
                continue
            last = _parse_time(fresh[-1].timestamp)
            if not session and (last is None or last > cutoff):
                continue  # still active
            source = make_source(session_id=sid, machine=machine, job_id=jobs.get(sid))
            span = {"source": source,
                    "span": {"start": fresh[0].timestamp, "end": fresh[-1].timestamp},
                    "resumed": bool(covered),
                    "messages": [{"timestamp": r.timestamp, "role": r.role,
                                  "text": redact.redact(r.text[:MAX_MESSAGE_CHARS])[0]}
                                 for r in fresh[-MAX_SPAN_MESSAGES:]]}
            if dry_run:
                print(f"notes sweep: would note {sid} ({len(fresh)} message(s))")
                continue
            name = summarizer.split()[0].rsplit("/", 1)[-1]
            try:
                summary = _summarize(summarizer, span)
                note = build(title=summary.get("title"), summary=summary.get("summary"),
                             decisions=summary.get("decisions"),
                             findings=summary.get("findings"), links=summary.get("links"),
                             source=source, span_start=fresh[0].timestamp,
                             span_end=fresh[-1].timestamp, author=f"sweep:{name}")
            except (NoteError, subprocess.TimeoutExpired, OSError) as exc:
                print(f"notes sweep: {sid}: model summary failed ({exc}); "
                      "writing a fallback note", file=sys.stderr)
                failed += 1
                summary = last_message(span)
                note = build(title=summary["title"], summary=summary["summary"],
                             source=source, span_start=fresh[0].timestamp,
                             span_end=fresh[-1].timestamp, author="sweep:fallback",
                             fallback=True)
            where = save(note)
            state.execute("INSERT INTO covered(session, last_ts, note_id) VALUES (?,?,?) "
                          "ON CONFLICT(session) DO UPDATE SET last_ts = excluded.last_ts, "
                          "note_id = excluded.note_id",
                          (sid, fresh[-1].timestamp, note["note_id"]))
            state.commit()
            written += 1
            print(f"notes sweep: {sid}: {note['note_id']} {where}")
    finally:
        state.close()
    if not dry_run and config.hub_ingest():
        ship_outbox()  # anything left from an earlier unreachable round
    print(f"notes sweep: {written} note(s) written"
          + (f", {failed} from the fallback" if failed else ""))
    return 1 if failed else 0


def hook_session_end(stdin: Iterable[str] | None = None) -> int:
    """For a Claude Code SessionEnd hook: start a sweep of that one session in
    the background and return at once (a summarizer can take a while)."""
    try:
        data = json.loads("".join(stdin if stdin is not None else sys.stdin))
    except ValueError:
        return 0
    sid = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(sid, str) or not _ID.match(sid):
        return 0
    argv = [sys.executable, "-m", "agent_history", "notes", "sweep", "--session", sid]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if config.WINDOWS:
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
    except OSError:
        pass
    return 0


# --------------------------------------------------------------------- scrub

def _replace_all(value, pattern: str, counter: list[int]):
    if isinstance(value, str):
        counter[0] += value.count(pattern)
        return value.replace(pattern, SCRUB_MARKER)
    if isinstance(value, list):
        return [_replace_all(v, pattern, counter) for v in value]
    if isinstance(value, dict):
        return {k: _replace_all(v, pattern, counter) for k, v in value.items()}
    return value


def _actor() -> str:
    try:
        return os.environ.get("SUDO_USER") or getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry
        return "unknown"


def scrub(note_id: str, pattern: str, reason: str) -> dict:
    """Replace a leaked secret in one note: its stored JSON, searchable text,
    full-text entry and embedding, plus its raw copy in the inbox archive.
    Appends an audit record that never contains the secret. The only write
    path that may change a stored note (see the UPDATE trigger)."""
    if not NOTE_ID.match(note_id or ""):
        raise NoteError("not a note id (note-…)")
    if not pattern or len(pattern) < 4:
        raise NoteError("the pattern must be at least 4 characters")
    reason = _clean(reason, "reason", 500, required=True)
    if pattern in reason:
        raise NoteError("the reason contains the secret; describe it instead")
    reason = redact.redact(reason)[0]
    if not db_path().exists():
        raise NoteError("no notes store here")
    db, vectors = open_store(write=True)
    try:
        row = db.execute("SELECT id, title, text, note FROM notes WHERE note_id = ?",
                         (note_id,)).fetchone()
        if row is None:
            raise NoteError(f"no note {note_id}")
        rowid, title, text, stored = row
        counts = {}
        new = {}
        for field, value in (("title", title), ("text", text)):
            n = [0]
            new[field] = _replace_all(value, pattern, n)
            counts[field] = n[0]
        n = [0]
        new["note"] = json.dumps(_replace_all(json.loads(stored), pattern, n),
                                 ensure_ascii=False, sort_keys=True)
        counts["note"] = n[0]
        total = sum(counts.values())
        if not total:
            raise NoteError("the pattern does not occur in that note")
        vector = None
        if vectors:
            try:
                vector = embed.embed(new["text"])
            except embed.EmbedError:
                vector = None  # the old vector goes regardless; text search still works
        with db:  # one transaction: gate, rewrite, index, ungate, audit
            db.execute("INSERT INTO scrub_gate(note_id) VALUES (?)", (note_id,))
            if _fts(db):
                db.execute("INSERT INTO notes_fts(notes_fts, rowid, text) "
                           "VALUES ('delete', ?, ?)", (rowid, text))
            db.execute("UPDATE notes SET title = ?, text = ?, note = ? WHERE id = ?",
                       (new["title"], new["text"], new["note"], rowid))
            if _fts(db):
                db.execute("INSERT INTO notes_fts(rowid, text) VALUES (?, ?)",
                           (rowid, new["text"]))
            if vectors:
                db.execute("DELETE FROM vec_notes WHERE rowid = ?", (rowid,))
                if vector is not None:
                    db.execute("INSERT INTO vec_notes(rowid, embedding) VALUES (?, ?)",
                               (rowid, embed.pack(vector)))
            db.execute("DELETE FROM scrub_gate WHERE note_id = ?", (note_id,))
            fields = sorted(k for k, v in counts.items() if v)
            db.execute(
                "INSERT INTO notes_audit (at, actor, action, note_id, reason, "
                "replacements, fields) VALUES (?, ?, 'scrub', ?, ?, ?, ?)",
                (_now(), _actor(), note_id, reason, total, json.dumps(fields)))
    finally:
        db.close()
    archived = _scrub_archive(note_id, pattern)
    return {"note_id": note_id, "replacements": total, "fields": fields,
            "archive_files": archived, "reembedded": vector is not None}


def _scrub_archive(note_id: str, pattern: str) -> int:
    """Rewrite the note's raw copy under inbox/.processed/<machine>/notes/."""
    root = config.inbox_dir()
    if not root:
        return 0
    rewritten = 0
    for path in (root / PROCESSED).glob(f"*/{NOTES_DIR}/*{note_id}.jsonl*"):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern not in body:
            continue
        lines = []
        for line in body.splitlines():
            try:
                lines.append(json.dumps(_replace_all(json.loads(line), pattern, [0]),
                                        ensure_ascii=False))
            except ValueError:
                lines.append(line.replace(pattern, SCRUB_MARKER))
        part = path.with_name(f".{path.name}.part")
        part.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(part, path)
        rewritten += 1
    return rewritten


def audit_log() -> list[dict]:
    if not db_path().exists():
        return []
    db, _ = open_store(write=False)
    try:
        return [{"at": at, "actor": actor, "action": action, "note_id": nid,
                 "reason": reason, "replacements": n, "fields": json.loads(fields)}
                for at, actor, action, nid, reason, n, fields in db.execute(
                    "SELECT at, actor, action, note_id, reason, replacements, fields "
                    "FROM notes_audit ORDER BY id")]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


# ----------------------------------------------------------- record migration

def _stitch(pieces: list[str]) -> tuple[str, bool]:
    """Join a record's chunks back into its text. Chunks overlap by up to
    CHUNK_OVERLAP characters; returns (text, every join found its overlap)."""
    from .textproc import CHUNK_OVERLAP

    text, clean = pieces[0], True
    for piece in pieces[1:]:
        overlap = 0
        for n in range(min(len(text), len(piece), CHUNK_OVERLAP + 100), 0, -1):
            if text.endswith(piece[:n]):
                overlap = n
                break
        clean = clean and overlap > 0
        text += piece[overlap:]
    return text, clean


def migrate_records(apply: bool = False) -> int:
    """Move history_record entries out of the transcript index into notes.

    Dry run unless ``apply``. Per record: write the note (idempotent: the id
    comes from the record id), read it back, append the record's chunks to a
    rollback file, and only then delete them from the transcript index."""
    from . import store

    if not config.db_path().exists():
        print("migrate-records: no transcript index here", file=sys.stderr)
        return 1
    db = store.open_for_maintenance()
    try:
        rows = db.execute(
            "SELECT id, origin, session_id, timestamp, text, machine FROM chunks "
            "WHERE agent = ? AND role = 'summary' AND origin LIKE ? "
            "ORDER BY origin, coalesce(chunk_ordinal, 0), coalesce(char_start, 0), id",
            (RECORD_SURFACE, f"mcp:{RECORD_SURFACE}/rec-%")).fetchall()
        groups: dict[str, list[tuple]] = {}
        for row in rows:
            groups.setdefault(row[1], []).append(row)
        plans = []
        for origin, chunks in groups.items():
            text, clean = _stitch([c[4] or "" for c in chunks])
            machine = chunks[0][5] or config.machine_id()
            try:
                note = from_legacy(text, session=chunks[0][2] or "", machine=machine,
                                   timestamp=chunks[0][3] or "", origin=origin)
            except NoteError as exc:
                print(f"migrate-records: skipping {origin}: {exc}", file=sys.stderr)
                continue
            plans.append((origin, chunks, note, machine, clean))
            flag = "" if clean else "  [chunks joined without a clean overlap]"
            print(f"  {note['note_id']}  {note['record']['session']}  "
                  f"{len(chunks)} chunk(s)  {note['title'][:70]}{flag}")
        print(f"migrate-records: {len(plans)} record(s), {sum(len(p[1]) for p in plans)} "
              "chunk(s) in the transcript index")
        if not apply:
            print("migrate-records: dry run, nothing written. Run with --yes to move them.")
            return 0
        if not plans:
            return 0
        ndb, vectors = open_store(write=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = config.data_home() / f"migrate-records-{stamp}.jsonl"
        moved = 0
        try:
            for origin, chunks, note, machine, _clean_join in plans:
                try:
                    insert(ndb, note, machine, vectors)
                except embed.EmbedError:
                    insert(ndb, note, machine, False)   # text search still works
                ndb.commit()
                if not ndb.execute("SELECT 1 FROM notes WHERE note_id = ?",
                                   (note["note_id"],)).fetchone():
                    print(f"migrate-records: {origin}: note not readable; chunks kept",
                          file=sys.stderr)
                    continue
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    for cid, corigin, sid, ts, text, cmachine in chunks:
                        fh.write(json.dumps({"id": cid, "origin": corigin, "session_id": sid,
                                             "timestamp": ts, "machine": cmachine,
                                             "text": text}, ensure_ascii=False) + "\n")
                store._delete_rows(db, [(c[0], c[4]) for c in chunks])
                db.commit()
                moved += 1
        finally:
            ndb.close()
        print(f"migrate-records: moved {moved} record(s) into notes; rollback copy of "
              f"the removed chunks: {backup}")
        return 0 if moved == len(plans) else 1
    finally:
        db.close()
