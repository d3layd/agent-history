"""The show command and the viewer's API layer."""
from __future__ import annotations

import hashlib

import pytest

from agent_history import embed, show, store

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def put(db, text, *, ts, session="s1", origin="/t/s1.jsonl", role="user"):
    store.insert_chunk(
        db, agent="claude-code", origin=origin, session_id=session, cwd="/w",
        timestamp=ts, role=role, text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def conversation(fake_embed):
    db, _ = store.open_for_write()
    for hour in range(1, 7):
        put(db, f"message number {hour} in the long conversation",
            ts=f"2026-01-01T0{hour}:00:00Z")
    put(db, "a chunk from an unrelated conversation entirely",
        ts="2026-01-01T03:30:00Z", session="s2", origin="/t/s2.jsonl")
    return db


def test_rows_for_selects_one_conversation_by_session(conversation):
    rows = show.rows_for("s1")
    assert len(rows) == 6
    assert all(r["session_id"] == "s1" for r in rows)
    assert rows[0]["timestamp"] < rows[-1]["timestamp"]


def test_rows_for_selects_by_origin_too(conversation):
    rows = show.rows_for("/t/s2.jsonl")
    assert len(rows) == 1


def test_limit_keeps_the_end_of_the_conversation(conversation):
    rows = show.rows_for("s1", limit=2)
    assert [r["text"] for r in rows] == [
        "message number 5 in the long conversation",
        "message number 6 in the long conversation",
    ]


def test_around_centres_the_window(conversation):
    rows = show.rows_for("s1", around="2026-01-01T03:00:00Z", limit=3)
    texts = [r["text"] for r in rows]
    assert "message number 3 in the long conversation" in texts
    assert len(rows) == 3


def test_unknown_target_returns_nothing(conversation):
    assert show.rows_for("no-such-session") == []


def test_run_exit_codes(conversation, capsys):
    assert show.run("s1") == 0
    assert "message number 1" in capsys.readouterr().out
    assert show.run("no-such-session") == 1


def test_viewer_binds_loopback_and_serves_the_api(conversation):
    import http.client
    import threading

    from agent_history import viewer

    server = None
    # Pick a free port by binding port 0.
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer((viewer.HOST, 0), viewer.Handler)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        conn.request("GET", "/api/stats")
        body = conn.getresponse().read().decode()
        assert '"total"' in body

        conn.request("GET", "/api/session?session=s1&limit=2")
        resp = conn.getresponse()
        assert resp.status == 200
        assert "message number 6" in resp.read().decode()

        conn.request("GET", "/")
        assert b"agent-history" in conn.getresponse().read()

        conn.request("GET", "/nope")
        assert conn.getresponse().status == 404
    finally:
        server.shutdown()
        server.server_close()
