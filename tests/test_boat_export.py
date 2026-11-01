"""`boat-export`: pull Boat VM transcripts, ship them, and never delete a VM.

Boat is faked: each VM is a state plus a directory standing in for its home.
No test calls the real Boat CLI or the network.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from agent_history import boat_export, cli, config, jobs, ship
from jobfixtures import GH, JOB, landing_upload, make_job, session_line, shipped_lines

VM = "bx_u6qgdzjq"
WAIT = boat_export.EXIT_AWAITING_INDEX
VM2 = "bx_second01"
VSID = "9a9a9a9a-0000-4000-8000-00000000bbbb"


class FakeBoat:
    """VMs with a state and a home directory. Records every call."""

    def __init__(self, homes: Path):
        self.homes = homes
        self.states: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.stop_timeout = 1
        self.fail: dict[str, str] = {}       # method -> message
        self.corrupt = False

    def add(self, vm: str, state: str = "stopped", rows=None) -> Path:
        self.states[vm] = state
        home = self.homes / vm
        if rows is not None:
            d = home / ".claude" / "projects" / "-home-user-storm-check"
            d.mkdir(parents=True)
            (d / f"{VSID}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows))
        else:
            home.mkdir(parents=True)
        return home

    def _call(self, name: str, vm: str):
        self.calls.append((name, vm))
        if name in self.fail:
            raise boat_export.BoatError(self.fail[name])

    def state(self, vm):
        self._call("state", vm)
        return self.states[vm]

    def resume(self, vm):
        self._call("resume", vm)
        self.states[vm] = "running"

    def stop(self, vm):
        self._call("stop", vm)
        self.states[vm] = "stopped"

    def wait_for(self, vm, wanted, timeout):
        self._call("wait_for", vm)
        self.states[vm] = wanted

    def checksums(self, vm):
        self._call("checksums", vm)
        assert self.states[vm] == "running", "only a running VM can be read"
        return boat_export.local_checksums(self.homes / vm / ".claude" / "projects")

    def pull(self, vm, dest):
        self._call("pull", vm)
        src = self.homes / vm / ".claude" / "projects"
        shutil.copytree(src, dest / "projects")
        if self.corrupt:
            for f in (dest / "projects").rglob("*.jsonl"):
                f.write_text(f.read_text() + "tampered\n")
        return dest / "projects"


ROWS = [
    session_line("user", "Audit the signup form for accessibility issues.",
                 "2026-11-01T23:00:01.000Z", sid=VSID, cwd="/home/user/storm-check"),
    session_line("assistant", f"Found 3 issues; also saw {GH} in .env, rotate it.",
                 "2026-11-01T23:01:30.000Z", sid=VSID, cwd="/home/user/storm-check"),
]


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
    make_job(root, tmp_path / "projects")
    return {"root": root, "landing": landing, "boat": FakeBoat(tmp_path / "homes"),
            "job": root / JOB}


def confirm_index(env, *vms, state="indexed", copies=None):
    """What the runner's side does after confirm-indexed --job JOB exits 0:
    its --json result lands in the job directory. ``copies`` defaults to the
    digest of each VM's current copy (what the hub would have indexed)."""
    job = jobs.load_job(env["job"])
    if copies is None:
        copies = {f"{JOB}/vm/{vm}": [jobs.vm_copy_digest(
            job, vm, jobs.vm_files(env["job"] / "vms" / vm))] for vm in vms}
    (env["job"] / "index-confirmed.json").write_text(json.dumps({
        "job_id": JOB, "state": state, "checked_at": "2026-11-01T23:59:00Z",
        "sessions": [JOB] + [f"{JOB}/vm/{vm}" for vm in vms], "vm_copies": copies}))


def vm_lines(env, vm=VM):
    return [l for l in shipped_lines(env["landing"])
            if l["session_id"] == f"{JOB}/vm/{vm}"]


def test_a_stopped_vm_is_resumed_pulled_shipped_and_left_stopped(env, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=boat) == WAIT
    out = capsys.readouterr().out
    assert "awaiting index confirmation" in out and "safe to delete" not in out
    assert boat.states[VM] == "stopped"
    names = [c[0] for c in boat.calls]
    assert names.index("resume") < names.index("checksums") < names.index("pull")
    assert names[-1] == "stop"
    assert (env["job"] / "vms" / VM / "projects").is_dir()
    receipt = json.loads((env["job"] / "vms" / VM / "EXPORTED.json").read_text())
    assert receipt["job_id"] == JOB and receipt["vm_id"] == VM
    assert receipt["records"] == 3 and len(receipt["files"]) == 1


