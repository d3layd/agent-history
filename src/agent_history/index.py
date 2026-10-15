"""Incremental indexing.

Every chunk is keyed by the SHA-256 of its text, so re-running is cheap and
safe: already-embedded content is skipped without touching Ollama, and the same
mechanism works identically for file-backed and database-backed adapters.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter

from . import adapters, config, embed, redact, sessions, store, textproc

COMMIT_EVERY = 16


def _guard_reindex(force: bool) -> int | None:
    """Refuse a wipe that would destroy history whose sources are gone.

    Agents delete old transcripts; for those chunks the index is the only
    surviving copy, and a rebuild from disk cannot recreate them. Model
    changes — the one legitimate reason to rebuild — have `reembed`, which
    keeps everything. Returns an exit code to stop with, or None to proceed.
    """
    if not config.db_path().exists():
        return None  # nothing exists yet; a "wipe" destroys nothing
    try:
        db = store.open_for_maintenance()
    except store.StoreError as exc:
        # Fail closed: an index this Python cannot open is one it cannot
        # rebuild either — deleting it here would be pure loss.
        print(f"reindex refused: the index exists but cannot be opened "
              f"({exc}).", file=sys.stderr)
        return 1
    try:
        chunks, origins = store.orphaned_counts(db)
    finally:
        db.close()
    if chunks == 0 or force:
        return None
    print(
        f"reindex refused: {chunks} chunk(s) from {origins} deleted "
        "source(s) exist only in this index and would be lost.\n"
        "  changing embedding model?  agent-history reembed   (keeps everything)\n"
        "  really discard them?       agent-history reindex --force",
        file=sys.stderr,
    )
    return 1


def redacted_chunks(text: str) -> list[str]:
    """Chunk ``text`` and redact secrets inside each chunk.

    Secrets are located on the whole text and chunk boundaries are computed
    on the original, so turning redaction on never moves a boundary: every
    chunk without a secret keeps the exact text (and hash) it had before.
    """
    return [piece for _start, _end, piece in redacted_pieces(text)]


def redacted_pieces(text: str) -> list[tuple[int, int, str]]:
    """``redacted_chunks`` with each chunk's (start, end) offsets into
    ``text`` — the provenance a chunk is stored with."""
    bounds = textproc.chunk_spans(text)
    if not config.redact_enabled():
        return [(start, end, text[start:end]) for start, end in bounds]
    found = redact.spans(text)
    return [(start, end, redact.apply(text[start:end], found, start))
            for start, end in bounds]


def record_id(record: adapters.Record) -> str:
    """A stable id for the record a chunk was cut from.

    Chunks of one record share it, which (with the ordinal and offsets) lets
    a later pass reassemble text that a chunk boundary split. Hashed over the
    redacted text when redaction is on, so the id derives from nothing the
    index does not hold.
    """
    text = record.text
    if config.redact_enabled():
        text = redact.apply(text, redact.spans(text), 0)
    payload = json.dumps([record.agent, record.origin, record.session_id,
                          record.timestamp, record.role, text],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _pending(
    seen: set[str],
    counts: Counter[str] | None = None,
    active: list | None = None,
    links: dict | None = None,
) -> tuple[list[tuple], set[str], list[tuple[str, str]]]:
    """Collect what needs indexing, and what mutable sources currently hold.

    Returns (new chunks to embed, every hash produced this run, the
    (agent, origin) pairs of mutable sources that were scanned). The latter
    two feed stale-chunk removal: only re-read mutable origins are eligible,
    and nothing produced this run may be deleted. Each new chunk is
    (record, text, hash, (record id, ordinal, start, end)).

    ``links``, when given, collects one record per session that carries
    aliases, so they are linked even when nothing in the session is new.
    """
    out: list[tuple] = []
    produced: set[str] = set()
    mutable_targets: set[tuple[str, str]] = set()
    for adapter in (adapters.available_adapters() if active is None else active):
        count = 0
        for record in adapter.records():
            # Never index this tool's own injected recall — re-indexing it
            # would compound into a memories-about-memories feedback loop.
            if textproc.is_injection(record.text):
                continue
            if links is not None and record.aliases:
                links.setdefault(_session_key(record), record)
            rid = None
            for ordinal, (start, end, piece) in enumerate(
                    redacted_pieces(record.text)):
                digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
                produced.add(digest)
                if digest in seen:
                    continue
                seen.add(digest)
                if counts is not None and "[REDACTED:" in piece:
                    counts.update(redact.markers(piece))
                rid = rid or record_id(record)
                out.append((record, piece, digest, (rid, ordinal, start, end)))
                count += 1
        if getattr(adapter, "mutable", False):
            # Eligibility comes from what was read, not what was yielded —
            # a doc shrunk below the indexing minimum yields no records but
            # must still have its stale chunks refreshed.
            mutable_targets.update(
                (adapter.name, origin)
                for origin in getattr(adapter, "scanned", ())
            )
        print(f"  {adapter.name}: {count} new chunk(s)")
    return out, produced, sorted(mutable_targets)


def _session_key(record: adapters.Record) -> tuple:
    return (record.agent, record.machine, record.session_id, record.origin,
            record.session_uid)


class _Uids:
    """Each record's session uid, worked out once per session per run."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.cache: dict[tuple, str] = {}

    def __call__(self, record: adapters.Record) -> str:
        key = _session_key(record)
        if key not in self.cache:
            self.cache[key] = sessions.uid_for(
                self.db, record.agent, record.machine, record.session_id,
                record.origin, claimed=record.session_uid)
        return self.cache[key]


