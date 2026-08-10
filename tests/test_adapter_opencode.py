"""OpenCode SQLite parsing — and that nothing beyond named columns is touched."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_history.adapters.opencode import OpenCodeAdapter


@pytest.fixture
def opencode_db(isolated_env):
    path = isolated_env / "opencode/opencode.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                              time_created INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
                           session_id TEXT, data TEXT);
        -- The trap this adapter must never fall into:
        CREATE TABLE control_account (email TEXT, access_token TEXT,
                                      refresh_token TEXT);
        INSERT INTO control_account VALUES
            ('a@b.c', 'oauth-access-SECRET', 'oauth-refresh-SECRET');
        """
    )

    def _add(session, mid, role, text, *, directory="/proj", created=1771255190362,
             part_type="text", cwd=None):
        db.execute("INSERT OR IGNORE INTO session VALUES (?,?,?,?)",
                   (session, directory, "a title", created))
        data = {"role": role, "time": {"created": created}}
        if cwd:
            data["path"] = {"cwd": cwd}
        db.execute("INSERT OR REPLACE INTO message VALUES (?,?,?)",
                   (mid, session, json.dumps(data)))
        db.execute("INSERT INTO part VALUES (?,?,?,?)",
                   (f"prt-{mid}-{abs(hash(text))}", mid, session,
                    json.dumps({"type": part_type, "text": text})))
        db.commit()

    _add.path = path
    yield _add
    db.close()


def records():
    return list(OpenCodeAdapter().records())


def test_text_parts_join_message_and_session(opencode_db):
    opencode_db("ses-1", "msg-1", "user", "review the project and summarise it")
    opencode_db("ses-1", "msg-2", "assistant", "it is a Next.js 16 app router site")
    got = records()
    assert [r.role for r in got] == ["user", "assistant"]
    assert got[0].agent == "opencode"
    assert got[0].session_id == "ses-1"
    assert got[0].cwd == "/proj"
    assert got[0].timestamp.startswith("2026-")
    assert got[0].origin.startswith("db:") and got[0].origin.endswith("#part")


def test_message_cwd_wins_over_session_directory(opencode_db):
    opencode_db("ses-1", "msg-1", "assistant", "working from a subdirectory now",
                cwd="/proj/sub")
    assert records()[0].cwd == "/proj/sub"


def test_non_text_parts_are_skipped(opencode_db):
    opencode_db("ses-1", "msg-1", "assistant", "compressed reasoning artifact",
                part_type="reasoning")
    opencode_db("ses-1", "msg-2", "assistant", "a real visible reply to keep")
    got = records()
    assert len(got) == 1
    assert got[0].text == "a real visible reply to keep"


def test_tool_and_system_roles_are_skipped(opencode_db):
    opencode_db("ses-1", "msg-1", "tool", "raw tool output should not be indexed")
    opencode_db("ses-1", "msg-2", "system", "system prompt must not be indexed")
    assert records() == []


def test_oauth_tokens_never_appear_in_records(opencode_db):
    opencode_db("ses-1", "msg-1", "user", "an ordinary indexable user message")
    text = " ".join(r.text for r in records())
    assert "SECRET" not in text


def test_unavailable_without_db_file(isolated_env):
    assert OpenCodeAdapter().available() is False


def test_corrupt_message_data_does_not_abort(opencode_db):
    sqlite3.connect(opencode_db.path).execute(
        "INSERT INTO message VALUES ('msg-bad', 'ses-1', '{not json')"
    ).connection.commit()
    opencode_db("ses-1", "msg-2", "user", "a valid message after the corrupt row")
    assert len(records()) == 1
