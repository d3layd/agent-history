"""Session notes over MCP: history_note writes, history_recall(notes=...) reads.

Same in-process Streamable HTTP harness as test_mcp: no sockets, no Ollama.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from agent_history import config, notes  # noqa: E402
from test_mcp import GH, data, hub, run_calls, text_of  # noqa: E402,F401

URL = "https://claude.ai/chat/0f6a7c2e-1234-4abc-8def-0123456789ab"
JOB = "cj_20261101T120000Z_a1b2c3"


def index_tenant(hub, tenant="dennis"):  # noqa: F811
    """What the hub's index unit does for one tenant."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"] / tenant))
        with config.tenant_home(tenant):
            assert notes.index_inbox() == 0
    finally:
        mp.undo()


def note_files(hub, machine="claude-ai"):  # noqa: F811
    return sorted((hub["inbox"] / "dennis" / machine / "notes").glob("*.jsonl"))


def test_history_note_writes_to_the_notes_inbox_redacted(hub):  # noqa: F811
    async def write(client):
        return await client.call_tool("history_note", {
            "title": "Duet: job fleet plan agreed",
            "summary": f"Agreed the three agent-history pieces; token {GH} was pasted.",
            "decisions": ["notes default to exclude in recall"],
            "duet_channel_id": "chan_7Hq2", "duet_message_id": "msg_991"})

    out = data(run_calls(hub["tokens"]["write"], write))
    assert notes.NOTE_ID.match(out["note_id"])
    assert out["source"] == {"kind": "duet", "channel_id": "chan_7Hq2",
                             "message_id": "msg_991"}
    assert out["show"] is None
    (path,) = note_files(hub)
    body = path.read_text()
    assert GH not in body and "[REDACTED:github_token]" in body
    # nothing landed where transcripts go
    assert not list((hub["inbox"] / "dennis" / "claude-ai").glob("*.jsonl"))


def test_history_note_refuses_a_missing_or_mixed_source(hub):  # noqa: F811
    async def write(client):
        none = await client.call_tool("history_note", {"title": "t", "summary": "s"})
        both = await client.call_tool("history_note", {
            "title": "t", "summary": "s", "chat_url": URL,
            "session_id": "abc", "session_machine": "MSI"})
        return none, both

    none, both = run_calls(hub["tokens"]["write"], write)
    assert none.is_error and "exactly one source" in text_of(none)
    assert both.is_error
    assert not note_files(hub)


def test_a_read_token_cannot_write_notes(hub):  # noqa: F811
    async def write(client):
        return await client.call_tool("history_note", {
            "title": "t", "summary": "s", "chat_url": URL})

    assert run_calls(hub["tokens"]["read"], write).is_error


def test_recall_finds_notes_only_when_asked_and_links_the_source(hub):  # noqa: F811
    async def write(client):
        return await client.call_tool("history_note", {
            "title": "Vault rotation job finished",
            "summary": "The claude -p job rotated the vault keys and opened a draft PR.",
            "session_id": "5f2b8c1e-0000-4000-8000-00000000aaaa",
            "session_machine": "claude-jobs", "job_id": JOB,
            "span_start": "2026-11-01T12:00:01Z", "span_end": "2026-11-01T12:40:00Z"})

    run_calls(hub["tokens"]["write"], write)
    index_tenant(hub)

    async def read(client):
        default = await client.call_tool("history_recall", {"query": "vault rotation",
                                                            "mode": "text"})
        only = await client.call_tool("history_recall", {"query": "vault rotation",
                                                         "mode": "text", "notes": "only"})
        include = await client.call_tool("history_recall", {"query": "vault rotation",
                                                            "mode": "text", "notes": "include"})
        return data(default), data(only), data(include)

    default, only, include = run_calls(hub["tokens"]["read"], read)
    # default: transcripts exactly as before, no notes, no new fields
    assert all("type" not in hit for hit in default["results"])
    assert not any("[note]" in hit["excerpt"] for hit in default["results"])
    # only: the note, with the history_show call that opens its span
    (note,) = only["results"]
    assert note["type"] == "note" and note["title"] == "Vault rotation job finished"
    assert note["show"] == {"session": JOB, "around": "2026-11-01T12:00:01Z"}
    assert note["source"]["job_id"] == JOB and note["machine"] == "claude-ai"
    # include: notes first, then the transcript hits
    types = [hit["type"] for hit in include["results"]]
    assert types[0] == "note" and "transcript" in types
    assert types == sorted(types, key=lambda t: t != "note")
    assert [h["rank"] for h in include["results"]] == list(range(1, len(types) + 1))


def test_notes_never_enter_the_transcript_index(hub):  # noqa: F811
    async def write(client):
        return await client.call_tool("history_note", {
            "title": "Separate store check", "summary": "zebra-unique-note-token here",
            "chat_url": URL})

    run_calls(hub["tokens"]["write"], write)
    index_tenant(hub)

    async def read(client):
        return data(await client.call_tool("history_recall", {
            "query": "zebra unique note token", "mode": "text"}))

    assert not any("zebra" in h["excerpt"] for h in run_calls(hub["tokens"]["read"], read)["results"])


def test_notes_stay_inside_their_tenant(hub):  # noqa: F811
    async def write(client):
        return await client.call_tool("history_note", {
            "title": "Dennis-only note", "summary": "kept in the dennis tenant",
            "chat_url": URL})

    run_calls(hub["tokens"]["write"], write)
    index_tenant(hub)

    async def read(client):
        return data(await client.call_tool("history_recall", {
            "query": "dennis tenant", "mode": "text", "notes": "only"}))

    assert run_calls(hub["tokens"]["eagle"], read)["count"] == 0
    assert run_calls(hub["tokens"]["read"], read)["count"] == 1


def test_recall_rejects_an_unknown_notes_option(hub):  # noqa: F811
    async def read(client):
        return await client.call_tool("history_recall", {"query": "x", "notes": "maybe"})

    assert run_calls(hub["tokens"]["read"], read).is_error
