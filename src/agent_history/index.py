"""Incremental indexing.

Every chunk is keyed by the SHA-256 of its text, so re-running is cheap and
safe: already-embedded content is skipped without touching Ollama, and the same
mechanism works identically for file-backed and database-backed adapters.
"""
from __future__ import annotations

import hashlib
import sys
import time

from . import adapters, embed, store
from .textproc import chunk_text

COMMIT_EVERY = 16


def _pending(seen: set[str]) -> list[tuple[adapters.Record, str, str]]:
    """Collect (record, chunk_text, hash) for everything not yet indexed."""
    out: list[tuple[adapters.Record, str, str]] = []
    for adapter in adapters.available_adapters():
        count = 0
        for record in adapter.records():
            for piece in chunk_text(record.text):
                digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                out.append((record, piece, digest))
                count += 1
        print(f"  {adapter.name}: {count} new chunk(s)")
    return out


def run(reindex: bool = False) -> int:
    if reindex:
        print("reindex: removing existing index")
        store.reset()

    active = adapters.available_adapters()
    if not active:
        print("No agent transcripts found on this machine.", file=sys.stderr)
        print("Checked: ~/.claude/projects (override with $AGENT_HISTORY_PROJECTS)",
              file=sys.stderr)
        return 1
    print(f"Adapters: {', '.join(a.name for a in active)}")

    db, _dimension = store.open_for_write()
    seen = store.existing_hashes(db)
    print(f"Already indexed: {len(seen)} chunk(s)")

    pending = _pending(seen)
    if not pending:
        print("Up to date.")
        return 0

    print(f"Embedding {len(pending)} new chunk(s)...")
    started = time.time()
    failures = 0

    for position, (record, text, digest) in enumerate(pending, 1):
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
    return 0
