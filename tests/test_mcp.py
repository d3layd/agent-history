"""`agent-history mcp`: tools over the MCP protocol, auth, tenancy, caps.

Every protocol test goes through the SDK's own client against the real ASGI
app (bearer front door included) in-process: no sockets, no Ollama, and no
index outside tmp_path.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

import anyio
import pytest

pytest.importorskip("mcp")

import httpx2  # noqa: E402  (a dependency of the mcp SDK)
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from agent_history import (cli, config, embed, index, mcp_auth,  # noqa: E402
                           mcp_server, store)

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])
BASE = "http://127.0.0.1:8765"


def put(db, text, *, session, machine="pc", cwd="/work/alpha",
        timestamp="2026-01-01T00:00:00Z", role="user", agent="claude-code"):
    store.insert_chunk(
        db, agent=agent, origin=f"/home/someone/{session}.jsonl",
        session_id=session, cwd=cwd, timestamp=timestamp, role=role, text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text), machine=machine)


@pytest.fixture
def hub(tmp_path, monkeypatch, fake_embed):
    """Two tenants with their own index, an inbox per tenant, and tokens."""
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    inbox = tmp_path / "inbox"
    for tenant in ("dennis", "eagle"):
        (inbox / tenant).mkdir(parents=True)
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(inbox))
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(tmp_path / "tokens.json"))

    with config.tenant_home("dennis"):
        db, _ = store.open_for_write()
        put(db, "dennis vault rotation plan, decided to rotate quarterly",
            session="sess-dennis")
        put(db, "a follow-up on the vault rotation plan " + "x" * 3000,
            session="sess-dennis", role="assistant",
            timestamp="2026-01-01T00:01:00Z")
        for n in range(30):
            put(db, f"filler chunk number {n} about deployment pipelines",
                session=f"sess-fill-{n}")
        db.commit()
        db.close()
    with config.tenant_home("eagle"):
        db, _ = store.open_for_write()
        put(db, "eagle client onboarding plan for lexi", session="sess-eagle")
        db.commit()
        db.close()

    tokens = {
        "read": mcp_auth.add_token("dennis-read", ["read"], "dennis"),
        "write": mcp_auth.add_token("dennis-write", ["write"], "dennis",
                                    machine="claude-ai"),
        "eagle": mcp_auth.add_token("eagle-read", ["read"], "eagle"),
    }
    return {"inbox": inbox, "tokens": tokens, "tmp": tmp_path}


def app_for(limiter=None):
    gate = mcp_server.Gate(mcp_auth.TokenStore(), limiter=limiter)
    return mcp_server.http_app(gate)


def session(app, token, *, mode="legacy"):
    """An SDK client over Streamable HTTP into ``app``. 'legacy' is the
    initialize handshake the MCP router (TypeScript SDK) performs."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                              base_url=BASE, headers=headers)
    return http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http),
                        mode=mode)


def run_calls(token, calls, *, app=None, mode="legacy"):
    """Open one client, run ``calls(client)``, return its result."""
    app = app or app_for()
    out = {}

    async def main():
        async with app.server.session_manager.run():
            http, client = session(app, token, mode=mode)
            async with http, client:
                out["value"] = await calls(client)

    anyio.run(main)
    return out["value"]


def text_of(result):
    return result.content[0].text


def data(result):
    assert not result.is_error, text_of(result)
    return json.loads(text_of(result))


# ------------------------------------------------------------------ listing