def _link_sessions(db: sqlite3.Connection, links: dict, uids: _Uids) -> None:
    """Apply the aliases adapters found. Re-applying a link is a no-op, so
    this runs on every pass over sources that carry them."""
    for record in links.values():
        uid = uids(record)
        sessions.register_session(
            db, uid, agent=record.agent,
            machine=record.machine or config.machine_id(),
            session_id=record.session_id, origin=record.origin,
            timestamp=record.timestamp, cwd=record.cwd)
        target = uid
        for kind, value in record.aliases:
            if kind in ("cse", "bridge"):
                target = sessions.link_bridge(db, target, value)
            else:
                target = sessions.link_alias(db, target, kind, value)
    db.commit()


def _release_consumed(active: list) -> None:
    """Let queue-like adapters (the hub inbox) retire what this run read.

    Called only once everything read has been committed and nothing was
    skipped, so a crash or an embed failure just means the same batches are
    read again — content-hash dedup makes that free.
    """
    for adapter in active:
        release = getattr(adapter, "release", None)
        if callable(release):
            release()


def run(reindex: bool = False, force: bool = False) -> int:
    if reindex:
        code = _guard_reindex(force)
        if code is not None:
            return code
        print("reindex: removing existing index")
        store.reset()

    active = adapters.available_adapters()
    if not active:
        if config.hub_mode():
            print("hub mode: no inbox to index. Set AGENT_HISTORY_INBOX to an "
                  "existing directory.", file=sys.stderr)
            return 1
        print("No agent transcripts found on this machine.", file=sys.stderr)
        print("Checked: ~/.claude/projects, ~/.codex/sessions, "
              "~/.local/share/opencode, ~/.hermes*/state.db "
              "(override with $AGENT_HISTORY_PROJECTS and friends)",
              file=sys.stderr)
        return 1
    print(f"Adapters: {', '.join(a.name for a in active)}")

    db, _dimension = store.open_for_write()
    seen = store.existing_hashes(db)
    print(f"Already indexed: {len(seen)} chunk(s)")

    counts: Counter[str] = Counter()
    links: dict = {}
    pending, produced, mutable_targets = _pending(seen, counts, active, links)
    uids = _Uids(db)
    if counts:
        print("redacted in new chunks: " + ", ".join(f"{n} {cls}" for cls, n in counts.most_common()))
    store.set_meta(db, "redact_last", json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "enabled": config.redact_enabled(),
        "counts": dict(counts),
    }))
    db.commit()

    # Mutable sources (docs) are refreshed before the early return: an edit
    # that only removes text produces no new chunks but still leaves stale
    # ones to clear.
    stale = store.remove_stale(db, mutable_targets, keep=produced)
    if stale:
        print(f"docs refresh: removed {stale} stale chunk(s)")
    if links:
        _link_sessions(db, links, uids)

    if not pending:
        print("Up to date.")
        _release_consumed(active)
        return 0

    print(f"Embedding {len(pending)} new chunk(s)...")
    started = time.time()
    failures = 0

    for position, (record, text, digest, where) in enumerate(pending, 1):
        try:
            vector = embed.embed(text)
        except embed.EmbedError as exc:
            failures += 1
            # A handful of transient errors is survivable; a wall of them is not.
            if failures > 20:
                db.commit()
                print(f"\nAborting: {exc}", file=sys.stderr)
                return 1
            print(f"  skipped (embed error): {exc}", file=sys.stderr)
            continue

        store.insert_chunk(
            db,
            agent=record.agent,
            origin=record.origin,
            session_id=record.session_id,
            cwd=record.cwd,
            timestamp=record.timestamp,
            role=record.role,
            text=text,
            content_hash=digest,
            vector=vector,
            machine=record.machine,
            session_uid=uids(record),
            record_id=where[0],
            chunk_ordinal=where[1],
            char_start=where[2],
            char_end=where[3],
        )

        if position % COMMIT_EVERY == 0:
            db.commit()
            elapsed = max(time.time() - started, 0.001)
            rate = position / elapsed
            eta = (len(pending) - position) / max(rate, 0.001)
            print(f"  {position}/{len(pending)}  ({rate:.1f}/s, eta {eta:.0f}s)")

    db.commit()
    print(f"Done in {time.time() - started:.1f}s.")
    if failures:
        print(f"({failures} chunk(s) skipped due to embed errors)", file=sys.stderr)
    else:
        _release_consumed(active)
    return 0


