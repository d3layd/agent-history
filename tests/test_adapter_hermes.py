"""Hermes state.db parsing — JSON content walk, schema drift, profile naming."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_history.adapters.hermes import HermesAdapter


def make_profile(root, name="hermes-ops", *, with_cwd=True):
    profile = root / name
    profile.mkdir(parents=True)
    db = sqlite3.connect(profile / "state.db")
    cwd_col = ", cwd TEXT" if with_cwd else ""
    db.executescript(
        f"""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT,
                               system_prompt TEXT, started_at REAL{cwd_col});
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                               session_id TEXT, role TEXT, content TEXT,
                               tool_name TEXT, timestamp REAL);
        """
    )
    return db


@pytest.fixture
def hermes(isolated_env, monkeypatch):
    """One profile directory; AGENT_HISTORY_HERMES_DIRS points at its parent
    via conftest, but the adapter expects profile dirs, so repoint here."""
    root = isolated_env / "hermes"
    db = make_profile(root)
    monkeypatch.setenv("AGENT_HISTORY_HERMES_DIRS", str(root / "hermes-ops"))

    def _msg(role, content, *, session="hs-1", cwd="/srv/ops",
             epoch=1770000000.5):
        db.execute(
            "INSERT OR IGNORE INTO sessions (id, source, system_prompt, "
            "started_at, cwd) VALUES (?, 'cli', 'SECRET system prompt', ?, ?)",
            (session, epoch, cwd))
        db.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?,?,?,?)", (session, role, content, epoch))
        db.commit()

    _msg.root = root
    _msg.db = db
    yield _msg
    db.close()


def records():
    return list(HermesAdapter().records())


def test_plain_text_messages_are_kept(hermes):
    hermes("user", "what happened to the winch life bot")
    hermes("assistant", "the old systemd unit auto-started it after the reboot")
    got = records()
    assert [r.role for r in got] == ["user", "assistant"]
    assert got[0].agent == "hermes"
    assert got[0].session_id == "hs-1"
    assert got[0].cwd == "/srv/ops"
    assert got[0].timestamp.startswith("202")
    assert got[0].origin.endswith("#messages")


def test_json_content_is_walked_for_text(hermes):
    hermes("assistant", json.dumps([
        {"type": "text", "text": "the gateway restarted cleanly after the fix"},
        {"type": "tool_result", "id": "t1",
         "content": [{"type": "text", "text": "exit code 0 from the restart"}]},
    ]))
    texts = [r.text for r in records()]
    assert "the gateway restarted cleanly after the fix" in texts
    assert "exit code 0 from the restart" in texts


def test_json_that_is_not_blocks_falls_back_sanely(hermes):
    hermes("user", '{"note": "no text or content keys anywhere here"}')
    # A dict with neither "text" nor "content" yields nothing rather than
    # dumping metadata into the index.
    assert records() == []


def test_tool_role_rows_are_never_selected(hermes):
    hermes("tool", "raw tool output that stays out of the index")
    hermes("user", "an ordinary question that goes in")
    assert [r.role for r in records()] == ["user"]


def test_system_prompt_column_never_leaks(hermes):
    hermes("user", "a normal message in a session with a system prompt")
    assert "SECRET" not in " ".join(r.text for r in records())


def test_schema_without_cwd_column_uses_profile_name(isolated_env, monkeypatch):
    root = isolated_env / "hermes2"
    db = make_profile(root, name="hermes-family", with_cwd=False)
    db.execute("INSERT INTO sessions (id, source, system_prompt, started_at) "
               "VALUES ('hs-9', 'cli', 'x', 1770000000)")
    db.execute("INSERT INTO messages (session_id, role, content, timestamp) "
               "VALUES ('hs-9', 'user', 'a message in the older schema', "
               "1770000000)")
    db.commit()
    db.close()
    monkeypatch.setenv("AGENT_HISTORY_HERMES_DIRS", str(root / "hermes-family"))
    got = records()
    assert len(got) == 1
    assert got[0].cwd == "hermes-family"


def test_multiple_profiles_are_all_read(isolated_env, monkeypatch):
    root = isolated_env / "hermes3"
    for name in ("hermes-ops", "hermes-honors"):
        db = make_profile(root, name=name)
        db.execute("INSERT INTO sessions (id, source, system_prompt, "
                   "started_at, cwd) VALUES ('s', 'cli', 'x', 1, '/w')")
        db.execute("INSERT INTO messages (session_id, role, content, timestamp)"
                   " VALUES ('s', 'user', ?, 1)",
                   (f"a message stored in the {name} profile",))
        db.commit()
        db.close()
    import os
    monkeypatch.setenv(
        "AGENT_HISTORY_HERMES_DIRS",
        os.pathsep.join(str(root / n) for n in ("hermes-ops", "hermes-honors")))
    assert len(records()) == 2


def test_unavailable_without_state_db(isolated_env):
    assert HermesAdapter().available() is False
