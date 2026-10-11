"""Redact secrets already stored in an index built before redaction existed.

Ingest redaction (``redact.py``) only protects chunks written from now on.
Orphaned chunks — whose transcripts the agent has since deleted — cannot be
re-derived from source, so the only way to clean them is to rewrite them in
place: new text, new content hash, FTS row swapped, vector re-embedded. Ids
and provenance are kept. A chunk whose redacted text already exists in the
index (the same transcript re-indexed after redaction was turned on) is a
duplicate and is deleted instead.

Preview is the default and needs no Ollama; ``--yes`` acts.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections import Counter

from . import config, embed, redact, store

COMMIT_EVERY = 16


def _targets(db: sqlite3.Connection) -> list[tuple[int, str, str, Counter[str]]]:
    out = []
    for rowid, text in db.execute("SELECT id, text FROM chunks WHERE text IS NOT NULL"):
        found = redact.spans(text)
        if not found:
            continue
        new = redact.apply(text, found)
        # Guard: an unchanged chunk would look like its own duplicate below
        # and be deleted.
        if new != text:
            out.append((rowid, text, new, Counter(cls for _, _, cls in found)))
    return out


def run(yes: bool = False) -> int:
    from . import trigger

    if not config.db_path().exists():
        print("no index yet — nothing to scrub")
        return 0
    # Same reasoning as reembed: a hook inserting mid-run must not race the
    # rewrite, so hooks no-op until this finishes and catch up afterwards.
    with trigger._exclusive(config.lock_path()) as acquired:
        if not acquired:
            print("another indexing run holds the lock; retry shortly",
                  file=sys.stderr)
            return 1
        return _run_locked(yes)


def _run_locked(yes: bool) -> int:
    db = store.open_for_maintenance()
    try:
        staged = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks_new'"
        ).fetchone()
        if staged:
            print("scrub refused: a reembed is in progress (vec_chunks_new "
                  "exists). Finish it first: agent-history reembed",
                  file=sys.stderr)
            return 1
        targets = _targets(db)
    finally:
        db.close()

    if not targets:
        print("scrub: no secrets found in stored chunks")
        return 0

    totals: Counter[str] = Counter()
    for *_, counts in targets:
        totals.update(counts)
    print(f"scrub: {len(targets)} chunk(s) contain secrets: "
          + ", ".join(f"{n} {cls}" for cls, n in totals.most_common()))
    if not yes:
        print("  preview only — nothing changed.\n"
              f"  rewrite them: agent-history scrub --yes   "
              f"(re-embeds up to {len(targets)} chunk(s) via Ollama)")
        return 0

    try:
        db, _dimension = store.open_for_write()  # model must match: we embed
    except store.StoreError as exc:
        print(f"scrub refused: {exc}", file=sys.stderr)
        return 1
    hashes = store.existing_hashes(db)
    rewritten = deduped = failures = 0
    try:
        for position, (rowid, old, new, _counts) in enumerate(targets, 1):
            digest = hashlib.sha256(new.encode("utf-8")).hexdigest()
            if digest in hashes:
                store.delete_chunk(db, rowid, old)
                deduped += 1
            else:
                try:
                    vector = embed.embed(new)
                except embed.EmbedError as exc:
                    failures += 1
                    print(f"  skipped chunk {rowid} (embed error): {exc}",
                          file=sys.stderr)
                    if failures > 20:
                        print("Aborting: too many embed errors; rerun to resume.",
                              file=sys.stderr)
                        break
                    continue
                store.rewrite_chunk(db, rowid, old_text=old, new_text=new,
                                    content_hash=digest, vector=vector)
                hashes.add(digest)
                rewritten += 1
            if position % COMMIT_EVERY == 0:
                db.commit()
        db.commit()
    finally:
        db.close()
    print(f"scrub: rewrote {rewritten}, removed {deduped} duplicate(s)"
          + (f", {failures} left for a rerun" if failures else ""))
    return 1 if failures else 0
