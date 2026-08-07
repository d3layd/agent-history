"""Chunking and scrubbing."""
from __future__ import annotations

from agent_history.textproc import (
    CHUNK_OVERLAP,
    MAX_CHUNK_CHARS,
    chunk_text,
    scrub,
)


def test_short_text_is_one_chunk():
    assert chunk_text("hello") == ["hello"]


def test_text_at_the_limit_is_not_split():
    text = "x" * MAX_CHUNK_CHARS
    assert chunk_text(text) == [text]


def test_one_char_over_the_limit_splits():
    chunks = chunk_text("x" * (MAX_CHUNK_CHARS + 1))
    assert len(chunks) == 2


def test_chunks_overlap_so_boundary_passages_stay_findable():
    # A distinctive marker straddling the boundary must survive intact in one
    # chunk, which is the whole point of the overlap.
    marker = "NEEDLEMARKER"
    text = "a" * (MAX_CHUNK_CHARS - len(marker) // 2) + marker
    text += "b" * MAX_CHUNK_CHARS
    chunks = chunk_text(text)
    assert any(marker in c for c in chunks), "marker was split across every chunk"


def test_overlap_is_the_configured_size():
    text = "".join(str(i % 10) for i in range(MAX_CHUNK_CHARS * 2))
    chunks = chunk_text(text)
    tail = chunks[0][-CHUNK_OVERLAP:]
    assert chunks[1].startswith(tail)


def test_chunks_reconstruct_the_original_ignoring_overlap():
    text = "".join(str(i % 10) for i in range(MAX_CHUNK_CHARS * 3 + 17))
    chunks = chunk_text(text)
    rebuilt = chunks[0]
    for c in chunks[1:]:
        rebuilt += c[CHUNK_OVERLAP:]
    assert rebuilt == text


def test_scrub_replaces_long_base64_runs():
    payload = "A1b2C3d4" * 50  # 400 chars, comfortably over the threshold
    assert "[blob]" in scrub(f"before {payload} after")


def test_scrub_leaves_ordinary_prose_alone():
    text = "The auth middleware was split so session refresh runs first."
    assert scrub(text) == text


def test_scrub_leaves_short_tokens_alone():
    # A hash-like string well under the 300-char threshold must survive; these
    # are meaningful in conversation (commit SHAs, short ids).
    text = "commit bbc2b8b1 fixed the escalation"
    assert scrub(text) == text
