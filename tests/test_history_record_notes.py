"""history_record writes notes (piece D), and the reads Duet relies on still
find records: default recall, surface=claude-ai-chat, and history_show on the
record's chat session. Old-format record lines are diverted to notes, and
`notes migrate-records` moves existing ones out of the transcript index.
"""
from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("mcp")

from agent_history import cli, config, embed, index, notes, store  # noqa: E402
from test_mcp import GH, RECORD, data, hub, put, record_files, run_calls, text_of  # noqa: E402,F401

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def index_hub(hub):  # noqa: F811
    """What the hub's index unit (trigger) does: transcripts, then notes."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AGENT_HISTORY_HUB", "1")
        mp.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"] / "dennis"))
        with config.tenant_home("dennis"):
            index.run()
            notes.index_inbox()
    finally:
        mp.undo()


def write(hub, **args):  # noqa: F811
    async def call(client):
        return await client.call_tool("history_record", {**RECORD, **args})
    return run_calls(hub["tokens"]["write"], call)


def read(hub, tool, args):  # noqa: F811
    async def call(client):
        return await client.call_tool(tool, args)
    return run_calls(hub["tokens"]["read"], call)


def chunks_like(pattern):
    with config.tenant_home("dennis"):
        db = store.open_for_read(check_model=False)
        try:
            return db.execute("SELECT COUNT(*) FROM chunks WHERE agent = 'claude-ai-chat' "
                              "AND text LIKE ?", (f"%{pattern}%",)).fetchone()[0]
        finally:
            db.close()


# ------------------------------------------------------------- read compat

def test_default_recall_still_returns_records_in_their_old_shape(hub):  # noqa: F811
    out = data(write(hub))
    index_hub(hub)
    assert chunks_like(out["record_id"]) == 0, "the record is not a transcript chunk"
    found = data(read(hub, "history_recall", {"query": "hub MCP design scoped tokens",
                                              "mode": "text"}))
    hit = next(h for h in found["results"] if h["session_id"] == out["session"])
    assert hit["agent"] == "claude-ai-chat" and hit["role"] == "summary"
    assert hit["machine"] == "claude-ai" and hit["project"] == "agent-history"
    assert hit["session"] == out["session"]
    assert hit["excerpt"].startswith("[claude.ai chat record] Decided the hub MCP design")
    assert GH not in json.dumps(found)
    assert [h["rank"] for h in found["results"]] == list(range(1, found["count"] + 1))


def test_filters_keep_their_meaning(hub):  # noqa: F811
    data(write(hub))
    index_hub(hub)
    q = {"query": "hub MCP design scoped tokens", "mode": "text"}
    surface = data(read(hub, "history_recall", {**q, "surface": "claude-ai-chat"}))
    assert surface["count"] >= 1
    assert all(h["agent"] == "claude-ai-chat" for h in surface["results"])
    other = data(read(hub, "history_recall", {**q, "agent": "claude-code"}))
    assert all(h["agent"] != "claude-ai-chat" for h in other["results"])
    by_project = data(read(hub, "history_recall", {**q, "cwd": "agent-history"}))
    assert any(h["agent"] == "claude-ai-chat" for h in by_project["results"])
    wrong_project = data(read(hub, "history_recall", {**q, "cwd": "storm-check"}))
    assert not any(h["agent"] == "claude-ai-chat" for h in wrong_project["results"])


def test_show_on_the_chat_session_returns_the_record_trail(hub):  # noqa: F811
    first = data(write(hub))
    data(write(hub, title="Later milestone: shipped the MCP server"))
    index_hub(hub)
    shown = data(read(hub, "history_show", {"session": first["session"]}))
    assert shown["agent"] == "claude-ai-chat" and shown["session"] == first["session"]
    assert [m["role"] for m in shown["messages"]] == ["summary", "summary"]
    assert any("Later milestone" in m["text"] for m in shown["messages"])


def test_records_also_show_up_as_notes_when_asked(hub):  # noqa: F811
    out = data(write(hub))
    index_hub(hub)
    only = data(read(hub, "history_recall", {"query": "hub MCP design", "mode": "text",
                                             "notes": "only"}))
    (note,) = [r for r in only["results"] if r.get("origin") == "history_record"]
    assert note["type"] == "note" and note["note_id"] == out["note_id"]


# --------------------------------------------------------------- source links

def test_duet_ids_give_the_record_a_duet_source(hub):  # noqa: F811
    out = data(write(hub, chat_url=None, duet_channel_id="chan_7Hq2", duet_message_id="msg_991"))
    (path,) = record_files(hub)
    note = notes.parse(path.read_text())
    assert note["source"] == {"kind": "duet", "channel_id": "chan_7Hq2", "message_id": "msg_991"}
    assert note["record"]["session"] == out["session"]


def test_claude_code_ids_give_a_claude_code_source(hub):  # noqa: F811
    data(write(hub, chat_url=None, session_id="5f2b8c1e-0000-4000-8000-00000000aaaa",
               session_machine="MSI", job_id="cj_20261101T120000Z_a1b2c3"))
    note = notes.parse(record_files(hub)[0].read_text())
    assert note["source"]["kind"] == "claude-code"
    assert note["source"]["job_id"] == "cj_20261101T120000Z_a1b2c3"


def test_a_record_without_a_link_is_linked_to_its_own_session(hub):  # noqa: F811
    out = data(write(hub, chat_url=None))
    note = notes.parse(record_files(hub)[0].read_text())
    assert note["source"] == {"kind": "record", "client": "claude-ai", "session": out["session"]}
    assert out["session"].startswith("claude-ai-chat-rec-")
    index_hub(hub)
    assert data(read(hub, "history_show", {"session": out["session"]}))["messages"]


def test_a_bad_duet_id_is_refused(hub):  # noqa: F811
    result = write(hub, chat_url=None, duet_channel_id="chan_7Hq2")
    assert result.is_error and "message id" in text_of(result)
    assert not record_files(hub)


def test_only_history_record_can_use_a_record_source():
    with pytest.raises(notes.NoteError):
        notes.build(title="t", summary="s",
                    source=notes.record_source("claude-ai", "claude-ai-chat-rec-1234567890"))
    note = notes.build(title="t", summary="s", note_id=notes.record_note_id("rec-abcdef123456"),
                       source=notes.record_source("claude-ai", "claude-ai-chat-rec-abcdef123456"),
                       record={"record_id": "rec-abcdef123456",
                               "session": "claude-ai-chat-rec-abcdef123456",
                               "project": None, "text": "[claude.ai chat record] t"})
    assert notes.parse(json.dumps(note)) is not None
    forged = {**note, "note_id": "note-" + "0" * 20}
    assert notes.parse(json.dumps(forged)) is None


# ------------------------------------------------------------------ diversion

LEGACY_TEXT = ("[claude.ai chat record] Paused five Clamps crons\nProject: Emerald Media ops\n"
               "Record: rec-9b7ddcbc2c1d4e58a1eb\n\nSummary:\nPaused the crons that "
               "duplicate Duet's routines.\n\nDecisions:\n- resume if anyone misses them")


def legacy_line(record_id="rec-9b7ddcbc2c1d4e58a1eb", text=LEGACY_TEXT):
    return json.dumps({"v": 1, "machine": "claude-ai", "agent": "claude-ai-chat",
                       "origin": f"mcp:claude-ai-chat/{record_id}",
                       "session_id": f"claude-ai-chat-{record_id}", "cwd": "Emerald Media ops",
                       "timestamp": "2026-10-29T23:49:00Z", "role": "summary", "text": text})


def test_an_old_format_record_line_goes_to_notes_not_the_transcript_index(hub):  # noqa: F811
    inbox = hub["inbox"] / "dennis" / "claude-ai"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "20261029T234900Z-old.jsonl").write_text(legacy_line() + "\n" + legacy_line() + "\n")
    index_hub(hub)
    assert chunks_like("Paused five Clamps crons") == 0
    with config.tenant_home("dennis"):
        hits = notes.search("Clamps crons duplicate", mode="text", records=True)
    assert len(hits) == 1
    assert hits[0]["record_session"] == "claude-ai-chat-rec-9b7ddcbc2c1d4e58a1eb"
    assert hits[0]["source"]["kind"] == "record"
    found = data(read(hub, "history_recall", {"query": "Clamps crons", "mode": "text",
                                              "surface": "claude-ai-chat"}))
    assert found["results"][0]["excerpt"].startswith("[claude.ai chat record] Paused five")


# ------------------------------------------------------------------ migration

def seed_old_records(hub):  # noqa: F811
    """Records as the old MCP server left them: chunks in the transcript
    index. One is long enough to span two overlapping chunks."""
    long_summary = " ".join(f"step {n} of the long migration plan." for n in range(80))
    long_text = (f"[claude.ai chat record] A long record\nProject: agent-history\n"
                 f"Record: rec-aaaaaaaaaaaa\n\nSummary:\n{long_summary}")
    with config.tenant_home("dennis"):
        db, _ = store.open_for_write()
        for record_id, text in (("rec-9b7ddcbc2c1d4e58a1eb", LEGACY_TEXT),
                                ("rec-aaaaaaaaaaaa", long_text)):
            spans = [(0, len(text))] if len(text) <= 1500 else [(0, 1500), (1300, len(text))]
            for n, (a, b) in enumerate(spans):
                piece = text[a:b]
                store.insert_chunk(
                    db, agent="claude-ai-chat", origin=f"mcp:claude-ai-chat/{record_id}",
                    session_id=f"claude-ai-chat-{record_id}", cwd="agent-history",
                    timestamp="2026-10-29T23:49:00Z", role="summary", text=piece,
                    content_hash=hashlib.sha256(piece.encode()).hexdigest(),
                    vector=embed.embed(piece), machine="claude-ai", chunk_ordinal=n,
                    char_start=a, char_end=b)
        db.commit()
        db.close()
    return long_text


def test_migrate_records_dry_run_then_move(hub, capsys):  # noqa: F811
    long_text = seed_old_records(hub)
    assert chunks_like("chat record") == 2
    with config.tenant_home("dennis"):
        assert cli.main(["notes", "migrate-records"]) == 0
    out = capsys.readouterr().out
    assert "2 record(s), 3 chunk(s)" in out and "dry run" in out
    assert chunks_like("chat record") == 2
    with config.tenant_home("dennis"):
        assert not notes.db_path().exists() or not notes.search("long migration", mode="text")
        assert cli.main(["notes", "migrate-records", "--yes"]) == 0
        backups = list(config.data_home().glob("migrate-records-*.jsonl"))
        hits = notes.search("long migration plan", mode="text", records=True)
    assert "moved 2 record(s)" in capsys.readouterr().out
    assert chunks_like("chat record") == 0 and chunks_like("long migration") == 0
    assert len(backups) == 1 and len(backups[0].read_text().splitlines()) == 3
    (hit,) = hits
    assert hit["text"] == long_text, "the overlapping chunks are stitched back exactly"
    shown = data(read(hub, "history_show", {"session": "claude-ai-chat-rec-aaaaaaaaaaaa"}))
    assert shown["messages"][0]["text"].startswith("[claude.ai chat record] A long record")
    found = data(read(hub, "history_recall", {"query": "Clamps crons", "mode": "text"}))
    assert found["results"][0]["agent"] == "claude-ai-chat"


def test_migrate_records_is_idempotent(hub, capsys):  # noqa: F811
    seed_old_records(hub)
    with config.tenant_home("dennis"):
        assert notes.migrate_records(apply=True) == 0
        assert notes.migrate_records(apply=True) == 0
        db, _ = notes.open_store(write=False)
        try:
            assert db.execute("SELECT COUNT(*) FROM notes WHERE origin = 'history_record'"
                              ).fetchone()[0] == 2
        finally:
            db.close()
    assert "0 record(s)" in capsys.readouterr().out
