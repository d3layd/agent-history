"""reembed and the reindex orphan guard — model changes without data loss."""
from __future__ import annotations

import pytest

from agent_history import embed as embed_module
from agent_history import index, store
from conftest import msg

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


@pytest.fixture
def orphaned_index(transcripts, fake_embed):
    """An index whose only transcript has been deleted from disk."""
    path = transcripts("proj", "s", [
        msg("user", "the only surviving copy of this conversation is the index"),
        msg("assistant", "which is exactly why a wipe must be refused"),
    ])
    assert index.run() == 0
    path.unlink()
    return path


def test_reindex_refuses_when_orphans_exist(orphaned_index, capsys):
    assert index.run(reindex=True) == 1
    err = capsys.readouterr().err
    assert "refused" in err and "reembed" in err
    db = store.open_for_read()
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2


def test_reindex_force_still_wipes(orphaned_index):
    assert index.run(reindex=True, force=True) == 0
    db = store.open_for_read()
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_reindex_proceeds_when_nothing_is_orphaned(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a transcript that is still on disk")])
    index.run()
    assert index.run(reindex=True) == 0


def test_reembed_keeps_orphaned_chunks(orphaned_index, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "brand-new-model")
    assert index.run_reembed() == 0
    db = store.open_for_read()  # model check passes: meta now says new model
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    info = store.stats(db)
    assert info["model"] == "brand-new-model"
    assert "index now uses brand-new-model" in capsys.readouterr().out


def test_reembed_rebuilds_vectors_at_the_new_dimension(orphaned_index,
                                                       monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "four-dim-model")
    monkeypatch.setattr(embed_module, "probe_dimension", lambda: 4)
    monkeypatch.setattr(embed_module, "embed",
                        lambda text, timeout=120: [0.5, 0.25, 0.125, 0.0625])
    assert index.run_reembed() == 0
    db = store.open_for_read()
    ddl = db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'vec_chunks'"
    ).fetchone()[0]
    assert "float[4]" in ddl
    assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 2
    assert store.stats(db)["dimension"] == "4"


def test_reembed_search_works_after_the_swap(orphaned_index, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "brand-new-model")
    index.run_reembed()
    db = store.open_for_read()
    vector = embed_module.pack(embed_module.embed(
        "the only surviving copy of this conversation is the index"))
    rows = store.query(db, vector, 2)
    assert rows and "only surviving copy" in rows[0][6]


def test_reembed_resumes_after_an_embed_outage(orphaned_index, monkeypatch,
                                               capsys):
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "brand-new-model")
    calls = {"n": 0}
    real_embed = embed_module.embed

    def flaky(text, timeout=120):
        calls["n"] += 1
        if calls["n"] > 1:
            raise embed_module.EmbedError("ollama went away")
        return real_embed(text, timeout)

    monkeypatch.setattr(embed_module, "embed", flaky)
    assert index.run_reembed() == 1
    assert "resume" in capsys.readouterr().err

    monkeypatch.setattr(embed_module, "embed", real_embed)
    resumed = {"n": 0}

    def counting(text, timeout=120):
        resumed["n"] += 1
        return real_embed(text, timeout)

    monkeypatch.setattr(embed_module, "embed", counting)
    assert index.run_reembed() == 0
    assert resumed["n"] == 1, "the already-staged chunk must not re-embed"
    out = capsys.readouterr().out
    assert "resuming: 1 already staged" in out


def test_interrupted_staging_for_a_different_model_restarts(orphaned_index,
                                                            monkeypatch):
    db = store.open_for_maintenance()
    db.execute("CREATE VIRTUAL TABLE vec_chunks_new USING vec0(embedding float[8])")
    db.execute("INSERT INTO meta(key, value) VALUES ('reembed_model', 'stale-model')")
    db.commit()
    db.close()
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "brand-new-model")
    assert index.run_reembed() == 0
    db = store.open_for_read()
    assert store.stats(db)["model"] == "brand-new-model"
    assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 2


def test_reembed_without_an_index_fails_cleanly(fake_embed, capsys):
    with pytest.raises(store.StoreError):
        index.run_reembed()


def test_reembed_refuses_while_an_index_run_holds_the_lock(orphaned_index,
                                                           monkeypatch,
                                                           capsys):
    """A hook inserting old-dimension vectors mid-reembed would leave those
    chunks unsearchable after the swap — the shared lock prevents it."""
    from agent_history import config, trigger

    monkeypatch.setenv("AGENT_HISTORY_MODEL", "brand-new-model")
    with trigger._exclusive(config.lock_path()) as held:
        assert held
        assert index.run_reembed() == 1
    assert "lock" in capsys.readouterr().err
    # And with the lock free again, it proceeds.
    assert index.run_reembed() == 0