def test_tool_listing_follows_the_token_scope(hub):
    async def names(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    read = run_calls(hub["tokens"]["read"], names)
    write = run_calls(hub["tokens"]["write"], names)
    assert set(read) == {"history_recall", "history_show"}
    assert set(write) == {"history_record", "history_note"}
    assert read["history_recall"].annotations.read_only_hint is True
    assert write["history_record"].annotations.read_only_hint is False
    assert write["history_record"].annotations.destructive_hint is False
    schema = read["history_recall"].input_schema
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == {"query", "k", "agent", "surface", "cwd",
                                         "machine", "mode", "sort", "notes"}
    assert schema["properties"]["notes"]["default"] == "exclude"
    assert write["history_note"].annotations.destructive_hint is False


def test_no_tool_can_change_or_delete_history(hub):
    combined = mcp_auth.add_token("both", ["read", "write"], "dennis")

    async def names(client):
        return {t.name for t in (await client.list_tools()).tools}

    assert run_calls(combined, names) == {"history_recall", "history_show",
                                          "history_record", "history_note"}


def test_modern_protocol_clients_work_too(hub):
    async def names(client):
        return {t.name for t in (await client.list_tools()).tools}

    assert run_calls(hub["tokens"]["read"], names, mode="auto") == {
        "history_recall", "history_show"}


# ------------------------------------------------------------------- recall

def test_recall_returns_bounded_hits_without_paths(hub):
    async def call(client):
        return await client.call_tool("history_recall",
                                      {"query": "vault rotation plan", "mode": "text"})

    out = data(run_calls(hub["tokens"]["read"], call))
    assert out["count"] >= 1
    hit = out["results"][0]
    assert set(hit) == {"rank", "timestamp", "agent", "machine", "project", "session_id",
                        "session", "role", "score", "distance", "excerpt"}
    assert hit["session_id"] == "sess-dennis" and hit["machine"] == "pc"
    assert hit["project"] == "/work/alpha"
    assert all(len(h["excerpt"]) <= mcp_server.EXCERPT_CHARS + 2
               for h in out["results"])
    # The origin (a file path on the machine that shipped it) is never sent.
    assert "/home/someone" not in json.dumps(out)


def test_recall_hybrid_uses_the_embedder(hub, fake_embed):
    async def call(client):
        return await client.call_tool("history_recall", {"query": "vault rotation"})

    out = data(run_calls(hub["tokens"]["read"], call))
    assert out["mode"] == "hybrid" and out["count"] >= 1
    assert "vault rotation" in fake_embed


def test_recall_caps_k(hub):
    async def call(client):
        return await client.call_tool("history_recall", {
            "query": "filler chunk deployment pipelines", "k": 500, "mode": "text"})

    out = data(run_calls(hub["tokens"]["read"], call))
    assert out["count"] == mcp_server.MAX_K


def test_recall_rejects_an_oversized_query(hub):
    async def call(client):
        return await client.call_tool("history_recall", {"query": "q" * 5000})

    result = run_calls(hub["tokens"]["read"], call)
    assert result.is_error and "longer than" in text_of(result)


def test_recall_filters_reach_search(hub):
    async def call(client):
        return await client.call_tool("history_recall", {
            "query": "vault rotation plan", "mode": "text", "surface": "codex"})

    assert data(run_calls(hub["tokens"]["read"], call))["count"] == 0


# --------------------------------------------------------------------- show

def test_show_reads_around_a_hit_and_caps_text(hub):
    async def call(client):
        return await client.call_tool("history_show", {"session": "sess-dennis"})

    out = data(run_calls(hub["tokens"]["read"], call))
    assert out["session_id"] == "sess-dennis"
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
    assert all(len(m["text"]) <= mcp_server.SHOW_TEXT_CHARS + 2
               for m in out["messages"])
    assert "/home/someone" not in json.dumps(out)


def test_show_limit_is_capped(hub):
    with config.tenant_home("dennis"):
        db = store.open_for_maintenance()
        for n in range(80):
            put(db, f"long session line {n}", session="sess-long",
                timestamp=f"2026-02-01T00:{n // 60:02d}:{n % 60:02d}Z")
        db.commit()
        db.close()

    async def call(client):
        return await client.call_tool("history_show",
                                      {"session": "sess-long", "limit": 1000})

    out = data(run_calls(hub["tokens"]["read"], call))
    assert len(out["messages"]) == mcp_server.MAX_SHOW_LIMIT


def test_show_unknown_session_is_an_error(hub):
    async def call(client):
        return await client.call_tool("history_show", {"session": "nope"})

    assert run_calls(hub["tokens"]["read"], call).is_error


def test_show_resolves_aliases_when_the_store_supports_it(hub, monkeypatch):
    monkeypatch.setattr(store, "resolve_session",
                        lambda db, any_id: "sess-dennis" if any_id == "alias-1" else None,
                        raising=False)

    async def call(client):
        return await client.call_tool("history_show", {"session": "alias-1"})

    out = data(run_calls(hub["tokens"]["read"], call))
    assert out["messages"]


def test_session_uid_is_preferred_when_present():
    assert mcp_server._session_of({"session_uid": "u1", "session_id": "s1"}) == "u1"
    assert mcp_server._session_of({"session_id": "s1"}) == "s1"


# ------------------------------------------------------------------- record

RECORD = {
    "title": "Decided the hub MCP design",
    "summary": f"Agreed on scoped tokens. Pasted by mistake: {GH}",
    "decisions": ["two tokens: read and write", "loopback only"],
    "findings": ["the router holds one header per upstream"],
    "links": ["https://github.com/d3layd/agent-history/issues/28"],
    "chat_url": "https://claude.ai/chat/0b5e1d4c-1111-4222-8333-444455556666",
    "project": "agent-history",
}


def batches(hub, machine="claude-ai"):
    return sorted((hub["inbox"] / "dennis" / machine).glob("*.jsonl"))


def test_record_is_redacted_and_lands_in_the_inbox(hub):
    async def call(client):
        return await client.call_tool("history_record", RECORD)

    out = data(run_calls(hub["tokens"]["write"], call))
    assert out["record_id"].startswith("rec-")
    assert out["session"] == "claude-ai-chat-0b5e1d4c-1111-4222-8333-444455556666"
    assert out["redacted"] == {"github_token": 1}

    (path,) = batches(hub)
    if os.name != "nt":  # Windows has no POSIX mode bits
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert GH not in raw and "[REDACTED:github_token]" in raw
    line = json.loads(raw)
    assert line["v"] == 1 and line["agent"] == "claude-ai-chat"
    assert line["machine"] == "claude-ai" and line["cwd"] == "agent-history"
    assert line["session_id"] == out["session"]
    assert out["record_id"] in line["text"] and "two tokens" in line["text"]


def test_record_is_append_only(hub):
    async def call(client):
        first = await client.call_tool("history_record", RECORD)
        second = await client.call_tool("history_record", RECORD)
        return data(first), data(second)

    first, second = run_calls(hub["tokens"]["write"], call)
    assert first["record_id"] != second["record_id"]
    assert first["session"] == second["session"]  # one chat, one session
    paths = batches(hub)
    assert len(paths) == 2
    before = {p: p.read_bytes() for p in paths}

    async def third(client):
        return await client.call_tool("history_record", {**RECORD, "title": "later"})

    data(run_calls(hub["tokens"]["write"], third))
    assert all(p.read_bytes() == b for p, b in before.items())
    assert len(batches(hub)) == 3


def test_recorded_chats_are_indexed_like_shipped_records(hub, monkeypatch):
    """The record takes the same inbox → index path as `ship`, then is
    found by history_recall and readable with history_show."""
    async def write(client):
        return await client.call_tool("history_record", RECORD)

    written = data(run_calls(hub["tokens"]["write"], write))

    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"] / "dennis"))
    with config.tenant_home("dennis"):
        assert index.run() == 0
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"]))
    assert not batches(hub), "the indexer retires consumed batches"

    async def read(client):
        found = await client.call_tool("history_recall", {
            "query": "hub MCP design scoped tokens", "mode": "text",
            "surface": "claude-ai-chat"})
        shown = await client.call_tool("history_show",
                                       {"session": written["session"]})
        return data(found), data(shown)

    found, shown = run_calls(hub["tokens"]["read"], read)
    assert found["count"] >= 1
    hit = found["results"][0]
    assert hit["agent"] == "claude-ai-chat" and hit["machine"] == "claude-ai"
    assert hit["session_id"] == written["session"]
    assert GH not in json.dumps(found)
    assert shown["messages"][0]["role"] == "summary"


def test_record_validates_its_input(hub):
    async def call(client):
        return [
            await client.call_tool("history_record", {"title": "", "summary": "s"}),
            await client.call_tool("history_record", {"title": "t" * 500, "summary": "s"}),
            await client.call_tool("history_record", {
                "title": "t", "summary": "s", "decisions": ["d"] * 100}),
            await client.call_tool("history_record", {
                "title": "t", "summary": "s", "chat_url": "javascript:alert(1)"}),
        ]

    assert all(r.is_error for r in run_calls(hub["tokens"]["write"], call))
    assert not batches(hub)


def test_record_needs_the_tenant_inbox_to_exist(hub):
    (hub["inbox"] / "dennis").rename(hub["inbox"] / "elsewhere")

    async def call(client):
        return await client.call_tool("history_record", RECORD)

    result = run_calls(hub["tokens"]["write"], call)
    assert result.is_error and "inbox" in text_of(result)
    assert not (hub["inbox"] / "dennis").exists(), "a client must not create it"


# --------------------------------------------------------------------- auth

def post(app, headers):
    async def main():
        async with app.server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                          base_url=BASE) as http:
                return await http.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    return anyio.run(main)


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"},
    {"Authorization": "Basic dXNlcjpwYXNz"},
    {"Authorization": "Bearer"},
])
def test_missing_or_wrong_tokens_are_rejected_before_mcp(hub, headers):
    response = post(app_for(), headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_healthz_needs_no_token_and_says_nothing(hub):
    async def main():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app_for()),
                                      base_url=BASE) as http:
            return await http.get("/healthz")

    response = anyio.run(main)
    assert response.status_code == 200 and response.text == "ok\n"


