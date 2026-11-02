"""`boat-archive`: copy back and confirm before a job VM is let go, never
delete, and a daily sweep for stopped VMs whose transcripts were not copied.

Boat is faked (the boat-export fake plus ``info``/``list_all``). No test calls
the real Boat CLI or the network.
"""
from __future__ import annotations

import datetime
import json

import pytest

from agent_history import boat_archive, boat_export, cli, jobs, ship
from jobfixtures import JOB, landing_upload, make_job
from test_boat_export import ROWS, VM, VM2, FakeBoat, confirm_index

WAIT = boat_export.EXIT_AWAITING_INDEX
NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=datetime.timezone.utc)


class FakeArchiveBoat(FakeBoat):
    def __init__(self, homes):
        super().__init__(homes)
        self.stopped_at: dict[str, str] = {}
        self.expired: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.gone: set[str] = set()
        self.list_fails = False

    def stop(self, vm):
        super().stop(vm)
        self.stopped_at.setdefault(vm, "2026-11-19T12:00:00.000Z")

    def info(self, vm):
        self._call("info", vm)
        if vm in self.gone:
            raise boat_export.BoatError("boat info failed (1): sandbox not found")
        return {"id": vm, "name": self.names.get(vm, vm), "state": self.states[vm],
                "stoppedAt": self.stopped_at.get(vm) if self.states[vm] == "stopped" else None,
                "dataExpiredAt": self.expired.get(vm)}

    def list_all(self):
        self.calls.append(("list_all", ""))
        if self.list_fails:
            raise boat_export.BoatError("boat list timed out after 180s")
        return [{"id": vm, "name": self.names.get(vm, vm), "state": st, "labels": {}}
                for vm, st in self.states.items() if vm not in self.gone]


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
    return {"root": root, "landing": landing, "boat": FakeArchiveBoat(tmp_path / "homes"),
            "job": root / JOB}


def archived_json(env, vm=VM):
    path = env["job"] / "vms" / vm / boat_archive.ARCHIVED
    return json.loads(path.read_text()) if path.exists() else None


def set_vms(env, vms):
    meta = json.loads((env["job"] / "job.json").read_text())
    meta["vms"] = vms
    (env["job"] / "job.json").write_text(json.dumps(meta))


# ------------------------------------------------------------------ archive

