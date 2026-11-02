"""The `claude` alias kind: a lookup pointer from a Claude Code session uuid
to the hub session that files it (a job), never evidence to merge two."""
from __future__ import annotations

import sqlite3

from agent_history import sessions


def db_with(*uids):
    db = sqlite3.connect(":memory:")
    db.executescript(sessions.SCHEMA)
    for n, uid in enumerate(uids):
        sessions.register_session(db, uid, agent="claude-code", machine="claude-jobs",
                                  session_id=f"cj_2026110{n}T120000Z_a1b2c3", origin=None,
                                  timestamp=f"2026-11-0{n + 1}T00:00:00Z")
    return db


def test_claude_is_a_known_kind_that_never_merges():
    assert "claude" in sessions.ALIAS_KINDS
    assert "claude" not in sessions.IDENTITY_KINDS
    a, b = sessions.derive_uid("claude-code", "x", "a"), sessions.derive_uid("claude-code", "x", "b")
    db = db_with(a, b)
    assert sessions.link_alias(db, a, "claude", "uuid-1") == a
    assert sessions.link_alias(db, b, "claude", "uuid-1") == b   # no merge
    assert sessions.canonical(db, b) == b
    assert sessions.resolve_session(db, "uuid-1") == a           # first claim wins
    assert ("claude", "uuid-1") in sessions.links_of(db, a)
