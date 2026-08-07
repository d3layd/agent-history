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


def open_for_read() -> sqlite3.Connection:
    path = config.db_path()
    if not path.exists():
        raise StoreError(
            f"no index at {path}. Run: agent-history index"
        )
    db = _raw_connect(path)
    stored_model = _get_meta(db, "model")
    wanted = config.model()
    if stored_model and stored_model != wanted:
        raise StoreError(
            f"index was built with model '{stored_model}' but '{wanted}' is "
            f"configured; a query embedded with a different model cannot match.\n"
            f"Run `agent-history reindex`, or set AGENT_HISTORY_MODEL={stored_model}."
        )
    return db


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
                db.execute("DELETE FROM meta WHERE key IN ('model', 'dimension')")
                db.commit()
            finally:
                db.close()


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

    if agent:
        rows = [r for r in rows if r[0] == agent]
    if cwd:
        rows = [r for r in rows if cwd in (r[3] or "")]
    return rows[:k]


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
