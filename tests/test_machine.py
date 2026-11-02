"""Machine provenance: the column, its in-place migration, and the filters."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from agent_history import config, embed, index, search, store
from conftest import msg

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def test_machine_id_comes_from_the_env_then_the_hostname(monkeypatch):
    assert config.machine_id() == "testbox"
    monkeypatch.delenv("AGENT_HISTORY_MACHINE")
    monkeypatch.setattr("socket.gethostname", lambda: "Laptop.local")
    assert config.machine_id() == "Laptop"


def test_machine_id_is_filename_safe(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_MACHINE", "../my box/1")
    assert "/" not in config.machine_id() and " " not in config.machine_id()
    assert not config.machine_id().startswith(".")


def _legacy_db(path):
    """An index as 0.16 wrote it: no machine column."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, agent TEXT NOT NULL DEFAULT 'claude-code',
            origin TEXT, session_id TEXT, cwd TEXT, timestamp TEXT, role TEXT,
            text TEXT, content_hash TEXT UNIQUE);
        CREATE VIRTUAL TABLE fts_chunks
            USING fts5(text, content='chunks', content_rowid='id');
    """)
    db.execute("INSERT INTO chunks(agent, origin, session_id, cwd, timestamp, "
               "role, text, content_hash) VALUES "
               "('codex','/o','s1','/w','2026-01-01','user','old chunk text here','h1')")
    db.execute("INSERT INTO fts_chunks(rowid, text) VALUES (1, 'old chunk text here')")
    db.commit()
    db.close()


def test_migration_adds_the_column_without_losing_data(fake_embed):
    config.ensure_data_home()
    _legacy_db(config.db_path())
    # Make it a usable index so open_for_write accepts it.
    raw = sqlite3.connect(config.db_path())
    raw.execute("INSERT INTO meta VALUES ('model', 'nomic-embed-text')")
    raw.execute("INSERT INTO meta VALUES ('dimension', '8')")
    raw.commit()
    raw.close()

    db, _ = store.open_for_write()
    cols = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    assert "machine" in cols
    row = db.execute("SELECT agent, text, content_hash, machine FROM chunks").fetchone()
    assert row == ("codex", "old chunk text here", "h1", None)
    assert fake_embed == [], "a migration must never re-embed"


def test_readers_migrate_too_and_treat_old_rows_as_local(fake_embed):
    config.ensure_data_home()
    _legacy_db(config.db_path())
    db = store.open_for_read(check_model=False)
    assert "machine" in {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    rows = store.text_query(db, "old chunk", 5)
    assert rows and rows[0][7] is None
    results = [search._as_dict((*r, None, None)) for r in rows]
    assert results[0]["machine"] == "testbox"
    # ...and the filters see NULL as this machine.
    assert store.text_query(db, "old chunk", 5, machine="testbox")
    assert not store.text_query(db, "old chunk", 5, exclude_machine=["testbox"])


def test_migration_is_idempotent(fake_embed):
    store.open_for_write()
    store.open_for_write()
    db = store.open_for_read()
    assert [r[1] for r in db.execute("PRAGMA table_info(chunks)")].count("machine") == 1


def test_local_indexing_stores_the_local_machine(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    index.run()
    db = store.open_for_read()
    assert db.execute("SELECT machine FROM chunks").fetchone()[0] == "testbox"


def _put(db, text, machine):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{machine}.jsonl", session_id=machine,
        cwd="/w", timestamp="2026-01-01T00:00:00Z", role="user", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text), machine=machine)
    db.commit()


def test_machine_filters_on_every_query_path(fake_embed):
    db, _ = store.open_for_write()
    _put(db, "shared words about the deploy pipeline", "laptop")
    _put(db, "other words about the deploy pipeline", "server")
    vec = embed.pack(embed.embed("deploy pipeline"))
    for rows in (store.query(db, vec, 5, machine="server"),
                 store.text_query(db, "deploy pipeline", 5, machine="server"),
                 store.hybrid_query(db, vec, "deploy pipeline", 5, machine="server")):
        assert {r[7] for r in rows} == {"server"}
    rows = store.hybrid_query(db, vec, "deploy pipeline", 5,
                              exclude_machine=["server"])
    assert {r[7] for r in rows} == {"laptop"}


def test_search_shows_the_machine_only_when_it_is_not_local(fake_embed, capsys):
    db, _ = store.open_for_write()
    _put(db, "words about the deploy pipeline here", "laptop")
    _put(db, "mine: words about the deploy pipeline", "testbox")
    search.run("deploy pipeline", mode="text")
    out = capsys.readouterr().out
    assert "@laptop" in out
    assert "@testbox" not in out


def test_json_output_carries_the_machine(fake_embed, capsys):
    db, _ = store.open_for_write()
    _put(db, "words about the deploy pipeline here", "laptop")
    search.run("deploy pipeline", mode="text", as_json=True, machine="laptop")
    assert '"machine": "laptop"' in capsys.readouterr().out
