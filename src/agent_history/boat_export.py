"""`agent-history boat-export` — keep a Boat VM's transcripts before teardown.

A fleet job runs Claude Code on Boat VMs and keeps them stopped (paused), not
deleted, until the work is verified. Before teardown it runs::

    agent-history boat-export --job <job_id> <vm_id> [<vm_id> ...]

For each VM: resume it if it is stopped, list the sha256 of every
``~/.claude/projects/**.jsonl`` on it, pull that tree with ``boat scp`` into
``<jobs>/<job_id>/vms/<vm_id>/projects/``, check the copy against the list,
and stop the VM again (always, also on failure). Then the job is shipped with
``ship-jobs --final``; once the hub confirmed arrival a receipt is written.

A VM is reported **safe to delete** only when, on top of that, indexing is
confirmed: ``confirm-indexed --job <job_id>`` (run on the runner's side,
never as claude-jobs) wrote its result to ``<jobs>/<job_id>/index-confirmed.json``
saying the VM's session is indexed, checked after the export. Until then the
exit is 4, "arrived, awaiting index confirmation", and ``--check`` can be
polled without touching Boat. Nothing here ever deletes a VM, and the VM
needs nothing installed.

See docs/design/boat-vm-transcripts.md.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, jobs, ship

REMOTE = ".claude/projects"        # relative to the VM user's home
RUNNING = frozenset({"running", "ready", "up", "started"})
STOPPED = "stopped"
TRANSIENT = frozenset({"stopping", "pending", "starting", "resuming", "cloning"})
POLL_SECONDS = 3.0
EXIT_AWAITING_INDEX = 4
INDEX_RECEIPT = "index-confirmed.json"


class BoatError(Exception):
    """A Boat CLI call that failed, timed out or answered nonsense."""


class Boat:
    """The Boat CLI, one subprocess per call, each with a timeout (the Boat
    API has hung before while VMs kept running). Tests swap in a fake."""

    def __init__(self, binary: str | None = None, timeout: int = 180,
                 stop_timeout: int = 300) -> None:
        self.binary = binary or os.environ.get("AGENT_HISTORY_BOAT") or "boat"
        self.timeout = timeout
        self.stop_timeout = stop_timeout

    def _run(self, *args: str, timeout: int | None = None) -> str:
        env = {**os.environ, "BOAT_NO_UPDATE": "1"}  # never self-update mid-export
        try:
            done = subprocess.run([self.binary, *args], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, env=env,
                                  timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            raise BoatError(f"boat {args[0]} timed out after "
                            f"{timeout or self.timeout}s") from None
        except OSError as exc:
            raise BoatError(f"cannot run {self.binary}: {exc}") from None
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()
            raise BoatError(f"boat {args[0]} failed ({done.returncode}): "
                            f"{tail[-1] if tail else 'no output'}")
        return done.stdout

    @staticmethod
    def _json(text: str) -> dict:
        # --json may print progress events first; the answer is the last object.
        for line in reversed([l for l in text.splitlines() if l.strip()]):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        try:
            value = json.loads(text)
        except ValueError:
            raise BoatError("boat printed no JSON") from None
        if not isinstance(value, dict):
            raise BoatError("boat printed no JSON object")
        return value

    def state(self, vm: str) -> str:
        data = self._json(self._run("info", vm, "--json"))
        box = data.get("sandbox") if isinstance(data.get("sandbox"), dict) else data
        state = box.get("state")
        if not isinstance(state, str):
            raise BoatError(f"boat info {vm}: no state")
        return state

    def resume(self, vm: str) -> None:
        self._run("resume", vm, "--json")

    def stop(self, vm: str) -> None:
        """Stop and wait until Boat reports the VM stopped."""
        if self.state(vm) != STOPPED:
            self._run("stop", vm, "--json")
        self.wait_for(vm, STOPPED, self.stop_timeout)

    def wait_for(self, vm: str, wanted: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = self.state(vm)
            if state == wanted or (wanted == "running" and state in RUNNING):
                return
            if state not in TRANSIENT and wanted == STOPPED and state not in RUNNING:
                raise BoatError(f"{vm} is {state!r}, not {wanted!r}")
            if time.monotonic() >= deadline:
                raise BoatError(f"{vm} still {state!r} after {timeout}s")
            time.sleep(POLL_SECONDS)

    def checksums(self, vm: str) -> dict[str, str]:
        """sha256 of every transcript on the VM, by path under projects/."""
        script = (f'if [ -d "$HOME/{REMOTE}" ]; then cd "$HOME/{REMOTE}" && '
                  "find . -type f -name '*.jsonl' -exec sha256sum {} +; fi")
        data = self._json(self._run("exec", vm, "--json", "--timeout", "300",
                                    "--", "sh", "-c", script, timeout=self.timeout + 300))
        if data.get("exitCode") not in (0, None) or data.get("success") is False:
            raise BoatError(f"listing transcripts on {vm} failed: "
                            f"{(data.get('stderr') or '').strip()[:200]}")
        if data.get("stdoutTruncated"):
            raise BoatError(f"the transcript list on {vm} was truncated")
        return parse_sha256sum(data.get("stdout") or "")

    def pull(self, vm: str, dest: Path) -> Path:
        """Copy the VM's projects tree to ``dest/projects`` and return it."""
        dest.mkdir(parents=True, exist_ok=True)
        self._run("scp", "-r", f"{vm}:{REMOTE}", f"{dest}{os.sep}",
                  timeout=max(self.timeout, 1800))
        return dest / "projects"