def test_an_unexported_vm_is_copied_back_first_and_not_archived_until_indexed(env, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    assert boat_archive.run(JOB, [VM], boat=boat) == WAIT
    err = capsys.readouterr().err
    assert "NOT archived, left stopped" in err and "indexing not confirmed" in err
    assert ("pull", VM) in boat.calls
    assert boat.states[VM] == "stopped"
    assert archived_json(env) is None


def test_once_indexed_the_vm_is_archived_without_copying_again(env, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    assert boat_archive.run(JOB, [VM], boat=boat) == WAIT
    confirm_index(env, VM)
    boat.calls.clear()
    assert boat_archive.run(JOB, [VM], boat=boat) == 0
    names = [c[0] for c in boat.calls]
    assert "pull" not in names and "resume" not in names
    data = archived_json(env)
    assert data["job_id"] == JOB and data["vm_id"] == VM
    assert data["stopped_at"] == "2026-11-19T12:00:00Z"
    assert data["boat_data_kept_until"] == "2026-12-03T12:00:00Z"
    assert data["session"] == f"{JOB}/vm/{VM}"
    assert "archived" in capsys.readouterr().out


def test_a_running_vm_is_copied_then_stopped_then_archived_in_one_go(env, monkeypatch):
    boat = env["boat"]
    boat.add(VM, "running", ROWS)
    real = boat_export.run

    def export_then_index(*a, **kw):          # the runner confirms right after the ship
        code = real(*a, **kw)
        confirm_index(env, VM)
        return code
    monkeypatch.setattr(boat_export, "run", export_then_index)
    assert boat_archive.run(JOB, [VM], boat=boat) == 0
    assert boat.states[VM] == "stopped" and archived_json(env)


def test_a_failed_copy_back_is_refused_and_the_vm_left_stopped(env, capsys):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.fail["pull"] = "scp: connection reset"
    assert boat_archive.run(JOB, [VM], boat=boat) == 1
    assert "NOT archived" in capsys.readouterr().err
    assert boat.states[VM] == "stopped" and archived_json(env) is None


def test_one_vm_waiting_does_not_block_archiving_the_other(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.add(VM2, "stopped", ROWS)
    assert boat_archive.run(JOB, [VM, VM2], boat=boat) == WAIT
    confirm_index(env, VM)                     # only VM's copy is indexed
    assert boat_archive.run(JOB, [VM, VM2], boat=boat) == WAIT
    assert archived_json(env, VM) and archived_json(env, VM2) is None


def test_boat_archive_never_deletes_a_vm(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat_archive.run(JOB, [VM], boat=boat)
    confirm_index(env, VM)
    boat_archive.run(JOB, [VM], boat=boat)
    assert not any(c[0] in ("delete", "rm", "destroy") for c in boat.calls)
    assert not hasattr(boat_export.Boat, "delete")
    assert VM in boat.states


@pytest.mark.parametrize("job_id,vms", [("cj_bad", [VM]), (JOB, []), (JOB, ["../etc"])])
def test_bad_input_touches_nothing(env, job_id, vms):
    assert boat_archive.run(job_id, vms, boat=env["boat"]) == 2
    assert env["boat"].calls == []


# -------------------------------------------------------------------- sweep

def test_a_stopped_vm_named_for_the_job_and_never_copied_is_an_urgent_alert(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.names[VM] = f"{JOB} audit signup"
    boat.stopped_at[VM] = "2026-11-08T12:00:00.000Z"      # 12 days before NOW
    alerts, warnings = boat_archive.sweep(boat=boat, now=NOW)
    assert warnings == []
    (a,) = alerts
    assert a["job"] == JOB and a["vm"] == VM and a["urgent"] is True
    assert a["repo"] == "lvtd-dgtl/storm-check" and a["branch"] == f"job/{JOB}"
    assert "about 2 more day(s), until 2026-11-22" in a["text"]
    assert f"Run: agent-history boat-archive --job {JOB} {VM}" in a["text"]
    text = boat_archive.format_alerts(alerts)
    assert text.splitlines()[0] == "Boat VM transcripts not copied (1)"
    assert text.splitlines()[1].startswith("!! ")


def test_vms_recorded_in_job_json_are_checked_and_running_ones_are_quiet(env):
    boat = env["boat"]
    boat.add(VM, "running", ROWS)
    boat.add(VM2, "stopped", ROWS)
    boat.stopped_at[VM2] = "2026-11-18T12:00:00.000Z"
    set_vms(env, [VM, {"id": VM2, "purpose": "lighthouse"}])
    alerts, _ = boat_archive.sweep(boat=boat, discover=False, now=NOW)
    assert [a["vm"] for a in alerts] == [VM2]
    assert alerts[0]["urgent"] is False and "12 more day(s)" in alerts[0]["text"]
    assert ("list_all", "") not in boat.calls


def test_copied_and_indexed_or_archived_vms_are_not_alerts(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.add(VM2, "stopped", ROWS)
    boat_export.run(JOB, [VM, VM2], boat=boat)
    confirm_index(env, VM, VM2)
    boat_archive.run(JOB, [VM2], boat=boat)        # VM2 archived; VM only indexed
    alerts, _ = boat_archive.sweep(boat=boat, now=NOW)
    assert alerts == []
    assert boat_archive.format_alerts(alerts) == ""


def test_arrived_but_not_indexed_is_reported_with_the_reason(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat_export.run(JOB, [VM], boat=boat)
    alerts, _ = boat_archive.sweep(boat=boat, now=NOW)
    (a,) = alerts
    assert "indexing not confirmed yet" in a["text"]


def test_a_vm_that_is_gone_or_expired_without_a_copy_is_urgent(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.add(VM2, "stopped", ROWS)
    set_vms(env, [VM, VM2])
    boat.gone.add(VM)
    boat.expired[VM2] = "2026-11-17T00:00:00Z"
    alerts, _ = boat_archive.sweep(boat=boat, discover=False, now=NOW)
    by_vm = {a["vm"]: a for a in alerts}
    assert by_vm[VM]["urgent"] and "no longer exists" in by_vm[VM]["text"]
    assert by_vm[VM2]["urgent"] and "expired this VM's data" in by_vm[VM2]["text"]


def test_a_failed_boat_list_still_checks_recorded_vms(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    set_vms(env, [VM])
    boat.list_fails = True
    alerts, warnings = boat_archive.sweep(boat=boat, now=NOW)
    assert [a["vm"] for a in alerts] == [VM]
    assert any("could not list Boat sandboxes" in w for w in warnings)


def test_vms_named_for_unknown_jobs_are_ignored(env):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    boat.names[VM] = "cj_20261101T000000Z_abcdef someone else's"
    assert boat_archive.sweep(boat=boat, now=NOW) == ([], [])


# ---------------------------------------------------------------------- CLI

def test_cli_sweep_is_silent_when_nothing_is_wrong_and_posts_text_when_it_is(
        env, monkeypatch, capsys):
    boat = env["boat"]
    monkeypatch.setattr(boat_export, "Boat", lambda **kw: boat)
    assert cli.main(["boat-archive", "--sweep"]) == 0
    assert capsys.readouterr().out == ""
    boat.add(VM, "stopped", ROWS)
    boat.names[VM] = f"{JOB} audit"
    assert cli.main(["boat-archive", "--sweep"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Boat VM transcripts not copied (1)") and VM in out
    assert cli.main(["boat-archive", "--sweep", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["alerts"][0]["vm"] == VM


def test_cli_argument_errors(env, monkeypatch):
    monkeypatch.setattr(boat_export, "Boat", lambda **kw: env["boat"])
    assert cli.main(["boat-archive", "--sweep", "--job", JOB]) == 2
    assert cli.main(["boat-archive", "--job", JOB]) == 2
    assert cli.main(["boat-archive", VM]) == 2


def test_cli_archive_path(env, monkeypatch):
    boat = env["boat"]
    boat.add(VM, "stopped", ROWS)
    monkeypatch.setattr(boat_export, "Boat", lambda **kw: boat)
    assert cli.main(["boat-archive", "--job", JOB, VM]) == WAIT
    confirm_index(env, VM)
    assert cli.main(["boat-archive", "--job", JOB, VM]) == 0
    assert archived_json(env)


def test_cli_sweep_needs_the_jobs_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_JOBS_DIR", str(tmp_path / "missing"))
    assert cli.main(["boat-archive", "--sweep"]) == 2


def test_boat_wrapper_list_all_parses_array_object_and_jsonl(tmp_path, monkeypatch):
    b = boat_export.Boat(binary="unused")
    for text in ('[{"id": "bx_a"}]', '{"sandboxes": [{"id": "bx_a"}]}',
                 '{"sandbox": {"id": "bx_a"}}\n{"id": "bx_b"}\n'):
        monkeypatch.setattr(b, "_run", lambda *a, _t=text, **k: _t)
        ids = [r["id"] for r in b.list_all()]
        assert ids[0] == "bx_a"
