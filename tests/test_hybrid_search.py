"""Hybrid retrieval — FTS availability, fusion, fallbacks, and migration."""
from __future__ import annotations

import hashlib

import pytest

from agent_history import embed, search, store

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def put(db, text, *, agent="claude-code", cwd="/w", session="s1",
        origin="/t/s1.jsonl", ts="2026-01-01T00:00:00Z"):
    vector = embed.embed(text)
    store.insert_chunk(
        db, agent=agent, origin=origin, session_id=session, cwd=cwd,
        timestamp=ts, role="user", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(), vector=vector)
    db.commit()


@pytest.fixture
def db(fake_embed):
    db, _dim = store.open_for_write()
    return db


def test_fts_table_is_created_and_filled(db):
    put(db, "the reconnect timer never fires under fake timers")
    assert store.fts_enabled(db)
    got = store.text_query(db, "reconnect timer", 5)
    assert len(got) == 1


def test_text_query_finds_exact_identifiers(db):
    put(db, "the deploy failed with os error 11001 from the resolver")
    put(db, "we chose sqlite-vec over a separate vector service")
    got = store.text_query(db, "11001", 5)
    assert len(got) == 1
    assert "11001" in got[0][6]


def test_text_query_survives_fts_operator_syntax(db):
    put(db, "we don't index tool output (it's mostly noise) - by design")
    for query in ['don\'t "index" (it\'s', "NOT AND OR", 'a"b']:
        store.text_query(db, query, 5)  # must not raise


def test_hybrid_ranks_double_hits_first(db):
    put(db, "authentication middleware split because session refresh runs first")
    put(db, "the kitchen renovation budget spreadsheet needs new numbers")
    vector = embed.pack(embed.embed(
        "authentication middleware split because session refresh runs first"))
    got = store.hybrid_query(
        db, vector, "authentication middleware session refresh", 5)
    assert got[0][6].startswith("authentication middleware")
    assert got[0][8] > got[-1][8]  # RRF score orders the list


def test_hybrid_respects_agent_and_cwd_filters(db):
    put(db, "a codex-only note about the resolver failure", agent="codex",
        cwd="/codex-proj")
    put(db, "a claude note about the resolver failure", agent="claude-code",
        cwd="/claude-proj")
    vector = embed.pack(embed.embed("resolver failure"))
    got = store.hybrid_query(db, vector, "resolver failure", 5, agent="codex")
    assert {r[0] for r in got} == {"codex"}
    got = store.hybrid_query(db, vector, "resolver failure", 5,
                             cwd="claude-proj")
    assert {r[3] for r in got} == {"/claude-proj"}


def test_pre_fts_index_is_backfilled_on_open(db):
    put(db, "a chunk written while the fts table exists")
    db.execute("DROP TABLE fts_chunks")
    db.commit()
    db.close()
    db2, _ = store.open_for_write()
    assert store.fts_enabled(db2)
    assert len(store.text_query(db2, "chunk written", 5)) == 1


def test_search_rows_text_mode_needs_no_embedding(db, monkeypatch):
    put(db, "full text still works when ollama is completely down")

    def _boom(*a, **k):
        raise embed.EmbedError("ollama is down")

    monkeypatch.setattr(embed, "embed", _boom)
    got = search.search_rows("ollama down", mode="text")
    assert len(got) == 1
    assert got[0]["distance"] is None


def test_search_rows_hybrid_falls_back_to_text_when_embed_fails(db, monkeypatch):
    put(db, "hybrid degrades to text search when the embedder is unreachable")

    def _boom(*a, **k):
        raise embed.EmbedError("connection refused")

    monkeypatch.setattr(embed, "embed", _boom)
    got = search.search_rows("embedder unreachable", mode="hybrid")
    assert len(got) == 1