# ------------------------------------------------------------------- reembed

def run_reembed() -> int:
    """Re-embed every stored chunk with the configured model, losing nothing.

    The chunk text lives in the index, so a model change never needs the
    source transcripts — which is what makes this safe where `reindex` is
    not. New vectors are staged into `vec_chunks_new` and swapped in only
    when complete, so searches keep working against the old vectors
    throughout, a crash or an Ollama outage leaves a resumable staging
    table, and the index is never in a half-embedded state.
    """
    from . import trigger

    # Hold the indexing lock for the whole run: a hook firing mid-reembed
    # would insert old-dimension vectors that the swap then discards,
    # leaving those chunks permanently unsearchable. With the lock held,
    # hooks no-op (their 5-second timeout) and catch up afterwards.
    with trigger._exclusive(config.lock_path()) as acquired:
        if not acquired:
            print("another indexing run holds the lock; retry shortly",
                  file=sys.stderr)
            return 1
        return _reembed_locked()


def _reembed_locked() -> int:
    db = store.open_for_maintenance()
    total = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    if total == 0:
        print("index is empty; nothing to re-embed")
        return 0

    model = config.model()
    dimension = embed.probe_dimension()
    print(f"re-embedding {total} chunk(s) with {model} ({dimension}-dim)")

    # Stage table, resumable: a prior interrupted run for the same model is
    # continued; one for a different model is discarded. The table probe
    # also heals a staging table that vanished out from under the marker.
    staged_for = db.execute(
        "SELECT value FROM meta WHERE key = 'reembed_model'"
    ).fetchone()
    try:
        done = {row[0] for row in db.execute("SELECT rowid FROM vec_chunks_new")}
    except sqlite3.OperationalError:
        done = set()
        staged_for = None
    if staged_for is None or staged_for[0] != model:
        db.execute("DROP TABLE IF EXISTS vec_chunks_new")
        db.execute(
            f"CREATE VIRTUAL TABLE vec_chunks_new "
            f"USING vec0(embedding float[{dimension}])"
        )
        db.execute(
            "INSERT INTO meta(key, value) VALUES ('reembed_model', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (model,),
        )
        db.commit()
        done = set()
    if done:
        print(f"resuming: {len(done)} already staged")

    # Materialised id list: no read cursor stays open across the writes and
    # commits below, and a resumed run's rate reflects real work only.
    todo = [row[0] for row in
            db.execute("SELECT id FROM chunks ORDER BY id").fetchall()
            if row[0] not in done]

    started = time.time()
    for position, rowid in enumerate(todo, 1):
        text = db.execute(
            "SELECT text FROM chunks WHERE id = ?", (rowid,)
        ).fetchone()[0]
        try:
            vector = embed.embed(text)
        except embed.EmbedError as exc:
            db.commit()
            print(f"\nstopped: {exc}\n"
                  "progress is saved — rerun `agent-history reembed` to resume",
                  file=sys.stderr)
            return 1
        db.execute(
            "INSERT INTO vec_chunks_new(rowid, embedding) VALUES (?, ?)",
            (rowid, embed.pack(vector)),
        )
        if position % COMMIT_EVERY == 0:
            db.commit()
            elapsed = max(time.time() - started, 0.001)
            rate = position / elapsed
            eta = (len(todo) - position) / max(rate, 0.001)
            print(f"  {position}/{len(todo)}  ({rate:.1f}/s, eta {eta:.0f}s)")
    db.commit()

    # Atomic swap: the old vectors serve searches right up to this point.
    db.execute("DROP TABLE vec_chunks")
    db.execute(
        f"CREATE VIRTUAL TABLE vec_chunks "
        f"USING vec0(embedding float[{dimension}])"
    )
    db.execute(
        "INSERT INTO vec_chunks(rowid, embedding) "
        "SELECT rowid, embedding FROM vec_chunks_new"
    )
    db.execute("DROP TABLE vec_chunks_new")
    for key, value in (("model", model), ("dimension", str(dimension))):
        db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.execute("DELETE FROM meta WHERE key = 'reembed_model'")
    db.commit()
    print(f"Done in {time.time() - started:.1f}s — index now uses {model}.")
    return 0
