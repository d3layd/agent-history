"""A parent reads a Boat VM's transcript back from the hub after boat-export."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from agent_history import boat_export, jobs  # noqa: E402
from jobfixtures import GH, JOB, make_job  # noqa: E402
from test_boat_export import ROWS, VM, VSID, FakeBoat  # noqa: E402
from test_jobs_readback import deliver_and_index, hub  # noqa: E402,F401
from test_mcp import data, run_calls  # noqa: E402


def test_parent_reads_a_vm_back_by_job_id_and_vm_id(hub, tmp_path):  # noqa: F811
    hub["monkeypatch"].setattr(jobs, "RETRY_DELAYS", (0, 0))
    make_job(hub["jobs"], hub["projects"])
    boat = FakeBoat(tmp_path / "homes")
    boat.add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=boat) == 0
    deliver_and_index(hub)

    async def calls(client):
        by_job = await client.call_tool("history_recall", {
            "query": JOB, "mode": "text", "cwd": JOB, "k": 10})
        by_vm = await client.call_tool("history_recall", {"query": VM, "mode": "text"})
        shown = await client.call_tool("history_show", {"session": f"{JOB}/vm/{VM}"})
        parent = await client.call_tool("history_show", {"session": JOB, "limit": 50})
        return data(by_job), data(by_vm), data(shown), data(parent)

    by_job, by_vm, shown, parent = run_calls(hub["token"], calls)
    sessions = {h["session_id"] for h in by_job["results"]}
    assert {JOB, f"{JOB}/vm/{VM}"} <= sessions
    assert by_vm["results"][0]["session_id"] == f"{JOB}/vm/{VM}"
    texts = [m["text"] for m in shown["messages"]]
    assert texts[0].startswith(f"[claude-jobs vm] {JOB} vm {VM}")
    assert f"Claude session: {VSID}" in texts[0]
    assert "Audit the signup form for accessibility issues." in texts
    assert GH not in json.dumps(shown)
    # the parent job's own conversation does not absorb the VM's messages
    assert "Audit the signup form for accessibility issues." not in \
        [m["text"] for m in parent["messages"]]