def test_read_token_cannot_record(hub):
    async def call(client):
        return await client.call_tool("history_record", RECORD)

    result = run_calls(hub["tokens"]["read"], call)
    assert result.is_error and "may not call" in text_of(result)
    assert not batches(hub)


def test_write_token_cannot_read(hub):
    async def call(client):
        return [await client.call_tool("history_recall", {"query": "vault"}),
                await client.call_tool("history_show", {"session": "sess-dennis"})]

    results = run_calls(hub["tokens"]["write"], call)
    assert all(r.is_error and "may not call" in text_of(r) for r in results)
    assert all("vault" not in text_of(r) for r in results)


def test_revoked_token_stops_working_without_a_restart(hub):
    app = app_for()
    mcp_auth.revoke_token("dennis-read")
    assert post(app, {"Authorization": f"Bearer {hub['tokens']['read']}"}).status_code == 401


# ------------------------------------------------------------------ tenancy

def test_tenants_are_isolated(hub):
    async def call(client):
        return [await client.call_tool("history_recall",
                                       {"query": "vault rotation", "mode": "text"}),
                await client.call_tool("history_show", {"session": "sess-dennis"}),
                await client.call_tool("history_recall",
                                       {"query": "onboarding plan", "mode": "text"})]

    dennis_hit, dennis_show, own = run_calls(hub["tokens"]["eagle"], call)
    assert data(dennis_hit)["count"] == 0
    assert dennis_show.is_error
    assert data(own)["results"][0]["session_id"] == "sess-eagle"


