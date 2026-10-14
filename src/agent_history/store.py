"""SQLite + sqlite-vec storage layer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from . import config, embed


class StoreError(RuntimeError):
    pass


EXTENSION_HELP = (
    "This Python's sqlite3 module was built without extension support, so the "
    "sqlite-vec vector index cannot be loaded.\n"
    "It is a property of the Python build, not of your machine — commonly seen "
    "in Homebrew/uv macOS builds and some Conda and distribution packages.\n"
    "Check with:\n"
    "  python3 -c \"import sqlite3; "
    "sqlite3.connect(':memory:').enable_load_extension(True)\"\n"
    "Then install agent-history under a Python where that succeeds."
)


def extensions_supported() -> bool:
    """Whether this interpreter's sqlite3 can load extensions at all."""
    db = sqlite3.connect(":memory:")
    try:
        if not hasattr(db, "enable_load_extension"):
            return False
        db.enable_load_extension(True)
        return True
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
        return False
    finally:
        db.close()


def _raw_connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    if not hasattr(db, "enable_load_extension"):
        db.close()
        raise StoreError(EXTENSION_HELP)
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError) as exc:
        db.close()
        raise StoreError(f"{EXTENSION_HELP}\n(underlying error: {exc})") from exc
    return db


def _get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def open_for_write() -> tuple[sqlite3.Connection, int]:
    """Open (creating if needed) and return the connection plus vector dimension.

    On a fresh database the configured model is probed for its dimension, and
    both are recorded. On an existing one they are checked, so switching models
    fails loudly instead of silently writing mismatched vectors.
    """
    path = config.db_path()
    config.ensure_data_home()
    db = _raw_connect(path)
    config.secure_file(path)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id           INTEGER PRIMARY KEY,
            agent        TEXT NOT NULL DEFAULT 'claude-code',
            origin       TEXT,
            session_id   TEXT,
            cwd          TEXT,
            timestamp    TEXT,
            role         TEXT,
            text         TEXT,
            content_hash TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_origin ON chunks(origin);
        CREATE INDEX IF NOT EXISTS idx_chunks_agent  ON chunks(agent);
        """
    )

    _ensure_fts(db)

    stored_model = _get_meta(db, "model")
    stored_dim = _get_meta(db, "dimension")
    wanted = config.model()

    if stored_model is None:
        dimension = embed.probe_dimension()
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks "
            f"USING vec0(embedding float[{dimension}])"
        )
        _set_meta(db, "model", wanted)
        _set_meta(db, "dimension", str(dimension))
        db.commit()
        return db, dimension

    if stored_model != wanted:
        raise StoreError(
            f"index was built with model '{stored_model}' but '{wanted}' is "
            f"configured. Vectors from different models are not comparable.\n"
            f"Run `agent-history reindex` to rebuild, or set "
            f"AGENT_HISTORY_MODEL={stored_model} to keep using the existing index."
        )
    return db, int(stored_dim or 0)


def open_for_read(check_model: bool = True) -> sqlite3.Connection:
    """Open the index read-only-ish.

    `check_model=False` skips the embedding-model identity check — correct for
    text-only search, which never embeds and works fine against an index built
    with any model.
    """
    path = config.db_path()
    if not path.exists():
        raise StoreError(
            f"no index at {path}. Run: agent-history index"
        )
    db = _raw_connect(path)
    stored_model = _get_meta(db, "model")
    wanted = config.model()
    if check_model and stored_model and stored_model != wanted:
        raise StoreError(
            f"index was built with model '{stored_model}' but '{wanted}' is "
            f"configured; a query embedded with a different model cannot match.\n"
            f"Run `agent-history reindex`, or set AGENT_HISTORY_MODEL={stored_model}."
        )
    return db


def open_for_maintenance() -> sqlite3.Connection:
    """Open the index with no model-identity check.

    For operations that work on stored text or vectors regardless of which
    model produced them — re-embedding foremost.
    """
    path = config.db_path()
    if not path.exists():
        raise StoreError(f"no index at {path}. Run: agent-history index")
    return _raw_connect(path)


def orphaned_counts(db: sqlite3.Connection) -> tuple[int, int]:
    """(chunks, origins) whose source no longer exists on disk.

    These chunks are the ones `reindex` would destroy irrecoverably — the
    index is their only surviving copy. `db:<path>#<table>` origins are
    checked against the database file path inside them.
    """
    from pathlib import Path

    rows = db.execute(
        "SELECT origin, count(*) FROM chunks GROUP BY origin"
    ).fetchall()
    chunks = origins = 0
    for origin, count in rows:
        path = origin
        if origin.startswith("db:"):
            path = origin[3:].split("#", 1)[0]
        if not Path(path).exists():
            origins += 1
            chunks += count
    return chunks, origins


def reset() -> None:
    """Delete the index outright, so a rebuild starts from nothing.

    Removing the file rather than emptying the tables means a reindex onto a
    different model recreates the vector table at the new dimension instead of
    inheriting the old one.

    Windows refuses to unlink a file that is still open, and sqlite3 keeps
    connections alive until they are garbage collected, so collect first and
    fall back to emptying the tables if the file is still held.
    """
    import gc

    gc.collect()
    path = config.db_path()
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        try:
            target.unlink(missing_ok=True)
        except PermissionError:
            if suffix:
                continue
            # Still open somewhere: clear it in place instead. The vector table
            # is dropped so a new dimension can be created on the next open.
            db = _raw_connect(target)
            try:
                db.execute("DELETE FROM chunks")
                db.execute("DROP TABLE IF EXISTS vec_chunks")
                db.execute("DROP TABLE IF EXISTS vec_chunks_new")
                db.execute("DROP TABLE IF EXISTS fts_chunks")
                db.execute("DELETE FROM meta WHERE key IN "
                           "('model', 'dimension', 'reembed_model')")
                db.commit()
            finally:
                db.close()


def _ensure_fts(db: sqlite3.Connection) -> None:
    """Create the full-text index, backfilling an index built before it existed.

    FTS5 ships in practically every SQLite build, but it is a compile-time
    option — when it is absent the store simply works vector-only.
    """
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks "
            "USING fts5(text, content='chunks', content_rowid='id')"
        )
        # count(*) on an external-content FTS table delegates to the content
        # table, so it cannot reveal an empty index; the docsize shadow table
        # holds one row per actually-indexed document.
        indexed = db.execute("SELECT count(*) FROM fts_chunks_docsize").fetchone()[0]
        stored = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if indexed < stored:
            db.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")
            db.commit()
    except sqlite3.OperationalError:
        return


def fts_enabled(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fts_chunks'"
    ).fetchone()
    return row is not None


def existing_hashes(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT content_hash FROM chunks")}


def insert_chunk(
    db: sqlite3.Connection,
    *,
    agent: str,
    origin: str,
    session_id: str,
    cwd: str,
    timestamp: str,
    role: str,
    text: str,
    content_hash: str,
    vector: list[float],
) -> None:
    cursor = db.execute(
        "INSERT INTO chunks "
        "(agent, origin, session_id, cwd, timestamp, role, text, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (agent, origin, session_id, cwd, timestamp, role, text, content_hash),
    )
    db.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
        (cursor.lastrowid, embed.pack(vector)),
    )
    if fts_enabled(db):
        db.execute(
            "INSERT INTO fts_chunks(rowid, text) VALUES (?, ?)",
            (cursor.lastrowid, text),
        )


def query(
    db: sqlite3.Connection,
    vector: bytes,
    k: int,
    *,
    cwd: str | None = None,
    agent: str | None = None,
) -> list[tuple]:
    """Nearest neighbours, optionally narrowed by cwd substring or agent.

    Filters are applied after the vector search, so we over-fetch when one is
    present to avoid returning fewer than k results.
    """
    fetch = k * 4 if (cwd or agent) else k
    rows = db.execute(
        "SELECT c.agent, c.origin, c.session_id, c.cwd, c.timestamp, c.role, "
        "       c.text, v.distance "
        "FROM vec_chunks v JOIN chunks c ON c.id = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        (vector, fetch),
    ).fetchall()

    return _filter(rows, cwd=cwd, agent=agent)[:k]


_ROW_COLS = "c.agent, c.origin, c.session_id, c.cwd, c.timestamp, c.role, c.text"


def text_query(
    db: sqlite3.Connection,
    query: str,
    k: int,
    *,
    cwd: str | None = None,
    agent: str | None = None,
) -> list[tuple]:
    """BM25-ranked full-text matches, same row shape as query() minus distance.

    The query is quoted term-by-term so FTS5 operator syntax in ordinary
    prose ("don't", "a-b", stray quotes) cannot break the MATCH expression.
    """
    if not fts_enabled(db):
        return []
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    match = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
    fetch = k * 4 if (cwd or agent) else k
    try:
        rows = db.execute(
            f"SELECT {_ROW_COLS} "
            "FROM fts_chunks f JOIN chunks c ON c.id = f.rowid "
            "WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
            (match, fetch),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return _filter(rows, cwd=cwd, agent=agent)[:k]


def hybrid_query(
    db: sqlite3.Connection,
    vector: bytes,
    query_text: str,
    k: int,
    *,
    cwd: str | None = None,
    agent: str | None = None,
) -> list[tuple]:
    """Merge vector and full-text rankings with reciprocal rank fusion.

    Each list contributes 1/(60+rank) per hit; a chunk found by both ranks
    above one found by either alone. Returns rows shaped like query() with an
    RRF score appended: (..., distance | None, score).
    """
    vec_rows = query(db, vector, k * 2, cwd=cwd, agent=agent)
    txt_rows = text_query(db, query_text, k * 2, cwd=cwd, agent=agent)

    scored: dict[tuple, list] = {}
    for rank, row in enumerate(vec_rows):
        key = row[:7]
        scored[key] = [row[7], 1.0 / (60 + rank)]
    for rank, row in enumerate(txt_rows):
        key = row[:7]
        if key in scored:
            scored[key][1] += 1.0 / (60 + rank)
        else:
            scored[key] = [None, 1.0 / (60 + rank)]

    merged = sorted(scored.items(), key=lambda item: -item[1][1])
    return [(*key, distance, score) for key, (distance, score) in merged[:k]]


def _filter(rows: list[tuple], *, cwd: str | None, agent: str | None) -> list[tuple]:
    if agent:
        rows = [r for r in rows if r[0] == agent]
    if cwd:
        rows = [r for r in rows if cwd in (r[3] or "")]
    return rows


def session_rows(
    db: sqlite3.Connection,
    *,
    origin: str | None = None,
    session_id: str | None = None,
) -> list[tuple]:
    """Every chunk of one conversation, oldest first.

    This reads the index, not the source transcript — deliberately, since for
    a mature index most source files no longer exist.
    """
    where, params = [], []
    if origin:
        where.append("c.origin = ?")
        params.append(origin)
    if session_id:
        where.append("c.session_id = ?")
        params.append(session_id)
    if not where:
        return []
    return db.execute(
        f"SELECT {_ROW_COLS} FROM chunks c WHERE {' OR '.join(where)} "
        "ORDER BY c.timestamp, c.id",
        params,
    ).fetchall()


# Chunks that share a session id without being a conversation of their own:
# memory files and docs get synthetic `memory:` / `doc:` ids, and Claude Code
# subagent transcripts carry their parent's session id. Both separators are
# matched because a native-Windows origin keeps its backslashes.
_PSEUDO_SESSION = (
    "(c.session_id LIKE 'memory:%' OR c.session_id LIKE 'doc:%' "
    "OR c.origin LIKE '%/subagents/%' OR c.origin LIKE '%\\subagents\\%')"
)


def has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column
               for row in db.execute(f"PRAGMA table_info({table})"))


def session_summaries(
    db: sqlite3.Connection,
    *,
    cwd: str | None = None,
    agent: str | None = None,
    include_pseudo: bool = False,
) -> list[tuple]:
    """One row per session: (agent, session_id, cwd, last_timestamp, machine).

    Unordered — the caller sorts, because timestamps from different adapters
    are not lexicographically comparable. Within one session they are (one
    adapter wrote them all), so max() picks the latest chunk, and SQLite's
    bare-column rule makes cwd/machine come from that same row. `machine`
    is None on indexes without the column (it arrives with the 0.17 hub).
    """
    machine = "c.machine" if has_column(db, "chunks", "machine") else "NULL"
    where, params = ["c.session_id != ''"], []
    if not include_pseudo:
        where.append(f"NOT {_PSEUDO_SESSION}")
    if agent:
        where.append("c.agent = ?")
        params.append(agent)
    if cwd:
        where.append("instr(c.cwd, ?) > 0")  # same substring rule as search
        params.append(cwd)
    return db.execute(
        f"SELECT c.agent, c.session_id, c.cwd, max(c.timestamp), {machine} "
        f"FROM chunks c WHERE {' AND '.join(where)} "
        "GROUP BY c.agent, c.session_id",
        params,
    ).fetchall()


def first_prompts(
    db: sqlite3.Connection,
    session_ids: list[str],
    *,
    include_pseudo: bool = False,
) -> dict[str, str]:
    """The earliest user chunk of each given session, keyed by session id.

    Subagent chunks are skipped unless `include_pseudo`, so a session's first
    prompt is what the user typed, not a task the agent handed a subagent.
    """
    if not session_ids:
        return {}
    marks = ",".join("?" * len(session_ids))
    where = [f"c.session_id IN ({marks})", "c.role = 'user'"]
    if not include_pseudo:
        where.append(f"NOT {_PSEUDO_SESSION}")
    rows = db.execute(
        f"SELECT c.session_id, min(c.timestamp), c.text FROM chunks c "
        f"WHERE {' AND '.join(where)} GROUP BY c.session_id",
        session_ids,
    ).fetchall()
    return {session_id: text for session_id, _first, text in rows}


def _delete_rows(db: sqlite3.Connection, rows: list[tuple]) -> int:
    """Delete (id, text) rows from chunks, vec_chunks, and fts_chunks."""
    fts = fts_enabled(db)
    for rowid, text in rows:
        db.execute("DELETE FROM chunks WHERE id = ?", (rowid,))
        db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
        if fts:
            # External-content FTS5 cannot look up a deleted row's text
            # itself; the 'delete' command must be handed it.
            db.execute(
                "INSERT INTO fts_chunks(fts_chunks, rowid, text) "
                "VALUES('delete', ?, ?)",
                (rowid, text),
            )
    return len(rows)


def purge_targets(
    db: sqlite3.Connection,
    *,
    cwd: str | None = None,
    session: str | None = None,
    agent: str | None = None,
    everything: bool = False,
) -> list[tuple]:
    """(id, text) rows a purge would remove — computed before deleting so the
    caller can preview a count and require confirmation.

    Filters are AND-combined (narrowing). `everything=True` targets all rows.

    Dedup caveat: content-hash dedup stores identical text once, attributed to
    its first-indexed source. So `cwd`/`session` purges match a chunk by its
    *stored* attribution; verbatim text that also appeared in another project
    is purged under whichever source indexed it first. `--all` is the only
    guaranteed-complete destruction.
    """
    if everything:
        return db.execute("SELECT id, text FROM chunks").fetchall()
    clauses, params = [], []
    if cwd:
        clauses.append("cwd LIKE ?")
        params.append(f"%{cwd}%")
    if session:
        clauses.append("session_id = ?")
        params.append(session)
    if agent:
        clauses.append("agent = ?")
        params.append(agent)
    if not clauses:
        return []
    return db.execute(
        f"SELECT id, text FROM chunks WHERE {' AND '.join(clauses)}", params
    ).fetchall()


def purge(db: sqlite3.Connection, rows: list[tuple]) -> int:
    """Permanently delete the given (id, text) rows. Returns the count."""
    removed = _delete_rows(db, rows)
    if removed:
        db.commit()
    return removed


def remove_stale(
    db: sqlite3.Connection,
    targets: list[tuple[str, str]],
    keep: set[str],
) -> int:
    """Delete chunks of mutable origins whose text no longer exists.

    This is the store's only delete path, and it is deliberately narrow:
    `targets` is the (agent, origin) list of mutable sources scanned in the
    current run, and `keep` is every content hash any adapter produced this
    run. A row is removed only when its origin was re-read AND its text was
    produced by nothing at all — so transcript chunks (never in `targets`)
    are untouchable, an origin that vanished from disk keeps its chunks, and
    a hash shared with any other source survives even if this origin dropped
    it.
    """
    removed = 0
    for agent, origin in targets:
        rows = db.execute(
            "SELECT id, content_hash, text FROM chunks "
            "WHERE agent = ? AND origin = ?",
            (agent, origin),
        ).fetchall()
        doomed = [(rowid, text) for rowid, digest, text in rows
                  if digest not in keep]
        removed += _delete_rows(db, doomed)
    if removed:
        db.commit()
    return removed


def stats(db: sqlite3.Connection) -> dict:
    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    per_agent = dict(
        db.execute("SELECT agent, count(*) FROM chunks GROUP BY agent ORDER BY 2 DESC")
    )
    return {
        "total": total,
        "per_agent": per_agent,
        "model": _get_meta(db, "model"),
        "dimension": _get_meta(db, "dimension"),
    }
