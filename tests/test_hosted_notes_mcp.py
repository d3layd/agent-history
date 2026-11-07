"""Hosted-session notes through the MCP tools, with a temporary hub.

Same in-process Streamable HTTP harness as test_mcp: no sockets, no Ollama, no
live hub. This is the closest thing to the cloud-session flow that runs without
a deploy.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from agent_history import config, hosted, mcp_server, notes, store  # noqa: E402
from agent_history.mcp_auth import Identity  # noqa: E402
from test_mcp import GH, data, hub, run_calls, text_of  # noqa: E402,F401

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

SID = "session_01HhNr55p49oCfdQAUj4K8uQ"
LINK = f"https://claude.ai/code/{SID}"
CHAT = "https://claude.ai/chat/0f6a7c2e-1234-4abc-8def-0123456789ab"
HOSTED = {"provider": "claude-ai", "session_id": SID, "session_url": LINK}
NOTE = {"title": "Cloud session wrapped up", "summary": "Fixed the zebra import in the cloud."}


def call(hub, token, tool, args):  # noqa: F811
    async def go(client):
        return await client.call_tool(tool, args)
    return run_calls(hub["tokens"][token], go)


def index_tenant(hub, tenant="dennis"):  # noqa: F811
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"] / tenant))
        with config.tenant_home(tenant):
            assert notes.index_inbox() == 0
    finally:
        mp.undo()


def note_files(hub, machine="claude-ai"):  # noqa: F811
    return sorted((hub["inbox"] / "dennis" / machine / "notes").glob("*.jsonl"))


def recall_only(hub, query="zebra import"):  # noqa: F811
    return data(call(hub, "read", "history_recall",
                     {"query": query, "mode": "text", "notes": "only"}))


# ----------------------------------------------------------------- done when

def test_done_when_a_cloud_session_note_is_accepted_and_recalled_with_its_link(hub):  # noqa: F811
    out = data(call(hub, "write", "history_note", {**NOTE, **HOSTED}))
    assert out["source"] == {"kind": "hosted-session", "provider": "claude-ai",
                             "session_id": SID, "url": LINK}
    assert out["link"] == LINK
    assert out["show"] == {"session": SID, "around": None}

    index_tenant(hub)
    (hit,) = recall_only(hub)["results"]
    assert hit["type"] == "note" and hit["title"] == NOTE["title"]
    assert hit["link"] == LINK
    assert hit["source"]["provider"] == "claude-ai"
    assert hit["show"]["session"] == SID
    assert LINK in hit["excerpt"]
    assert hit["machine"] == "claude-ai"   # the token's inbox, not a claim by the note


def test_done_when_a_provider_outside_the_enum_is_refused(hub):  # noqa: F811
    result = call(hub, "write", "history_note",
                  {**NOTE, **HOSTED, "provider": "not-a-provider"})
    assert result.is_error
    assert not note_files(hub)


def test_done_when_a_url_host_that_does_not_match_its_provider_is_refused(hub):  # noqa: F811
    result = call(hub, "write", "history_note",
                  {**NOTE, **HOSTED, "session_url": f"https://chatgpt.com/code/{SID}"})
    assert result.is_error and "claude.ai" in text_of(result)
    assert not note_files(hub)


@pytest.mark.parametrize("url", [
    f"https://claude.ai.evil.com/code/{SID}",
    f"https://claude.ai@evil.com/code/{SID}",
    f"https://user:pw@claude.ai/code/{SID}",
    f"http://claude.ai/code/{SID}",
    f"https://claude.ai/chat/{SID}",
    f"https://claude.ai/code/{SID}?x=1",
    "https://claude.ai/code/session_someoneElse",
])
def test_spoofed_urls_are_refused_at_the_tool_and_write_nothing(hub, url):  # noqa: F811
    result = call(hub, "write", "history_note", {**NOTE, **HOSTED, "session_url": url})
    assert result.is_error
    assert not note_files(hub)


def test_the_logic_layer_refuses_an_unknown_provider_without_the_sdk_schema(hub):  # noqa: F811
    """The enum in the schema is one wall; the refusal in notes.py is the other."""
    identity = Identity(name="dennis-write", scopes=frozenset({"write"}), tenant="dennis",
                        machine="claude-ai")
    with pytest.raises(mcp_server.ToolFailure, match="provider must be one of"):
        mcp_server.note(identity, title="t", summary="s", provider="not-a-provider",
                        session_id=SID, session_url=LINK)
    assert not note_files(hub)


def test_an_unknown_provider_cannot_grow_the_enum_through_the_tool(hub):  # noqa: F811
    call(hub, "write", "history_note", {**NOTE, **HOSTED, "provider": "brand-new"})

    async def schema(client):
        tools = (await client.list_tools()).tools
        return next(t for t in tools if t.name == "history_note").input_schema

    assert "brand-new" not in json.dumps(run_calls(hub["tokens"]["write"], schema))
    assert hosted.slugs() == ("claude-ai",)


# ---------------------------------------------------------------- tool schema

@pytest.mark.parametrize("tool", ["history_note", "history_record"])
def test_provider_is_an_enum_in_the_schema_and_the_description_names_the_fields(hub, tool):  # noqa: F811
    async def listing(client):
        return next(t for t in (await client.list_tools()).tools if t.name == tool)

    found = run_calls(hub["tokens"]["write"], listing)
    options = found.input_schema["properties"]["provider"]["anyOf"]
    assert {"enum": ["claude-ai"], "type": "string"} in [
        {k: v for k, v in o.items() if k in ("enum", "type")} for o in options]
    assert {"type": "null"} in options, "optional: the other source kinds don't pass it"
    assert not any(o.get("type") == "string" and "enum" not in o for o in options), "no free text"
    assert "session_url" in found.input_schema["properties"]
    for word in ("provider", "session_id", "session_url", "claude-ai", "claude.ai/code"):
        assert word in found.description


def test_recall_and_show_describe_the_link(hub):  # noqa: F811
    async def listing(client):
        return {t.name: t.description for t in (await client.list_tools()).tools}

    described = run_calls(hub["tokens"]["read"], listing)
    assert "link" in described["history_recall"] and "link" in described["history_show"]


# --------------------------------------------------------------- history_show

def test_show_returns_the_link_with_no_transcript(hub):  # noqa: F811
    call(hub, "write", "history_note", {**NOTE, **HOSTED})
    index_tenant(hub)
    out = data(call(hub, "read", "history_show", {"session": SID}))
    assert out["link"] == LINK and out["provider"] == "claude-ai"
    assert out["messages"] == [] and out["session"] == SID


def test_show_returns_null_without_error_for_a_hosted_id_with_no_note(hub):  # noqa: F811
    result = call(hub, "read", "history_show", {"session": "session_01NoNoteForThisOneYet"})
    assert not result.is_error
    out = data(result)
    assert out["link"] is None and out["messages"] == []
    assert out["provider"] == "claude-ai"


def test_show_before_the_note_is_indexed_is_null_not_an_error(hub):  # noqa: F811
    call(hub, "write", "history_note", {**NOTE, **HOSTED})
    out = data(call(hub, "read", "history_show", {"session": SID}))
    assert out["link"] is None


def test_show_with_no_transcript_index_at_all_still_answers(hub):  # noqa: F811
    call(hub, "write", "history_note", {**NOTE, **HOSTED})
    index_tenant(hub)
    with config.tenant_home("dennis"):
        config.db_path().unlink()
    out = data(call(hub, "read", "history_show", {"session": SID}))
    assert out["link"] == LINK and out["messages"] == []


def test_show_keeps_the_transcript_and_adds_the_link_when_both_exist(hub):  # noqa: F811
    from test_mcp import put

    with config.tenant_home("dennis"):
        db, _ = store.open_for_write()
        put(db, "a transcript that did get indexed for this cloud session", session=SID)
        db.commit()
        db.close()
    call(hub, "write", "history_note", {**NOTE, **HOSTED})
    index_tenant(hub)
    out = data(call(hub, "read", "history_show", {"session": SID}))
    assert out["link"] == LINK
    assert any("did get indexed" in m["text"] for m in out["messages"])


def test_show_still_errors_for_an_unknown_id_that_is_not_hosted(hub):  # noqa: F811
    assert call(hub, "read", "history_show", {"session": "nope"}).is_error
    assert call(hub, "read", "history_show", {"session": "session_a-b"}).is_error


def test_show_on_an_ordinary_transcript_gets_no_link_field(hub):  # noqa: F811
    out = data(call(hub, "read", "history_show", {"session": "sess-dennis"}))
    assert "link" not in out and "provider" not in out and out["messages"]


def test_a_link_stays_inside_its_tenant(hub):  # noqa: F811
    call(hub, "write", "history_note", {**NOTE, **HOSTED})
    index_tenant(hub)
    assert recall_only(hub)["count"] == 1
    assert data(call(hub, "eagle", "history_recall",
                     {"query": "zebra import", "mode": "text", "notes": "only"}))["count"] == 0
    assert data(call(hub, "eagle", "history_show", {"session": SID}))["link"] is None


# ------------------------------------------------------------------ redaction

def test_secrets_in_the_note_are_redacted_and_the_link_survives(hub):  # noqa: F811
    out = data(call(hub, "write", "history_note", {
        **HOSTED, "title": "Rotated keys", "summary": f"zebra import; token {GH} was pasted.",
        "links": [LINK, f"https://x.test/?t={GH}"]}))
    assert out["redacted"]["github_token"] >= 2
    (path,) = note_files(hub)
    body = path.read_text()
    assert GH not in body and "[REDACTED:github_token]" in body
    index_tenant(hub)
    (hit,) = recall_only(hub)["results"]
    assert hit["link"] == LINK and GH not in json.dumps(hit)
    assert LINK in hit["excerpt"]


def test_a_refusal_does_not_echo_a_secret_in_the_url(hub):  # noqa: F811
    result = call(hub, "write", "history_note",
                  {**NOTE, **HOSTED, "session_url": f"{LINK}?token={GH}"})
    assert result.is_error and GH not in text_of(result)


# -------------------------------------------------------------- history_record

def test_history_record_takes_a_hosted_source(hub):  # noqa: F811
    out = data(call(hub, "write", "history_record", {**NOTE, **HOSTED}))
    assert out["record_id"].startswith("rec-")
    index_tenant(hub)
    (hit,) = recall_only(hub)["results"]
    assert hit["source"]["kind"] == "hosted-session" and hit["link"] == LINK
    # The default recall returns records in their old shape; the link is in the text.
    default = data(call(hub, "read", "history_recall", {"query": "zebra import", "mode": "text"}))
    assert any(LINK in r["excerpt"] for r in default["results"])
    assert data(call(hub, "read", "history_show", {"session": SID}))["link"] == LINK


@pytest.mark.parametrize("patch", [
    {"provider": "not-a-provider"},
    {"session_url": f"https://claude.ai.evil.com/code/{SID}"},
    {"session_url": None},
    {"session_machine": "MSI"},
    {"chat_url": CHAT},
    {"duet_channel_id": "c1", "duet_message_id": "m1"},
])
def test_history_record_refuses_a_bad_or_mixed_hosted_source(hub, patch):  # noqa: F811
    args = {k: v for k, v in {**NOTE, **HOSTED, **patch}.items() if v is not None}
    result = call(hub, "write", "history_record", args)
    assert result.is_error
    assert not note_files(hub)


# ------------------------------------------------------- existing kinds intact

@pytest.mark.parametrize("args,kind,show", [
    ({"chat_url": CHAT}, "claude-ai", {"session": "0f6a7c2e-1234-4abc-8def-0123456789ab",
                                       "around": None}),
    ({"duet_channel_id": "chan_7Hq2", "duet_message_id": "msg_991"}, "duet", None),
    ({"session_id": "sess-1", "session_machine": "MSI"}, "claude-code",
     {"session": "sess-1", "around": None}),
    ({"session_id": "sess-1", "session_machine": "MSI", "job_id": "cj_20261101T120000Z_a1b2c3"},
     "claude-code", {"session": "cj_20261101T120000Z_a1b2c3", "around": None}),
])
def test_the_existing_kinds_work_and_their_answers_have_no_new_fields(hub, args, kind, show):  # noqa: F811
    out = data(call(hub, "write", "history_note", {**NOTE, **args}))
    assert out["source"]["kind"] == kind and out["show"] == show
    assert set(out) == {"note_id", "created_at", "source", "show", "status", "redacted"}
    index_tenant(hub)
    (hit,) = recall_only(hub)["results"]
    assert hit["source"]["kind"] == kind and hit["show"] == show
    assert "link" not in hit


@pytest.mark.parametrize("args", [
    {"session_id": "sess-1"},                                 # no machine
    {"session_id": SID},                                      # a hosted id with no provider
    {"chat_url": LINK},                                       # a hosted link in the old field
    {"duet_channel_id": "chan_7Hq2"},
    {},
])
def test_the_old_refusals_still_hold_and_a_hosted_agent_is_told_what_to_pass(hub, args):  # noqa: F811
    result = call(hub, "write", "history_note", {**NOTE, **args})
    assert result.is_error
    assert not note_files(hub)
    if args.get("session_id") or args.get("chat_url"):
        assert "provider" in text_of(result) and "session_url" in text_of(result)


def test_history_record_old_kinds_are_unchanged(hub):  # noqa: F811
    bad_url = data(call(hub, "write", "history_record", {**NOTE, "chat_url": LINK}))
    # A chat_url that isn't a chat link still falls back to the record's own session.
    index_tenant(hub)
    (hit,) = recall_only(hub)["results"]
    assert hit["source"]["kind"] == "record"
    assert bad_url["session"].startswith("claude-ai-chat-rec-")


def test_default_recall_leaves_hosted_notes_out(hub):  # noqa: F811
    call(hub, "write", "history_note", {**NOTE, **HOSTED})
    index_tenant(hub)
    default = data(call(hub, "read", "history_recall", {"query": "zebra import", "mode": "text"}))
    assert not any("zebra" in r["excerpt"] for r in default["results"])
