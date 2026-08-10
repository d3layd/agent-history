"""Refresh-on-change for docs — the store's only delete path.

The invariants under test: an edited doc's vanished text is removed from
chunks, vectors and FTS alike; a hash produced by anything this run
survives; transcript chunks are untouchable; a doc deleted from disk keeps
its chunks.
"""
from __future__ import annotations

import pytest

from agent_history import index, store
from conftest import msg

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


@pytest.fixture
def docs_root(isolated_env, monkeypatch):
    root = isolated_env / "docsroot"
    root.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_DOCS_DIRS", str(root))
    return root


def counts():
    db = store.open_for_read()
    chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    vecs = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
    return chunks, vecs


def texts():
    db = store.open_for_read()
    return [r[0] for r in db.execute("SELECT text FROM chunks ORDER BY id")]


def test_edited_doc_replaces_its_chunks(docs_root, fake_embed):
    doc = docs_root / "runbook.md"
    doc.write_text("Restart the gateway with the old procedure, step one.")
    assert index.run() == 0
    assert len(texts()) == 1

    doc.write_text("Restart the gateway with the NEW procedure entirely.")
    assert index.run() == 0
    remaining = texts()
    assert len(remaining) == 1
    assert "NEW procedure" in remaining[0]
    assert "old procedure" not in " ".join(remaining)
    chunks, vecs = counts()
    assert chunks == vecs, "vector rows must be deleted with their chunks"


def test_fts_no_longer_matches_removed_text(docs_root, fake_embed):
    doc = docs_root / "note.md"
    doc.write_text("The ephemeral ingest hostname must be repasted every time.")
    index.run()
    doc.write_text("A fully rewritten note with different content now.")
    index.run()
    db = store.open_for_read()
    assert store.text_query(db, "ephemeral ingest hostname", 5) == []
    assert len(store.text_query(db, "fully rewritten", 5)) == 1


def test_pure_deletion_is_refreshed_despite_no_new_chunks(docs_root, fake_embed):
    keep = docs_root / "keep.md"
    keep.write_text("A document that stays exactly the same across runs.")
    trim = docs_root / "trim.md"
    trim.write_text("A document about to lose this entire paragraph of text.")
    index.run()
    assert len(texts()) == 2

    trim.write_text("A document that stays exactly the same across runs.")
    # trim.md now duplicates keep.md: no NEW chunks anywhere, but its old
    # text must still be cleared on this run.
    assert index.run() == 0
    assert "lose this entire paragraph" not in " ".join(texts())


def test_transcript_chunks_are_never_deleted(docs_root, transcripts, fake_embed):
    transcripts("proj", "s", [
        msg("user", "a transcript message that must survive every docs edit"),
    ])
    doc = docs_root / "a.md"
    doc.write_text("Initial documentation content, soon to be rewritten.")
    index.run()
    doc.write_text("Rewritten documentation content, nothing like before.")
    index.run()
    assert any("must survive" in t for t in texts())


def test_hash_shared_with_a_transcript_survives_the_doc_edit(
        docs_root, transcripts, fake_embed):
    shared = "identical text living in both a transcript and a document"
    transcripts("proj", "s", [msg("user", shared)])
    doc = docs_root / "copy.md"
    doc.write_text(shared)
    index.run()
    doc.write_text("The document moved on to entirely different content.")
    index.run()
    assert any(shared in t for t in texts()), \
        "text still produced by the transcript must not be deleted"


def test_doc_shrunk_below_minimum_is_still_refreshed(docs_root, fake_embed):
    """A stubbed-out doc yields no records but was read — its old text must
    go. This is the case record-derived eligibility gets wrong."""
    doc = docs_root / "runbook.md"
    doc.write_text("A full procedure that is about to be stubbed out entirely.")
    index.run()
    doc.write_text("todo")
    assert index.run() == 0
    assert "about to be stubbed" not in " ".join(texts())


def test_doc_grown_past_the_size_cap_keeps_its_chunks(docs_root, fake_embed):
    """An oversized file is never read, so its content cannot be judged —
    like an unreadable file, its existing chunks stay."""
    from agent_history.adapters.docs import MAX_FILE_BYTES

    doc = docs_root / "log.md"
    doc.write_text("Original content indexed before the file blew up.")
    index.run()
    doc.write_text("x" * (MAX_FILE_BYTES + 1))
    index.run()
    assert any("before the file blew up" in t for t in texts())


def test_doc_deleted_from_disk_keeps_its_chunks(docs_root, fake_embed):
    doc = docs_root / "gone.md"
    doc.write_text("Content whose source file is about to disappear.")
    index.run()
    doc.unlink()
    index.run()
    assert any("about to disappear" in t for t in texts())


def test_unchanged_docs_cost_nothing(docs_root, fake_embed):
    (docs_root / "stable.md").write_text(
        "Stable content that should be embedded exactly once.")
    index.run()
    embeds_after_first = len(fake_embed)
    index.run()
    assert len(fake_embed) == embeds_after_first, "no re-embedding, no churn"
