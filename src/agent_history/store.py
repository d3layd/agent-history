"""SQLite + sqlite-vec storage layer."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable

import sqlite_vec

from . import config, embed, sessions
# Session identity helpers, re-exported: callers holding a store connection
# reach them here.
from .sessions import link_alias, resolve_session  # noqa: F401


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


def _migrate(db: sqlite3.Connection) -> None:
    """Bring an index written by an older version up to the current schema.

    Additive only: columns are added in place, nothing is rewritten and no
    chunk is re-embedded or deleted. `machine` (0.17) is NULL on rows indexed
    before it existed, which readers treat as "this machine" — those chunks
    were all indexed locally. `session_uid` and the `sessions` /
    `session_aliases` tables (session identity) are backfilled once, in one
    pass over the rows still lacking a uid. The chunk provenance columns
    (`record_id`, `chunk_ordinal`, `char_start`, `char_end`) stay NULL on old
    rows: the record they were cut from is not recoverable from a chunk.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
    for name, kind in _ADDED_COLUMNS:
        if name not in columns:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {name} {kind}")
    existing = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    # DDL only when something is missing, so a reader of a current index
    # never needs the write lock.
    if not {"idx_chunks_machine", "idx_chunks_session_uid", "sessions",
            "session_aliases", "idx_aliases_value",
            "idx_aliases_uid"} <= existing:
        db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_machine ON chunks(machine)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_session_uid "
                   "ON chunks(session_uid)")
        db.executescript(sessions.SCHEMA)
    sessions.backfill(db)
    db.commit()


# (name, type) of every column added to `chunks` after its first release.
_ADDED_COLUMNS = (
    ("machine", "TEXT"),
    ("session_uid", "TEXT"),
    ("record_id", "TEXT"),
    ("chunk_ordinal", "INTEGER"),
    ("char_start", "INTEGER"),
    ("char_end", "INTEGER"),
)


