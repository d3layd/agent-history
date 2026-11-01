"""`confirm-indexed`: the index gate, end to end on a temporary hub.

ship → the inbox → the hub indexer → confirm-indexed, read locally (as on
the hub) and through the read-only ssh path: ssh is faked by running the real
`serve-ssh` gate in-process with the forced command's tenant list, so the
check goes through exactly what Clamps' key allows (search and show).
"""
from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

pytest.importorskip("mcp")

from agent_history import cli, confirm, config, jobs, remote, serve_ssh, store  # noqa: E402
from jobfixtures import JOB, SID, make_job, stream  # noqa: E402
from test_jobs_readback import deliver_and_index, hub  # noqa: E402,F401
from test_mcp import data, run_calls  # noqa: E402

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def confirm_local(*args, **kw):
    with config.tenant_home("dennis"):
        return confirm.run(JOB, *args, local=True, **kw)


@pytest.fixture
def ssh_hub(hub, monkeypatch):  # noqa: F811
    """AGENT_HISTORY_REMOTE pointing at a fake ssh that runs serve-ssh with
    `--tenants dennis`, the way the hub's authorized_keys pins Clamps' key."""
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://ahub@hub.example.invalid")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = serve_ssh.run("dennis", command=argv[-1])
        return subprocess.CompletedProcess(argv, code, out.getvalue(), err.getvalue())

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    hub["ssh_calls"] = calls
    return hub


def test_a_shipped_and_indexed_job_is_confirmed_locally(hub, capsys):  # noqa: F811
    make_job(hub["jobs"], hub["projects"])
    assert jobs.run(JOB) == jobs.EXIT_OK
    deliver_and_index(hub)
    capsys.readouterr()
    assert confirm_local(as_json=True) == confirm.EXIT_INDEXED
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "indexed" and not result["missing"]
    assert result["sessions"] == [JOB] and result["claude_session_ids"] == [SID]
    assert result["checks"][f"recall {SID}"] is True
    assert result["claude_lookup"] == {SID: True}


def test_confirmed_through_the_read_only_ssh_key(ssh_hub, tmp_path, capsys):
    make_job(ssh_hub["jobs"], ssh_hub["projects"])
    assert jobs.run(JOB) == jobs.EXIT_OK
    deliver_and_index(ssh_hub)
    capsys.readouterr()
    receipt = tmp_path / "receipt.json"
    assert cli.main(["confirm-indexed", "--job", JOB, "--receipt", str(receipt)]) == 0
    assert "indexed (" in capsys.readouterr().out
    assert json.loads(receipt.read_text())["state"] == "indexed"
    commands = [argv[-1] for argv in ssh_hub["ssh_calls"]]
    assert commands and all(c.split()[0] in ("search", "show") for c in commands)


def test_not_yet_indexed_is_1(ssh_hub, capsys):
    make_job(ssh_hub["jobs"], ssh_hub["projects"])
    assert jobs.run(JOB) == jobs.EXIT_OK          # arrived, not indexed
    other = "cj_20261101T130000Z_bbbbbb"
    make_job(ssh_hub["jobs"], ssh_hub["projects"], job_id=other)
    jobs.run(other)
    for batch in ssh_hub["landing"].glob(f"{JOB}-*.jsonl"):
        batch.rename(batch.with_suffix(".held"))  # this job's batches not delivered yet
    deliver_and_index(ssh_hub)
    capsys.readouterr()
    assert confirm.run(JOB, as_json=True) == confirm.EXIT_PENDING
    out = capsys.readouterr()
    assert json.loads(out.out)["state"] == "pending"
    assert "pending" in out.err


def test_partial_is_1_and_names_what_is_missing(ssh_hub, capsys):
    path = make_job(ssh_hub["jobs"], ssh_hub["projects"])
    assert jobs.run() == 0                        # periodic ship: no seal yet
    deliver_and_index(ssh_hub)
    capsys.readouterr()
    assert confirm.run(JOB, as_json=True) == confirm.EXIT_PENDING
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "partial"
    assert result["checks"]["header"] and result["checks"][f"show {JOB}"]
    assert "seal" in result["missing"]

    with (path / "transcript.jsonl").open("a") as fh:
        fh.write(json.dumps(stream("assistant", "Wrapping up the job now.")) + "\n")
    assert jobs.run(JOB) == 0
    deliver_and_index(ssh_hub)
    assert confirm.run(JOB) == confirm.EXIT_INDEXED


def test_a_refused_key_is_a_credential_error(hub, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://ahub@hub.example.invalid")
    monkeypatch.setattr(remote.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 255, "", "ahub@hub.example.invalid: Permission denied (publickey).\n"))
    assert confirm.run(JOB) == confirm.EXIT_CONFIG
    assert "refused" in capsys.readouterr().err


def test_a_tenant_outside_the_key_is_a_config_error(ssh_hub):
    assert confirm.run(JOB, tenant="eagle") == confirm.EXIT_CONFIG


def test_an_unreachable_hub_is_not_yet(hub, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://ahub@hub.example.invalid")

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(remote.subprocess, "run", timeout)
    assert confirm.run(JOB) == confirm.EXIT_PENDING


def test_no_read_path_at_all_is_a_config_error(hub, capsys):  # noqa: F811
    """Even with a local index around (Clamps has one), no remote and no
    --local is a config error, not an endless "not yet"."""
    make_job(hub["jobs"], hub["projects"])
    jobs.run(JOB)
    deliver_and_index(hub)
    capsys.readouterr()
    with config.tenant_home("dennis"):
        assert confirm.run(JOB) == confirm.EXIT_CONFIG
    assert "no hub configured" in capsys.readouterr().err


def test_it_refuses_to_run_as_claude_jobs(hub, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(confirm.getpass, "getuser", lambda: "claude-jobs")
    assert confirm_local() == confirm.EXIT_CONFIG
    assert "upload-only" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["cj_bad", "", "../x"])
def test_a_bad_job_id_is_a_config_error(bad):
    assert confirm.run(bad) == confirm.EXIT_CONFIG


def test_lookup_by_the_claude_session_uuid(hub):  # noqa: F811
    make_job(hub["jobs"], hub["projects"])
    assert jobs.run(JOB) == jobs.EXIT_OK
    deliver_and_index(hub)

    async def calls(client):
        shown = await client.call_tool("history_show", {"session": SID})
        found = await client.call_tool("history_recall", {"query": SID, "mode": "text"})
        by_job = await client.call_tool("history_show", {"session": JOB})
        return data(shown), data(found), data(by_job)

    shown, found, by_job = run_calls(hub["token"], calls)
    assert shown["session_id"] == JOB
    assert shown["claude_session_ids"] == [SID]
    assert by_job["claude_session_ids"] == [SID]
    assert found["results"][0]["session_id"] == JOB
    assert found["results"][0]["claude_session_ids"] == [SID]
