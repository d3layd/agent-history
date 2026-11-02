"""A parent agent reads a claude -p job back through the hub's MCP tools.

End to end on a temporary hub: `ship-jobs` → the inbox directory the
upload-only key is pinned to → the hub indexer → `history_recall` (text
mode, by job id) and `history_show` over the real Streamable HTTP app.
The hub code is unchanged by this feature, so this is also the proof that
the live hub can read jobs without a redeploy.
"""
from __future__ import annotations

import json
import shutil

import pytest

pytest.importorskip("mcp")

from agent_history import config, index, jobs, mcp_auth, ship, store  # noqa: E402
from jobfixtures import GH, JOB, SID, landing_upload, make_job, stream  # noqa: E402
from test_mcp import data, run_calls  # noqa: E402

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


@pytest.fixture
def hub(tmp_path, monkeypatch, fake_embed):
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    landing = tmp_path / "landing"
    landing.mkdir()
    inbox = tmp_path / "inbox"
    (inbox / "dennis" / "claude-jobs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("AGENT_HISTORY_JOBS_DIR", str(jobs_root))
    monkeypatch.setenv("AGENT_HISTORY_MACHINE", "claude-jobs")
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@example.invalid:")
    monkeypatch.setattr(ship, "upload", landing_upload(landing))
    token = mcp_auth.add_token("router-read", ["read"], "dennis")
    return {"jobs": jobs_root, "projects": tmp_path / "projects",
            "landing": landing, "inbox": inbox, "token": token,
            "monkeypatch": monkeypatch}


def deliver_and_index(hub):
    """What rrsync and the hub's index unit do with the shipped batches."""
    target = hub["inbox"] / "dennis" / "claude-jobs"
    for batch in hub["landing"].glob("*.jsonl"):
        shutil.move(str(batch), target / batch.name)
    mp = hub["monkeypatch"]
    with mp.context() as m:
        m.setenv("AGENT_HISTORY_HUB", "1")
        m.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"] / "dennis"))
        m.setenv("AGENT_HISTORY_MACHINE", "hub")
        with config.tenant_home("dennis"):
            assert index.run() == 0
    assert not list(target.glob("*.jsonl")), "the indexer retires what it read"


def test_parent_reads_a_job_back_by_job_id(hub):
    make_job(hub["jobs"], hub["projects"],
             meta={"ask": f"Fix the login redirect loop; token {GH} is in .env"})
    assert jobs.run(JOB) == jobs.EXIT_OK
    deliver_and_index(hub)

    async def calls(client):
        found = await client.call_tool("history_recall", {
            "query": JOB, "mode": "text", "cwd": JOB, "k": 5})
        by_machine = await client.call_tool("history_recall", {
            "query": JOB, "mode": "text", "machine": "claude-jobs"})
        by_claude_session = await client.call_tool("history_recall", {
            "query": SID, "mode": "text"})
        shown = await client.call_tool("history_show", {"session": JOB, "limit": 50})
        return data(found), data(by_machine), data(by_claude_session), data(shown)

    found, by_machine, by_sid, shown = run_calls(hub["token"], calls)

    assert found["count"] >= 2   # the header and the seal name the job
    for hit in found["results"]:
        assert hit["session_id"] == JOB
        assert hit["machine"] == "claude-jobs"
        assert JOB in hit["project"]
    assert any(h["excerpt"].startswith(f"[claude-jobs job] {JOB}")
               for h in found["results"])
    assert any("Studio task: ATR-31" in h["excerpt"] for h in found["results"])
    assert by_machine["count"] == found["count"]
    assert by_sid["results"][0]["session_id"] == JOB

    assert shown["session_id"] == JOB and shown["machine"] == "claude-jobs"
    texts = [m["text"] for m in shown["messages"]]
    roles = [m["role"] for m in shown["messages"]]
    assert "Found it: the middleware redirects before the cookie is set." in texts
    assert any(t.startswith("[claude -p result] success") for t in texts)
    assert any(t.startswith(f"[claude-jobs final] {JOB} complete") for t in texts)
    assert {"job", "user", "assistant", "result"} <= set(roles)
    # the same message from the session file and transcript.jsonl is stored once
    assert texts.count("Found it: the middleware redirects before the cookie is set.") == 1
    assert GH not in json.dumps([found, shown])


def test_a_resumed_job_reads_back_as_one_conversation(hub):
    path = make_job(hub["jobs"], hub["projects"])
    assert jobs.run() == 0
    deliver_and_index(hub)
    with (path / "transcript.jsonl").open("a") as fh:
        fh.write(json.dumps(stream("assistant", "Resumed: CI is green on the fix.")) + "\n")
    assert jobs.run(JOB) == 0
    deliver_and_index(hub)

    async def calls(client):
        return data(await client.call_tool("history_show", {"session": JOB, "limit": 50}))

    shown = run_calls(hub["token"], calls)
    texts = [m["text"] for m in shown["messages"]]
    assert "Resumed: CI is green on the fix." in texts
    assert "Found it: the middleware redirects before the cookie is set." in texts