def test_a_tenant_cannot_be_chosen_by_the_request(hub):
    async def call(client):
        return await client.call_tool("history_recall", {
            "query": "vault rotation", "mode": "text", "tenant": "dennis"})

    result = run_calls(hub["tokens"]["eagle"], call)
    assert result.is_error or data(result)["count"] == 0


# ------------------------------------------------------------------- limits

def test_rate_limit(hub):
    limiter = mcp_auth.RateLimiter({"read": 2, "write": 1})

    async def call(client):
        return [await client.call_tool("history_recall", {"query": "x", "mode": "text"})
                for _ in range(3)]

    results = run_calls(hub["tokens"]["read"], call, app=app_for(limiter))
    assert [r.is_error for r in results] == [False, False, True]
    assert "rate limit" in text_of(results[2])


def test_rate_limiter_window_slides():
    now = [0.0]
    limiter = mcp_auth.RateLimiter({"read": 1}, clock=lambda: now[0])
    assert limiter.allow("a", "read") and not limiter.allow("a", "read")
    assert limiter.allow("b", "read"), "limits are per token"
    now[0] = 61
    assert limiter.allow("a", "read")


# ------------------------------------------------------------ token storage

def test_token_file_stores_only_hashes_and_is_private(hub):
    path = hub["tmp"] / "tokens.json"
    raw = path.read_text()
    if os.name != "nt":  # Windows has no POSIX mode bits
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(token not in raw for token in hub["tokens"].values())
    assert mcp_auth.list_tokens() and all("sha256" not in e
                                          for e in mcp_auth.list_tokens())


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_readable_token_file_is_refused(hub):
    (hub["tmp"] / "tokens.json").chmod(0o644)
    with pytest.raises(mcp_auth.TokenConfigError, match="0600"):
        mcp_auth.TokenStore()