def _get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(db: sqlite3.Connection, key: str) -> str | None:
    return _get_meta(db, key)


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    _set_meta(db, key, value)


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
            content_hash TEXT UNIQUE,
            machine      TEXT,
            session_uid  TEXT,
            -- provenance: which record a chunk was cut from, and where
            record_id     TEXT,
            chunk_ordinal INTEGER,
            char_start    INTEGER,
            char_end      INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_origin ON chunks(origin);
        CREATE INDEX IF NOT EXISTS idx_chunks_agent  ON chunks(agent);
        """
    )

    _migrate(db)
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
    _migrate_for_reader(db)
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
    db = _raw_connect(path)
    _migrate_for_reader(db)
    return db


def _migrate_for_reader(db: sqlite3.Connection) -> None:
    """Upgrade the schema from a path that is not the indexer.

    The upgrade is a few metadata-only ALTERs plus a one-pass uid backfill
    (seconds even on a large index), so a search or purge run first after an
    update just does it. If the index is read-only or locked for
    longer than SQLite's wait, say so rather than failing on a missing column
    later.
    """
    try:
        _migrate(db)
    except sqlite3.OperationalError as exc:
        db.close()
        raise StoreError(
            f"the index needs a one-time schema upgrade but could not be "
            f"written ({exc}). Run: agent-history index"
        ) from exc


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
                db.execute("DROP TABLE IF EXISTS sessions")
                db.execute("DROP TABLE IF EXISTS session_aliases")
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
    machine: str | None = None,
    session_uid: str | None = None,
    record_id: str | None = None,
    chunk_ordinal: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
) -> None:
    """Store one embedded chunk and register its session.

    `session_uid` is derived when not given (see sessions.derive_uid). The
    provenance fields say which record the chunk was cut from and where:
    `char_start`/`char_end` are offsets into that record's text.
    """
    machine = machine or config.machine_id()
    uid = session_uid or sessions.derive_uid(agent, machine, session_id, origin)
    cursor = db.execute(
        "INSERT INTO chunks "
        "(agent, origin, session_id, cwd, timestamp, role, text, content_hash, "
        "machine, session_uid, record_id, chunk_ordinal, char_start, char_end) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent, origin, session_id, cwd, timestamp, role, text, content_hash,
         machine, uid, record_id, chunk_ordinal, char_start, char_end),
    )
    sessions.register_session(db, uid, agent=agent, machine=machine,
                              session_id=session_id, origin=origin,
                              timestamp=timestamp, cwd=cwd)
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
    machine: str | None = None,
    exclude_machine: Iterable[str] | None = None,
) -> list[tuple]:
    """Nearest neighbours, optionally narrowed by cwd substring, agent or
    machine. Rows are (*_ROW_COLS, distance).

    Filters are applied after the vector search, so we over-fetch when one is
    present to avoid returning fewer than k results.
    """
    fetch = _fetch(k, cwd, agent, machine, exclude_machine)
    rows = db.execute(
        f"SELECT {_ROW_COLS}, v.distance "
        "FROM vec_chunks v JOIN chunks c ON c.id = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        (vector, fetch),
    ).fetchall()

    return _filter(rows, cwd=cwd, agent=agent, machine=machine,
                   exclude_machine=exclude_machine)[:k]


# The last column is the chunk's session uid, already canonicalised: a uid
# merged into another one reports the survivor.
_ROW_COLS = ("c.agent, c.origin, c.session_id, c.cwd, c.timestamp, c.role, "
             "c.text, c.machine, "
             "coalesce((SELECT a.session_uid FROM session_aliases a "
             "WHERE a.kind = 'uid' AND a.value = c.session_uid), c.session_uid)")
ROW_WIDTH = 9  # columns in _ROW_COLS; a ranking value, if any, follows
_VEC_K_MAX = 4096  # sqlite-vec's cap on k


def _fetch(k: int, cwd, agent, machine, exclude_machine) -> int:
    """How many candidates to pull before the post-filters trim them.

    A machine filter can discard most of the nearest neighbours (a hub's
    biggest contributor dominates), so it over-fetches harder than the others.
    """
    if machine or exclude_machine:
        return min(k * 16, _VEC_K_MAX)
    return min(k * 4 if (cwd or agent) else k, _VEC_K_MAX)


# Skipped by the OR fallback only, so "the/a/in" can't pull in every chunk.
# The strict AND match still uses every term.
_STOPWORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have "
    "how i if in into is it its me my no not of on or our so than that the "
    "their them then there these they this to too us was we were what when "
    "where which who why will with would you your".split()
)


# Mirrors FTS5's unicode61 tokenizer: runs of letters and digits, split on
# punctuation and underscores, so `foo_bar` is two terms like it is in the index.
_TOKEN = re.compile(r"[^\W_]+")


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def text_query(
    db: sqlite3.Connection,
    query: str,
    k: int,
    *,
    cwd: str | None = None,
    agent: str | None = None,
    machine: str | None = None,
    exclude_machine: Iterable[str] | None = None,
) -> list[tuple]:
    """BM25-ranked full-text matches, same row shape as query() minus distance.

    Chunks containing every term come first. If there are fewer than k of
    those, the rest are filled from chunks containing any non-stopword term,
    also BM25-ranked — otherwise a natural-language query, which rarely has
    every word in one chunk, would match nothing at all.

    Each term is quoted so FTS5 operator syntax in ordinary prose ("don't",
    "a-b", stray quotes) cannot break the MATCH expression. Terms are split
    the way the index tokenizes text, so punctuation-only tokens ("—", "->")
    disappear and `foo_bar` matches chunks holding just `foo` in the fill.
    """
    if not fts_enabled(db):
        return []
    terms = _TOKEN.findall(query)
    if not terms:
        return []
    fetch = _fetch(k, cwd, agent, machine, exclude_machine)
    narrowing = {"cwd": cwd, "agent": agent, "machine": machine,
                 "exclude_machine": exclude_machine}

    hits = _match(db, " ".join(map(_quote, terms)), fetch, **narrowing)
    if len(hits) < k:
        loose = [t for t in terms if t.lower() not in _STOPWORDS] or terms
        if len(loose) > 1 or loose != terms:
            # AND matches are a subset of these, so look past them.
            more = _match(db, " OR ".join(map(_quote, loose)), fetch + len(hits),
                          **narrowing)
            for rowid, row in more.items():
                hits.setdefault(rowid, row)
    return list(hits.values())[:k]


def _match(db: sqlite3.Connection, match: str, limit: int, *,
           cwd: str | None, agent: str | None, machine: str | None,
           exclude_machine: Iterable[str] | None) -> dict[int, tuple]:
    """Filtered FTS rows keyed by chunk id, best BM25 first."""
    try:
        rows = db.execute(
            f"SELECT c.id, {_ROW_COLS} "
            "FROM fts_chunks f JOIN chunks c ON c.id = f.rowid "
            "WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row[0]: row[1:] for row in rows
            if _filter([row[1:]], cwd=cwd, agent=agent, machine=machine,
                       exclude_machine=exclude_machine)}


def hybrid_query(
    db: sqlite3.Connection,
    vector: bytes,
    query_text: str,
    k: int,
    *,
    cwd: str | None = None,
    agent: str | None = None,
    machine: str | None = None,
    exclude_machine: Iterable[str] | None = None,
) -> list[tuple]:
    """Merge vector and full-text rankings with reciprocal rank fusion.

    Each list contributes 1/(60+rank) per hit; a chunk found by both ranks
    above one found by either alone. Returns rows shaped like query() with an
    RRF score appended: (..., distance | None, score).

    Both lists are pulled much deeper than k: a chunk that is only middling
    in each list (say rank 30 in both) should beat one that tops a single
    list, and it can only do that if both lists reach it.
    """
    pool = max(k * 10, 50)
    narrowing = {"cwd": cwd, "agent": agent, "machine": machine,
                 "exclude_machine": exclude_machine}
    vec_rows = query(db, vector, pool, **narrowing)
    txt_rows = text_query(db, query_text, pool, **narrowing)

    scored: dict[tuple, list] = {}
    for rank, row in enumerate(vec_rows):
        key = row[:ROW_WIDTH]
        scored[key] = [row[ROW_WIDTH], 1.0 / (60 + rank)]
    for rank, row in enumerate(txt_rows):
        key = row[:ROW_WIDTH]
        if key in scored:
            scored[key][1] += 1.0 / (60 + rank)
        else:
            scored[key] = [None, 1.0 / (60 + rank)]

    merged = sorted(scored.items(), key=lambda item: -item[1][1])
    return [(*key, distance, score) for key, (distance, score) in merged[:k]]


def _filter(rows: list[tuple], *, cwd: str | None, agent: str | None,
            machine: str | None = None,
            exclude_machine: Iterable[str] | None = None) -> list[tuple]:
    if agent:
        rows = [r for r in rows if r[0] == agent]
    if cwd:
        rows = [r for r in rows if cwd in (r[3] or "")]
    # A NULL machine is a chunk indexed here before provenance existed.
    local = config.machine_id()
    if machine:
        rows = [r for r in rows if (r[7] or local) == machine]
    if exclude_machine:
        skip = set(exclude_machine)
        rows = [r for r in rows if (r[7] or local) not in skip]
    return rows


def session_rows(
    db: sqlite3.Connection,
    *,
    origin: str | None = None,
    session_id: str | None = None,
    session_uid: str | None = None,
) -> list[tuple]:
    """Every chunk of one conversation, oldest first.

    `session_uid` should be canonical (see resolve_session): chunks written
    under any uid merged into it are included. This reads the index, not the
    source transcript — deliberately, since for a mature index most source
    files no longer exist.
    """
    where, params = [], []
    if origin:
        where.append("c.origin = ?")
        params.append(origin)
    if session_id:
        where.append("c.session_id = ?")
        params.append(session_id)
    if session_uid:
        group = sessions.members(db, session_uid)
        where.append(f"c.session_uid IN ({','.join('?' * len(group))})")
        params.extend(group)
    if not where:
        return []
    return db.execute(
        f"SELECT {_ROW_COLS} FROM chunks c WHERE {' OR '.join(where)} "
        "ORDER BY c.timestamp, c.id",
        params,
    ).fetchall()


def rewrite_chunk(
    db: sqlite3.Connection,
    rowid: int,
    *,
    old_text: str,
    new_text: str,
    content_hash: str,
    vector: list[float],
) -> None:
    """Replace a chunk's text in place, keeping its id and provenance."""
    fts = fts_enabled(db)
    if fts:
        db.execute(
            "INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', ?, ?)",
            (rowid, old_text),
        )
    db.execute(
        "UPDATE chunks SET text = ?, content_hash = ? WHERE id = ?",
        (new_text, content_hash, rowid),
    )
    db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
    db.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
        (rowid, embed.pack(vector)),
    )
    if fts:
        db.execute(
            "INSERT INTO fts_chunks(rowid, text) VALUES (?, ?)", (rowid, new_text)
        )


def delete_chunk(db: sqlite3.Connection, rowid: int, text: str) -> None:
    _delete_rows(db, [(rowid, text)])


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
        # Any id of the session works (uid, raw id, alias); the raw-id match
        # is kept so an id this index cannot resolve purges as it always did.
        uid = resolve_session(db, session)
        group = sessions.members(db, uid) if uid else []
        clauses.append("(session_id = ?"
                       + "".join(" OR session_uid = ?" for _ in group) + ")")
        params.extend([session, *group])
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
    local = config.machine_id()
    per_machine: dict[str, int] = {}
    for name, count in db.execute(
            "SELECT machine, count(*) FROM chunks GROUP BY machine ORDER BY 2 DESC"):
        per_machine[name or local] = per_machine.get(name or local, 0) + count
    return {
        "total": total,
        "per_agent": per_agent,
        "per_machine": per_machine,
        "model": _get_meta(db, "model"),
        "dimension": _get_meta(db, "dimension"),
    }
