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


def test_text_mode_ignores_a_model_mismatch(db, monkeypatch):
    """Text search never embeds, so a different configured model is fine."""
    put(db, "text search should survive an embedding model change")
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "some-other-model")
    got = search.search_rows("model change", mode="text")
    assert len(got) == 1
    with pytest.raises(store.StoreError):
        search.search_rows("model change", mode="vector")


def test_search_rows_hybrid_falls_back_to_text_when_embed_fails(db, monkeypatch):
    put(db, "hybrid degrades to text search when the embedder is unreachable")

    def _boom(*a, **k):
        raise embed.EmbedError("connection refused")

    monkeypatch.setattr(embed, "embed", _boom)
    got = search.search_rows("embedder unreachable", mode="hybrid")
    assert len(got) == 1


def put_vec(db, text, vector, *, session="s1"):
    """Insert with a hand-picked vector, so vector ranks are under test control."""
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp="2026-01-01T00:00:00Z", role="user", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(), vector=vector)
    db.commit()


def test_text_query_matches_when_no_chunk_has_every_term(db):
    """A prose query must not need every word in one chunk (implicit AND)."""
    put(db, "the ux audit covered the frontend navigation")
    put(db, "backend listeners are passive until a webhook arrives")
    put(db, "the kitchen renovation budget spreadsheet")
    query = "UX audit — recommend implementations frontend backend passive"
    got = store.text_query(db, query, 5)
    assert {r[6] for r in got} == {
        "the ux audit covered the frontend navigation",
        "backend listeners are passive until a webhook arrives",
    }


def test_text_query_or_fallback_skips_stopwords(db):
    put(db, "the of in and a to")
    put(db, "the reconnect timer never fires")
    got = store.text_query(db, "the timer in the loop", 5)
    assert [r[6] for r in got] == ["the reconnect timer never fires"]


def test_text_query_ranks_all_term_matches_before_partial_ones(db):
    for text in ["os os os os error error error in the os layer",
                 "error error error everywhere",
                 "the os kernel"]:
        put(db, text)
    put(db, "the deploy failed with os error 11001 from the resolver")
    got = store.text_query(db, "os error 11001", 5)
    assert len(got) == 4
    assert "11001" in got[0][6]


def test_text_query_or_fill_does_not_duplicate_rows(db):
    put(db, "alpha beta gamma together")
    put(db, "alpha alone")
    got = store.text_query(db, "alpha beta gamma", 5)
    assert [r[6] for r in got] == ["alpha beta gamma together", "alpha alone"]


def test_hybrid_surfaces_a_chunk_ranked_below_2k_in_both_lists(db):
    """A chunk mid-table in both lists beats ones that top only one list.

    With k=2, the target sits at rank 5 in each list — past the old k*2 = 4
    candidate depth — so it was never fused. The deeper pool must reach it.
    """
    k = 2
    query_vec = [1.0] + [0.0] * 7
    # Vector-only distractors: nearest the query, but share no terms with it.
    for i in range(5):
        put_vec(db, f"unrelated note number {i}", [1.0, 0.01 * (i + 1)] + [0.0] * 6,
                session=f"v{i}")
    # Text-only distractors: dense in query terms, far away in vector space.
    for i in range(5):
        put_vec(db, f"zebra quorum zebra quorum {i}", [0.0] * 7 + [1.0],
                session=f"t{i}")
    target = ("a longer chunk that mentions zebra and quorum once each, among "
              "plenty of other words that dilute its full-text score")
    put_vec(db, target, [1.0, 0.06] + [0.0] * 6, session="target")
    # Filler between the target and the text distractors in vector space, so
    # the distractors fall outside the vector pool and stay single-list hits.
    for i in range(60):
        put_vec(db, f"filler chunk {i}", [1.0, 0.1 + 0.01 * i] + [0.0] * 6,
                session=f"f{i}")

    vector = embed.pack(query_vec)
    vec_rank = [r[6] for r in store.query(db, vector, 20)].index(target)
    txt_rank = [r[6] for r in store.text_query(db, "zebra quorum", 20)].index(target)
    assert vec_rank >= k * 2 and txt_rank >= k * 2  # the old pool missed it

    got = store.hybrid_query(db, vector, "zebra quorum", k)
    assert got[0][6] == target


def test_text_query_splits_identifiers_like_the_index(db):
    put(db, "the reconnect handler retries forever")
    got = store.text_query(db, "reconnect_handler timeout", 5)
    assert [r[6] for r in got] == ["the reconnect handler retries forever"]
