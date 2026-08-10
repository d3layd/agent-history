"""purge — surgical and total deletion, with vec/fts parity."""
from __future__ import annotations

import hashlib

import pytest

from agent_history import embed, store

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def put(db, text, *, agent="claude-code", cwd="/proj", session="s1"):
    store.insert_chunk(
        db, agent=agent, origin=f"/t/{session}.jsonl", session_id=session,
        cwd=cwd, timestamp="2026-01-01T00:00:00Z", role="user", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def populated(fake_embed):
    db, _ = store.open_for_write()
    put(db, "a cui client conversation about the contract", cwd="/clients/cui", session="cui-1")
    put(db, "another cui project note with sensitive detail", cwd="/clients/cui", session="cui-2")
    put(db, "a commercial client marketing chat", cwd="/clients/acme", session="acme-1")
    put(db, "a codex session on the cui project too", agent="codex", cwd="/clients/cui", session="cx-1")
    return db


def counts(db):
    c = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    v = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
    return c, v


def test_purge_by_cwd_removes_only_that_project(populated):
    rows = store.purge_targets(populated, cwd="/clients/cui")
    assert len(rows) == 3  # two claude + one codex, all cui
    removed = store.purge(populated, rows)
    assert removed == 3
    c, v = counts(populated)
    assert c == 1 and v == 1  # only the acme commercial chunk remains
    remaining = populated.execute("SELECT cwd FROM chunks").fetchone()[0]
    assert remaining == "/clients/acme"


def test_purge_by_session(populated):
    rows = store.purge_targets(populated, session="cui-1")
    store.purge(populated, rows)
    assert counts(populated)[0] == 3


def test_purge_by_agent(populated):
    rows = store.purge_targets(populated, agent="codex")
    store.purge(populated, rows)
    assert counts(populated)[0] == 3
    assert populated.execute(
        "SELECT count(*) FROM chunks WHERE agent='codex'").fetchone()[0] == 0


def test_filters_are_and_combined(populated):
    # cui project AND codex agent = just the one codex chunk
    rows = store.purge_targets(populated, cwd="/clients/cui", agent="codex")
    assert len(rows) == 1


def test_purge_all_wipes_everything(populated):
    rows = store.purge_targets(populated, everything=True)
    assert len(rows) == 4
    store.purge(populated, rows)
    assert counts(populated) == (0, 0)


def test_no_target_returns_no_rows(populated):
    assert store.purge_targets(populated) == []


def test_fts_no_longer_matches_purged_text(populated):
    rows = store.purge_targets(populated, cwd="/clients/cui")
    store.purge(populated, rows)
    assert store.text_query(populated, "sensitive detail", 5) == []
    assert len(store.text_query(populated, "marketing", 5)) == 1