def test_env_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(tmp_path / "none.json"))
    monkeypatch.setenv("AGENT_HISTORY_MCP_READ_TOKEN", "r" * 40)
    monkeypatch.setenv("AGENT_HISTORY_MCP_TENANT", "dennis")
    store_ = mcp_auth.TokenStore()
    who = store_.authenticate("r" * 40)
    assert who.scopes == {"read"} and who.tenant == "dennis"
    assert store_.authenticate("r" * 39) is None
    monkeypatch.setenv("AGENT_HISTORY_MCP_WRITE_TOKEN", "short")
    with pytest.raises(mcp_auth.TokenConfigError, match="shorter"):
        mcp_auth.TokenStore()


def test_bearer_parsing():
    assert mcp_auth.bearer("Bearer abc") == "abc"
    assert mcp_auth.bearer("bearer  abc ") == "abc"
    assert mcp_auth.bearer("Basic abc") is None
    assert mcp_auth.bearer("Bearer a b") is None
    assert mcp_auth.bearer(None) is None


# -------------------------------------------------------------- serve / cli

def test_http_refuses_a_public_bind(hub, capsys):
    assert mcp_server.serve(host="0.0.0.0", port=1) == 1
    assert "refusing to bind" in capsys.readouterr().err


def test_http_refuses_to_start_without_tokens(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(tmp_path / "none.json"))
    assert mcp_server.serve(port=1) == 1
    assert "no tokens" in capsys.readouterr().err


def test_stdio_needs_a_valid_token_from_the_environment(hub, monkeypatch, capsys):
    assert mcp_server.serve(transport="stdio") == 1
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKEN", "not-a-token")
    assert mcp_server.serve(transport="stdio") == 1
    assert "AGENT_HISTORY_MCP_TOKEN" in capsys.readouterr().err


def test_in_process_server_with_a_fixed_identity(hub):
    """stdio's model: one identity for the whole connection, still scoped."""
    from agent_history.mcp_app import build_server

    who = mcp_auth.TokenStore().authenticate(hub["tokens"]["read"])
    server = build_server(mcp_server.Gate(None, fixed=who))

    async def main():
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            denied = await client.call_tool("history_record", RECORD)
            found = await client.call_tool("history_recall",
                                           {"query": "vault rotation", "mode": "text"})
            return names, denied, found

    names, denied, found = anyio.run(main)
    assert names == {"history_recall", "history_show"}
    assert denied.is_error and data(found)["count"] >= 1


def test_cli_token_commands(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(tmp_path / "t" / "tokens.json"))
    assert cli.main(["mcp", "token", "add", "router-read", "--scope", "read",
                     "--tenant", "dennis"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith(mcp_auth.TOKEN_PREFIX)
    assert mcp_auth.TokenStore().authenticate(token).name == "router-read"
    assert cli.main(["mcp", "token", "add", "router-read", "--scope", "read"]) == 1
    assert cli.main(["mcp", "token", "list"]) == 0
    listed = capsys.readouterr().out
    assert "router-read" in listed and token not in listed
    assert cli.main(["mcp", "token", "revoke", "router-read"]) == 0
    assert mcp_auth.TokenStore().authenticate(token) is None
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "t").stat().st_mode) == 0o700


def test_cli_serve_options_parse_either_side():
    parser = cli._build_parser()
    args = parser.parse_args(["mcp", "--port", "9001", "serve"])
    assert args.port == 9001 and args.host == "127.0.0.1"
    args = parser.parse_args(["mcp", "serve", "--port", "9002", "--stdio"])
    assert args.port == 9002 and args.stdio
