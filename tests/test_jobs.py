"""`ship-jobs`: claude -p job transcripts out, tagged with the job, confirmed.

The transport is a copy into a local directory: no test runs rsync or opens
a connection, and nothing touches a real hub.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_history import cli, config, jobs, ship, trigger
from jobfixtures import (GH, JOB, SID, landing_upload, make_job, session_line,
                         shipped_lines, stream)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    root.mkdir()
    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_JOBS_DIR", str(root))
    monkeypatch.setenv("AGENT_HISTORY_MACHINE", "claude-jobs")
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@example.invalid:")
    monkeypatch.setattr(ship, "upload", landing_upload(landing))
    monkeypatch.setattr(jobs, "RETRY_DELAYS", (0, 0))
    return {"root": root, "projects": tmp_path / "projects", "landing": landing}


def job(env, **kw):
    return make_job(env["root"], env["projects"], **kw)


# ------------------------------------------------------------- the records

def test_every_record_carries_the_job_in_path_and_header(env):
    job(env)
    assert jobs.run() == 0
    files = sorted(env["landing"].glob("*.jsonl"))
    assert files and all(f.name.startswith(JOB + "-") for f in files)
    lines = shipped_lines(env["landing"])
    assert lines
    for line in lines:
        assert line["v"] == 1 and line["machine"] == "claude-jobs"
        assert line["session_id"] == JOB
        assert JOB in line["cwd"]
        assert line["job"] == {"job_id": JOB, "duet_channel_id": "chan_7Hq2",
                               "studio_task_key": "ATR-31", "kind": "job"}


def test_the_header_record_makes_the_job_findable_by_text(env):
    job(env)
    jobs.run()
    (header,) = [l for l in shipped_lines(env["landing"]) if l["role"] == "job"]
    text = header["text"]
    assert text.startswith(f"[claude-jobs job] {JOB}")
    for expected in ("Duet channel: chan_7Hq2", "Studio task: ATR-31",
                     "Repo: lvtd-dgtl/storm-check", f"Claude session: {SID}",
                     "Status: running", "Fix the login redirect loop on staging"):
        assert expected in text
    assert header["timestamp"] == "2026-11-01T12:00:00.000Z"


def test_session_file_and_transcript_both_ship(env):
    job(env, subagent=[session_line("assistant", "Side chain: checked the cookie flags.",
                                    "2026-11-01T12:02:00.000Z")])
    jobs.run()
    lines = shipped_lines(env["landing"])
    origins = {Path(l["origin"]).name for l in lines}
    assert {"transcript.jsonl", f"{SID}.jsonl", "agent-1.jsonl", "job.json"} <= origins
    (result,) = [l for l in lines if l["role"] == "result"]
    assert result["text"].startswith("[claude -p result] success turns=7")
    assert "Opened draft PR #12" in result["text"]
    # the result sorts after the session's last real message
    assert result["timestamp"] == "2026-11-01T12:03:00.000Z"
    synthetic = [l["timestamp"] for l in lines
                 if l["origin"].endswith("transcript.jsonl") and l["role"] != "result"]
    assert synthetic == sorted(synthetic) and synthetic[0] == "2026-11-01T12:00:00.002Z"


def test_shipping_is_idempotent_and_picks_up_growth(env):
    path = job(env)
    jobs.run()
    first = len(shipped_lines(env["landing"]))
    jobs.run()
    assert len(shipped_lines(env["landing"])) == first
    with (path / "transcript.jsonl").open("a") as fh:
        fh.write(json.dumps(stream("assistant", "One more finding after a resume.")) + "\n")
    jobs.run()
    assert len(shipped_lines(env["landing"])) == first + 1


def test_a_status_change_adds_a_new_header_record(env):
    path = job(env)
    jobs.run()
    meta = json.loads((path / "job.json").read_text())
    meta["status"] = "done"
    (path / "job.json").write_text(json.dumps(meta))
    jobs.run()
    headers = [l["text"] for l in shipped_lines(env["landing"]) if l["role"] == "job"]
    assert len(headers) == 2
    assert sorted(h.split("\n")[1] for h in headers) == ["Status: done", "Status: running"]


def test_secrets_are_redacted_everywhere(env):
    job(env, meta={"ask": f"deploy with token {GH} please",
                   "duet_channel_id": f"chan {GH}"},
        transcript=[stream("init"), stream("assistant", f"I used {GH} to push the branch")])
    jobs.run()
    blob = json.dumps(shipped_lines(env["landing"]))
    assert GH not in blob
    assert "[REDACTED:github_token]" in blob
    assert not list(config.outbox_dir().glob("*.jsonl"))


def test_cwd_falls_back_to_the_job_dir_when_the_worktree_lacks_the_id(env):
    job(env, transcript=[stream("init", cwd="/srv/repo"), stream("user", "a prompt for the job")],
        session=[session_line("user", "a prompt for the job", "2026-11-01T12:00:01Z",
                              cwd="/srv/repo")])
    jobs.run()
    cwds = {l["cwd"] for l in shipped_lines(env["landing"])}
    assert cwds == {str(env["root"] / JOB)}
    (header,) = [l for l in shipped_lines(env["landing"]) if l["role"] == "job"]
    assert "Worktree: /srv/repo" in header["text"]


def test_session_files_of_no_job_are_not_shipped(env):
    job(env)
    other = env["projects"] / "-home-claude-jobs"
    other.mkdir(parents=True)
    (other / "unrelated.jsonl").write_text(json.dumps(session_line(
        "user", "a daily auth check, not a job", "2026-11-01T09:00:00Z",
        sid="unrelated")) + "\n")
    jobs.run()
    assert "daily auth check" not in json.dumps(shipped_lines(env["landing"]))


def test_bad_job_dirs_are_skipped_not_fatal(env):
    job(env)
    (env["root"] / "cj_20261101T130000Z_ffffff").mkdir()          # no job.json
    bad = env["root"] / "cj_20261101T140000Z_eeeeee"
    bad.mkdir()
    (bad / "job.json").write_text(json.dumps({"job_id": "cj_other"}))
    (env["root"] / "not-a-job").mkdir()
    assert jobs.run() == 0
    assert {l["session_id"] for l in shipped_lines(env["landing"])} == {JOB}


def test_dry_run_writes_and_sends_nothing(env, capsys):
    job(env)
    assert jobs.run(dry_run=True) == 0
    assert "would ship" in capsys.readouterr().out
    assert not list(env["landing"].iterdir())
    assert not config.ship_state_path().exists()


def test_no_destination_is_a_config_error(env, monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_HUB_INGEST")
    job(env)
    assert jobs.run() == jobs.EXIT_CONFIG
    assert jobs.run(JOB) == jobs.EXIT_CONFIG


# ------------------------------------------------------------ ship-final

def test_final_confirms_and_adds_a_seal(env, capsys):
    job(env)
    assert jobs.run(JOB) == jobs.EXIT_OK
    assert "confirmed" in capsys.readouterr().out
    (seal,) = [l for l in shipped_lines(env["landing"])
               if l["text"].startswith("[claude-jobs final]")]
    assert f"[claude-jobs final] {JOB} complete" in seal["text"]
    assert "Transcript result: success" in seal["text"]
    assert "Records: 6 (session files 2, transcript.jsonl 3, header 1)" in seal["text"]
    state = ship.State.open(writable=False)
    try:
        assert not state.db.execute("SELECT COUNT(*) FROM queued").fetchone()[0]
    finally:
        state.close()


def test_final_is_idempotent(env):
    job(env)
    assert jobs.run(JOB) == 0
    count = len(shipped_lines(env["landing"]))
    assert jobs.run(JOB) == 0
    assert len(shipped_lines(env["landing"])) == count


def test_final_after_periodic_ships_only_the_rest(env):
    path = job(env)
    jobs.run()
    before = len(shipped_lines(env["landing"]))
    with (path / "transcript.jsonl").open("a") as fh:
        fh.write(json.dumps(stream("assistant", "Late message before the job ends.")) + "\n")
    assert jobs.run(JOB) == 0
    assert len(shipped_lines(env["landing"])) == before + 2  # the message and the seal


def test_final_reports_a_missing_result_line(env):
    job(env, transcript=[stream("init"), stream("user", "the job died before a result")])
    assert jobs.run(JOB) == 0
    (seal,) = [l for l in shipped_lines(env["landing"])
               if l["text"].startswith("[claude-jobs final]")]
    assert "Transcript result: missing" in seal["text"]


def test_final_fails_and_keeps_batches_when_the_hub_is_down(env, monkeypatch, capsys):
    job(env)
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    assert jobs.run(JOB) == jobs.EXIT_UNCONFIRMED
    assert "NOT confirmed" in capsys.readouterr().err
    assert list(config.outbox_dir().glob(f"{JOB}-*.jsonl"))
    monkeypatch.setattr(ship, "upload", landing_upload(env["landing"]))
    assert jobs.run(JOB) == jobs.EXIT_OK
    assert not list(config.outbox_dir().glob("*.jsonl"))


def test_final_retries_a_transient_failure(env, monkeypatch):
    job(env)
    real = landing_upload(env["landing"])
    calls = []

    def flaky(files, dest, key):
        calls.append(len(files))
        return real(files, dest, key) if len(calls) > 1 else False

    monkeypatch.setattr(ship, "upload", flaky)
    assert jobs.run(JOB) == jobs.EXIT_OK
    assert len(calls) == 2


def test_final_counts_a_transport_exception_as_unconfirmed(env, monkeypatch):
    job(env)

    def boom(files, dest, key):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(ship, "upload", boom)
    assert jobs.run(JOB) == jobs.EXIT_UNCONFIRMED


def test_final_never_reports_success_for_a_partial_delivery(env, monkeypatch):
    """rsync reported success but one batch is still in the outbox."""
    job(env)
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: True)
    monkeypatch.setattr(jobs, "_upload", lambda outbox, pattern, dest, attempts: True)
    assert jobs.run(JOB) == jobs.EXIT_UNCONFIRMED


@pytest.mark.parametrize("bad", ["cj_bad", "../etc", "cj_20261101T120000Z_A1B2C3", ""])
def test_final_rejects_a_malformed_job_id(env, bad):
    assert jobs.run(bad) == jobs.EXIT_CONFIG


def test_final_rejects_a_missing_or_mismatched_job(env):
    assert jobs.run(JOB) == jobs.EXIT_CONFIG
    job(env, meta={"job_id": "cj_20261101T120000Z_000000"})
    assert jobs.run(JOB) == jobs.EXIT_CONFIG


def test_final_waits_for_the_lock_and_gives_up_with_exit_3(env):
    job(env)
    config.ensure_data_home()
    with trigger._exclusive(config.ship_lock_path()) as held:
        assert held
        assert jobs.run(JOB, wait=1) == jobs.EXIT_BUSY
    assert jobs.run(JOB, wait=1) == jobs.EXIT_OK


def test_cli_ship_jobs(env, capsys):
    job(env)
    assert cli.main(["ship-jobs", "--dry-run"]) == 0
    assert cli.main(["ship-jobs", "--final", JOB, "--wait", "5"]) == 0
    assert "confirmed" in capsys.readouterr().out


# -------------------------------------------------- the ship-final wrapper

WRAPPER = REPO / "deploy" / "claude-jobs" / "ship-final"


@pytest.mark.skipif(sys.platform == "win32", reason="a bash wrapper for the fleet")
def test_wrapper_validates_and_passes_the_exit_code_through(tmp_path):
    stub = tmp_path / "agent-history"
    stub.write_text("#!/bin/sh\necho \"$AGENT_HISTORY_HUB_INGEST $*\"\nexit 7\n")
    stub.chmod(0o755)
    envfile = tmp_path / "env"
    envfile.write_text("AGENT_HISTORY_HUB_INGEST=ahub@example.invalid:\n"
                       "# a comment\nNOT_OURS=ignored\n")
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
            "AGENT_HISTORY_BIN": str(stub), "AGENT_HISTORY_ENV_FILE": str(envfile)}

    bad = subprocess.run(["bash", str(WRAPPER), "cj_nope"], env=base,
                         capture_output=True, text=True)
    assert bad.returncode == 2

    good = subprocess.run(["bash", str(WRAPPER), JOB], env=base,
                          capture_output=True, text=True)
    assert good.returncode == 7
    assert good.stdout.split() == ["ahub@example.invalid:", "ship-jobs", "--final",
                                   JOB, "--wait", "300"]

    missing = subprocess.run(["bash", str(WRAPPER), JOB],
                             env={**base, "AGENT_HISTORY_ENV_FILE": str(tmp_path / "x")},
                             capture_output=True, text=True)
    assert missing.returncode == 2