def test_vm_records_are_their_own_conversation_tagged_with_the_job(env):
    env["boat"].add(VM, "running", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT
    lines = vm_lines(env)
    assert len(lines) == 3
    for line in lines:
        assert line["job"] == {"job_id": JOB, "duet_channel_id": "chan_7Hq2",
                               "studio_task_key": "ATR-31", "kind": "vm", "vm_id": VM}
        assert JOB in line["cwd"] and VM in line["origin"] or line["role"] == "job"
    (header,) = [l for l in lines if l["role"] == "job"]
    assert header["text"].startswith(f"[claude-jobs vm] {JOB} vm {VM}")
    assert f"Claude session: {VSID}" in header["text"]
    assert "VM cwd: /home/user/storm-check" in header["text"]
    blob = json.dumps(shipped_lines(env["landing"]))
    assert GH not in blob and "[REDACTED:github_token]" in blob
    (seal,) = [l for l in shipped_lines(env["landing"])
               if l["text"].startswith("[claude-jobs final]")]
    assert f"Boat VMs 3 from 1 VM(s): {VM}" in seal["text"]
    (job_header,) = [l for l in shipped_lines(env["landing"])
                     if l["text"].startswith("[claude-jobs job]") and "Boat VMs" in l["text"]]
    assert f"Boat VMs: {VM}" in job_header["text"]


def test_a_running_vm_is_not_resumed_but_is_stopped(env):
    env["boat"].add(VM, "running", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT
    assert ("resume", VM) not in env["boat"].calls
    assert env["boat"].states[VM] == "stopped"


def test_a_copy_that_differs_from_the_vm_is_discarded(env, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.corrupt = True
    assert boat_export.run(JOB, [VM], boat=boat) == jobs.EXIT_UNCONFIRMED
    assert "does not match the VM" in capsys.readouterr().out
    assert boat.states[VM] == "stopped"
    assert not (env["job"] / "vms" / VM).exists()
    assert not list((env["job"] / "vms").glob(".*"))
    assert not vm_lines(env)


def test_boat_api_failure_leaves_the_vm_stopped(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.fail["checksums"] = "boat exec timed out after 180s"
    assert boat_export.run(JOB, [VM], boat=boat) == jobs.EXIT_UNCONFIRMED
    assert boat.states[VM] == "stopped"
    assert ("stop", VM) in boat.calls


def test_a_vm_that_cannot_be_confirmed_stopped_is_not_safe(env, capsys):
    boat = env["boat"]
    boat.add(VM, "running", ROWS)
    boat.fail["stop"] = "still 'stopping' after 300s"
    assert boat_export.run(JOB, [VM], boat=boat) == jobs.EXIT_UNCONFIRMED
    assert "could not confirm" in capsys.readouterr().out
    assert not (env["job"] / "vms" / VM / "EXPORTED.json").exists()


def test_an_empty_vm_is_refused_unless_allowed(env):
    env["boat"].add(VM, "stopped")
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == jobs.EXIT_UNCONFIRMED
    assert boat_export.run(JOB, [VM], boat=env["boat"], allow_empty=True) == WAIT


def test_hub_failure_means_nothing_is_safe_and_a_retry_recovers(env, monkeypatch, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    assert boat_export.run(JOB, [VM], boat=boat) == jobs.EXIT_UNCONFIRMED
    assert "nothing is safe to delete" in capsys.readouterr().err
    assert boat.states[VM] == "stopped"
    assert not (env["job"] / "vms" / VM / "EXPORTED.json").exists()
    assert (env["job"] / "vms" / VM / "projects").is_dir()   # kept for the retry
    monkeypatch.setattr(ship, "upload", landing_upload(env["landing"]))
    assert boat_export.run(JOB, [VM], boat=boat) == WAIT


def test_one_bad_vm_does_not_block_the_good_one(env, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.add(VM2, "error", ROWS)
    assert boat_export.run(JOB, [VM, VM2], boat=boat) == jobs.EXIT_UNCONFIRMED
    captured = capsys.readouterr()
    assert f"left stopped: {VM}" in captured.out
    assert f"NOT safe to delete (left stopped): {VM2}" in captured.err
    assert (env["job"] / "vms" / VM / "EXPORTED.json").exists()
    assert not (env["job"] / "vms" / VM2).exists()


def test_boat_export_never_deletes_a_vm(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat_export.run(JOB, [VM], boat=boat)
    assert not [c for c in boat.calls if "delete" in c[0]]
    assert not hasattr(boat_export.Boat, "delete")


def test_check_reconfirms_a_receipt_without_boat(env, capsys):
    env["boat"].add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT
    assert boat_export.check(JOB, [VM]) == WAIT        # arrived, not indexed yet
    confirm_index(env, VM)
    assert boat_export.check(JOB, [VM]) == 0
    assert "safe to delete" in capsys.readouterr().out
    copy = next((env["job"] / "vms" / VM / "projects").rglob("*.jsonl"))
    copy.write_text(copy.read_text() + "\n")
    assert boat_export.check(JOB, [VM]) == jobs.EXIT_UNCONFIRMED
    assert boat_export.check(JOB, [VM2]) == jobs.EXIT_UNCONFIRMED


def test_a_reexport_replaces_the_copy_and_ships_only_new_records(env):
    boat = env["boat"]
    home = boat.add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=boat) == WAIT
    first = len(vm_lines(env))
    f = next((home / ".claude" / "projects").rglob("*.jsonl"))
    with f.open("a") as fh:
        fh.write(json.dumps(session_line("assistant", "A follow-up after the VM resumed.",
                                         "2026-11-01T23:30:00.000Z", sid=VSID,
                                         cwd="/home/user/storm-check")) + "\n")
    assert boat_export.run(JOB, [VM], boat=boat) == WAIT
    lines = vm_lines(env)
    assert len(lines) == first + 2   # the new message, and a VM header for the new copy
    assert "A follow-up after the VM resumed." in json.dumps(lines)


@pytest.mark.parametrize("job_id,vms", [("cj_bad", [VM]), (JOB, []), (JOB, ["../x"]),
                                        ("cj_20261101T120000Z_000000", [VM])])
def test_bad_input_touches_nothing(env, job_id, vms):
    assert boat_export.run(job_id, vms, boat=env["boat"]) == jobs.EXIT_CONFIG
    assert not env["boat"].calls


def test_no_hub_configured_touches_nothing(env, monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_HUB_INGEST")
    env["boat"].add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == jobs.EXIT_CONFIG
    assert not env["boat"].calls


def test_cli_check_path(env, capsys):
    env["boat"].add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT
    assert cli.main(["boat-export", "--job", JOB, "--check", VM]) == WAIT
    confirm_index(env, VM)
    assert cli.main(["boat-export", "--job", JOB, "--check", VM]) == 0


@pytest.mark.parametrize("bad", [
    {"state": "partial"},
    {"copies": {f"{JOB}/vm/{VM}": ["0" * 64]}},  # the hub indexed another copy
    {"copies": {}},                              # no indexed copy of this VM
    {"vms": ()},                                 # does not list this VM
])
def test_an_index_confirmation_that_does_not_cover_the_vm_is_not_enough(env, bad):
    env["boat"].add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT
    vms = bad.pop("vms", (VM,))
    confirm_index(env, *vms, **bad)
    assert boat_export.check(JOB, [VM]) == WAIT


def test_a_confirmation_from_before_a_reexport_does_not_count(env):
    home = env["boat"].add(VM, "stopped", ROWS)
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT
    confirm_index(env, VM)
    assert boat_export.check(JOB, [VM]) == 0
    f = next((home / ".claude" / "projects").rglob("*.jsonl"))
    with f.open("a") as fh:
        fh.write(json.dumps(session_line("assistant", "More work after the confirmation.",
                                         "2026-11-01T23:40:00.000Z", sid=VSID)) + "\n")
    assert boat_export.run(JOB, [VM], boat=env["boat"]) == WAIT   # new copy, old confirmation
    assert boat_export.check(JOB, [VM]) == WAIT


def test_vm_records_carry_the_vm_claude_session_uuid(env):
    env["boat"].add(VM, "stopped", ROWS)
    boat_export.run(JOB, [VM], boat=env["boat"])
    for line in vm_lines(env):
        assert line["claude_session_id"] == VSID
        assert ["claude", VSID] in line["aliases"]


# ------------------------------------------------- the real CLI wrapper

def test_parse_sha256sum():
    text = ("08283c0d8dd1d39f6b8fba0765a9b7a66e4426a8d5ba420a54fa6386a3b24636"
            "  ./-home-user-x/a b.jsonl\nnot a line\n")
    assert boat_export.parse_sha256sum(text) == {
        "-home-user-x/a b.jsonl":
            "08283c0d8dd1d39f6b8fba0765a9b7a66e4426a8d5ba420a54fa6386a3b24636"}


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX stub binary")
def test_boat_wrapper_parses_the_cli_and_times_out(tmp_path, monkeypatch):
    stub = tmp_path / "boat"
    stub.write_text("""#!/bin/sh
case "$1" in
  info) echo '{"sandbox":{"id":"bx_x","state":"stopped"}}' ;;
  exec) printf '%s\\n' '{"event":"x"}' '{"exitCode":0,"success":true,"stdoutTruncated":false,"stdout":"08283c0d8dd1d39f6b8fba0765a9b7a66e4426a8d5ba420a54fa6386a3b24636  ./p/s.jsonl\\n"}' ;;
  scp) mkdir -p "$4/projects" ;;
  resume) sleep 5 ;;
  stop) echo 'boom' >&2; exit 4 ;;
esac
""")
    stub.chmod(0o755)
    boat = boat_export.Boat(binary=str(stub), timeout=1)
    assert boat.state("bx_x") == "stopped"
    assert boat.checksums("bx_x") == {"p/s.jsonl": "08283c0d8dd1d39f6b8fba0765a9b7a66e4426a8d5ba420a54fa6386a3b24636"}
    assert boat.pull("bx_x", tmp_path / "dest") == tmp_path / "dest" / "projects"
    with pytest.raises(boat_export.BoatError, match="timed out"):
        boat.resume("bx_x")
    with pytest.raises(boat_export.BoatError, match="failed \\(4\\): boom"):
        boat._run("stop", "bx_x")