def parse_sha256sum(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64:
            continue
        out[name[2:] if name.startswith("./") else name] = digest
    return out


def local_checksums(projects: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not projects.is_dir():
        return out
    for path in projects.rglob("*.jsonl"):
        if path.is_file():
            out[path.relative_to(projects).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


# ------------------------------------------------------------------ export

@dataclasses.dataclass
class VmResult:
    vm: str
    pulled: bool = False
    stopped: bool = False
    files: dict = dataclasses.field(default_factory=dict)
    problem: str = ""

    @property
    def ok(self) -> bool:
        return self.pulled and self.stopped and not self.problem


def pull_vm(boat: Boat, job: jobs.Job, vm: str, *, allow_empty: bool) -> VmResult:
    """Pull and verify one VM's transcripts; leave it stopped either way."""
    result = VmResult(vm)
    vms = job.path / "vms"
    staging = vms / f".{vm}.part"
    try:
        state = boat.state(vm)
        if state in TRANSIENT:
            boat.wait_for(vm, STOPPED if state == "stopping" else "running",
                          boat.stop_timeout)
            state = boat.state(vm)
        if state == STOPPED:
            boat.resume(vm)
        elif state not in RUNNING:
            raise BoatError(f"{vm} is {state!r}; not exporting it")

        remote = boat.checksums(vm)
        if not remote and not allow_empty:
            raise BoatError(f"{vm} has no transcripts under ~/{REMOTE} "
                            "(pass --allow-empty if that is expected)")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, mode=0o700)
        pulled = boat.pull(vm, staging) if remote else staging / "projects"
        pulled.mkdir(exist_ok=True)
        local = local_checksums(pulled)
        if local != remote:
            missing = sorted(set(remote) - set(local))
            differ = sorted(k for k in set(remote) & set(local) if remote[k] != local[k])
            raise BoatError(f"the copy of {vm} does not match the VM "
                            f"(missing {len(missing)}, different {len(differ)})")
        _swap_in(vms / vm, pulled)
        result.pulled, result.files = True, local
    except (BoatError, OSError) as exc:
        result.problem = str(exc)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            boat.stop(vm)
            result.stopped = True
        except BoatError as exc:
            result.problem = (result.problem + "; " if result.problem else "") + \
                f"could not confirm {vm} stopped: {exc}"
    return result


def _swap_in(target: Path, pulled: Path) -> None:
    """Replace ``target/projects`` with the verified copy, atomically enough
    that a crash leaves either the old copy or the new one in place."""
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = target / "projects"
    old = target.parent / f".{target.name}.old"
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        os.replace(final, old)
    os.replace(pulled, final)
    shutil.rmtree(old, ignore_errors=True)
    (target / "EXPORTED.json").unlink(missing_ok=True)  # stale until re-confirmed


def index_problem(job: jobs.Job, vm: str, exported_at: str,
                  receipt: Path | None = None) -> str:
    """Why the runner's index confirmation does not (yet) cover ``vm``; ""
    when it does. The confirmation is ``confirm-indexed --json`` output."""
    path = receipt or job.path / INDEX_RECEIPT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "indexing not confirmed yet"
    except (OSError, ValueError):
        return "the index confirmation is unreadable"
    if not isinstance(data, dict) or data.get("job_id") != job.job_id:
        return "the index confirmation names another job"
    if data.get("state") != "indexed":
        return f"indexing is {data.get('state') or 'unknown'}"
    if job.vm_session(vm) not in (data.get("sessions") or []):
        return "the index confirmation does not cover this VM yet"
    if str(data.get("checked_at") or "") < exported_at:
        return "the index confirmation predates this VM's export"
    return ""


def vm_digests(job: jobs.Job, vm: str) -> list[str]:
    files = jobs.vm_files(job.path / "vms" / vm)
    return [jobs.record_digest(r) for r, _ in jobs.vm_records(job, vm, files)]


def write_receipt(job: jobs.Job, vm: str, files: dict[str, str]) -> Path:
    receipt = job.path / "vms" / vm / "EXPORTED.json"
    data = {"job_id": job.job_id, "vm_id": vm,
            "exported_at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "files": files, "records": len(vm_digests(job, vm)),
            "session": job.vm_session(vm),
            "confirmed": "ship-jobs --final exit 0"}
    part = receipt.with_name(".EXPORTED.json.part")
    part.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(part, receipt)
    return receipt


def run(job_id: str, vms: list[str], *, allow_empty: bool = False,
        wait: int = jobs.DEFAULT_WAIT, boat: Boat | None = None,
        index_receipt: Path | None = None) -> int:
    problem = _validate(job_id, vms)
    if problem:
        print(f"boat-export: {problem}", file=sys.stderr)
        return jobs.EXIT_CONFIG
    if not config.hub_ingest():
        print("boat-export: AGENT_HISTORY_HUB_INGEST is not set; refusing to pull "
              "transcripts that could not be shipped", file=sys.stderr)
        return jobs.EXIT_CONFIG
    job = jobs.load_job(config.jobs_dir() / job_id)
    boat = boat or Boat()

    results = []
    for vm in vms:
        result = pull_vm(boat, job, vm, allow_empty=allow_empty)
        results.append(result)
        state = "pulled and stopped" if result.ok else f"NOT exported: {result.problem}"
        print(f"boat-export: {vm}: {state} ({len(result.files)} transcript file(s))")

    pulled = [r for r in results if r.ok]
    if not pulled:
        print("boat-export: nothing pulled; every VM is left stopped", file=sys.stderr)
        return jobs.EXIT_UNCONFIRMED
    code = jobs.run(job_id, wait=wait)
    if code != jobs.EXIT_OK:
        print(f"boat-export: the hub did not confirm {job_id} (ship-jobs exit "
              f"{code}); the VMs stay stopped, nothing is safe to delete",
              file=sys.stderr)
        return code
    safe, waiting = [], []
    for result in pulled:
        receipt = write_receipt(job, result.vm, result.files)
        exported = json.loads(receipt.read_text(encoding="utf-8"))["exported_at"]
        (waiting if index_problem(job, result.vm, exported, index_receipt) else safe).append(result.vm)
    if safe:
        print("boat-export: safe to delete: " + " ".join(safe))
    if waiting:
        print("boat-export: arrived; awaiting index confirmation (confirm-indexed "
              f"--job {job_id} on the runner's side), left stopped: " + " ".join(waiting))
    failed = [r.vm for r in results if not r.ok]
    if failed:
        print("boat-export: NOT safe to delete (left stopped): " + " ".join(failed),
              file=sys.stderr)
        return jobs.EXIT_UNCONFIRMED
    return EXIT_AWAITING_INDEX if waiting else jobs.EXIT_OK


def check(job_id: str, vms: list[str], index_receipt: Path | None = None) -> int:
    """Re-confirm without Boat: files unchanged, records shipped, and the
    runner's index confirmation covers the VM. 0 safe to delete, 4 arrived
    but indexing not confirmed yet, 1 anything else."""
    problem = _validate(job_id, vms)
    if problem:
        print(f"boat-export: {problem}", file=sys.stderr)
        return jobs.EXIT_CONFIG
    job = jobs.load_job(config.jobs_dir() / job_id)
    state = ship.State.open(writable=False)
    bad: list[str] = []
    waiting: list[str] = []
    try:
        for vm in vms:
            receipt = job.path / "vms" / vm / "EXPORTED.json"
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bad.append(f"{vm}: no receipt")
                continue
            if data.get("job_id") != job_id or data.get("vm_id") != vm:
                bad.append(f"{vm}: receipt names another job or VM")
                continue
            if local_checksums(job.path / "vms" / vm / "projects") != data.get("files"):
                bad.append(f"{vm}: files changed since the export")
                continue
            unshipped = [d for d in vm_digests(job, vm) if not state.shipped(d)]
            if unshipped:
                bad.append(f"{vm}: {len(unshipped)} record(s) not confirmed shipped")
                continue
            problem = index_problem(job, vm, str(data.get("exported_at") or ""),
                                    index_receipt)
            if problem:
                waiting.append(f"{vm}: arrived; {problem}")
                continue
            print(f"boat-export: {vm}: arrived and indexed on the hub; safe to delete")
    finally:
        state.close()
    for line in bad + waiting:
        print(f"boat-export: {line}", file=sys.stderr)
    if bad:
        return jobs.EXIT_UNCONFIRMED
    return EXIT_AWAITING_INDEX if waiting else jobs.EXIT_OK


def _validate(job_id: str, vms: list[str]) -> str:
    if not jobs.JOB_ID.match(job_id or ""):
        return f"{job_id!r} is not a job id"
    if not vms:
        return "name at least one VM"
    bad = [vm for vm in vms if not jobs.VM_ID.match(vm)]
    if bad:
        return f"not a VM id: {', '.join(bad)}"
    try:
        jobs.load_job(config.jobs_dir() / job_id)
    except jobs.JobError as exc:
        return str(exc)
    return ""
