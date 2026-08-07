"""Storage schema, dedup, model identity, and end-to-end indexing."""
from __future__ import annotations

import pytest

from agent_history import config, index, store
from conftest import msg

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def test_schema_is_created_with_the_probed_dimension(fake_embed):
    db, dim = store.open_for_write()
    assert dim == 8
    ddl = db.execute(
        "select sql from sqlite_master where name='vec_chunks'"
    ).fetchone()[0]
    assert "float[8]" in ddl, "dimension must come from the probe, not a constant"


def test_model_and_dimension_are_recorded(fake_embed):
    store.open_for_write()
    db = store.open_for_read()
    info = store.stats(db)
    assert info["model"] == "nomic-embed-text"
    assert info["dimension"] == "8"


def test_index_is_created_owner_only(fake_embed):
    store.open_for_write()
    assert config.db_path().stat().st_mode & 0o777 == 0o600


def test_switching_model_is_refused_not_silently_mixed(fake_embed, monkeypatch):
    store.open_for_write()
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "some-other-model")
    with pytest.raises(store.StoreError) as e:
        store.open_for_write()
    assert "reindex" in str(e.value).lower()


def test_reading_with_a_different_model_is_refused(fake_embed, monkeypatch):
    store.open_for_write()
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "some-other-model")
    with pytest.raises(store.StoreError):
        store.open_for_read()


def test_search_before_indexing_gives_an_actionable_error():
    with pytest.raises(store.StoreError) as e:
        store.open_for_read()
    assert "agent-history index" in str(e.value)


def test_reset_removes_the_index(fake_embed):
    store.open_for_write()
    assert config.db_path().exists()
    store.reset()
    assert not config.db_path().exists()


# --- indexing -------------------------------------------------------------

def test_index_embeds_each_chunk_once(transcripts, fake_embed):
    transcripts("proj", "s", [
        msg("user", "the first distinct message in this transcript"),
        msg("assistant", "the second distinct message in this transcript"),
    ])
    assert index.run() == 0
    assert len(fake_embed) == 2


def test_reindexing_is_idempotent(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message that will be indexed twice")])
    index.run()
    first = len(fake_embed)
    index.run()
    assert len(fake_embed) == first, "second run must not re-embed anything"


def test_new_content_is_picked_up_incrementally(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "the original message in the file")])
    index.run()
    before = len(fake_embed)
    transcripts("proj", "s2", [msg("user", "a brand new message in a new file")])
    index.run()
    assert len(fake_embed) == before + 1


def test_identical_text_across_files_is_stored_once(transcripts, fake_embed):
    same = "this exact sentence appears in two different transcripts"
    transcripts("a", "s", [msg("user", same)])
    transcripts("b", "s", [msg("user", same)])
    index.run()
    db = store.open_for_read()
    assert store.stats(db)["total"] == 1, "content hash must dedup across files"


def test_index_reports_failure_when_nothing_is_available(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HISTORY_PROJECTS", str(tmp_path / "absent"))
    assert index.run() == 1


def test_reindex_rebuilds_from_scratch(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message present in the transcript")])
    index.run()
    before = len(fake_embed)
    index.run(reindex=True)
    assert len(fake_embed) > before, "reindex must re-embed, not skip via dedup"


def test_agent_column_is_populated(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message to check the agent column")])
    index.run()
    db = store.open_for_read()
    assert store.stats(db)["per_agent"] == {"claude-code": 1}


# --- search ---------------------------------------------------------------

def test_query_returns_nearest_and_respects_agent_filter(transcripts, fake_embed):
    transcripts("proj", "s", [
        msg("user", "the auth middleware split decision and why"),
        msg("assistant", "an unrelated note about css grid fallbacks"),
    ])
    index.run()
    db = store.open_for_read()
    from agent_history import embed as embed_module
    vec = embed_module.pack(embed_module.embed("anything"))

    assert len(store.query(db, vec, 5)) == 2
    assert len(store.query(db, vec, 5, agent="claude-code")) == 2
    assert store.query(db, vec, 5, agent="codex") == []


def test_cwd_filter_narrows_results(transcripts, fake_embed):
    transcripts("proj", "s", [
        msg("user", "a message from the first working directory", cwd="/repos/alpha"),
        msg("user", "a message from the second working directory", cwd="/repos/beta"),
    ])
    index.run()
    db = store.open_for_read()
    from agent_history import embed as embed_module
    vec = embed_module.pack(embed_module.embed("anything"))
    rows = store.query(db, vec, 5, cwd="alpha")
    assert len(rows) == 1
    assert "alpha" in rows[0][3]
