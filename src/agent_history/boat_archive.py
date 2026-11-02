"""`agent-history boat-archive` — let a job's Boat VM go only after its
transcripts are safely on the hub, and catch the ones that were forgotten.

Boat has no separate archive step: ``boat stop`` snapshots a sandbox and keeps
its data for 14 days (``boat snapshot <id> <name>`` keeps it longer); after
that the disk is gone. So "archiving" a job VM means its final stop, and the
danger is a VM that is stopped and forgotten with transcripts nobody copied.

``boat-archive --job <job_id> <vm_id>...`` is the guarded final stop:

1. If the VM's copy is not yet confirmed (``boat-export --check`` would not
   exit 0), run the copy-back first (``boat-export``: resume if stopped, pull
   ``~/.claude/projects``, verify, stop, ship).
2. Only when the copy has arrived **and** the runner's ``confirm-indexed``
   result covers it: make sure the VM is stopped and record
   ``<jobs>/<job_id>/vms/<vm_id>/ARCHIVED.json``.
3. Otherwise refuse: the VM is left stopped, nothing is recorded, and the
   reason is printed. Exit 4 means "arrived, awaiting index confirmation".

Nothing here ever deletes a VM; the Boat wrapper has no delete call.

``boat-archive --sweep`` is the daily check. For every job under
``AGENT_HISTORY_JOBS_DIR`` it finds the job's VMs and prints one alert line
per VM that is **stopped (or gone) with transcripts not confirmed copied**,
with how long Boat keeps its data. Empty output means nothing to report, so
a cron that posts the output verbatim (Clamps' Hermes ``deliver=telegram``
jobs, like ``claude-job notify --drain``) stays silent on good days.

Which VMs belong to a job, in this order (union):
- ``job.json`` ``vms``: a list of ids (or objects with ``id``/``vm_id``),
  for whoever records VMs in the job file;
- ``<jobs>/<job_id>/vms/<vm_id>/``: VMs already exported once;
- Boat itself: any sandbox whose **name** contains the job id. Start job VMs
  with ``boat new --name "<job_id> <purpose>"``; Boat labels cannot be set
  from the CLI, the name can (``boat rename`` fixes one that was missed).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

from . import boat_export, config, jobs, ship

RETENTION_DAYS = 14          # Boat keeps a stopped sandbox's data this long
URGENT_DAYS = 3              # alert with "!!" when this little is left
ARCHIVED = "ARCHIVED.json"
JOB_IN_NAME = re.compile(r"cj_\d{8}T\d{6}Z_[0-9a-f]{6}")
GONE_HINTS = ("not found", "404", "no such", "does not exist", "unknown sandbox")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


def archived(job: jobs.Job, vm: str) -> dict | None:
    """The VM's ARCHIVED.json when it names this job and VM, else None."""
    try:
        data = json.loads((job.path / "vms" / vm / ARCHIVED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("job_id") == job.job_id and data.get("vm_id") == vm:
        return data
    return None


def _write_archived(job: jobs.Job, vm: str, info: dict) -> dict:
    receipt = json.loads((job.path / "vms" / vm / "EXPORTED.json").read_text(encoding="utf-8"))
    stopped = _parse(info.get("stoppedAt")) or _now()
    data = {"job_id": job.job_id, "vm_id": vm, "archived_at": _iso(_now()),
            "stopped_at": _iso(stopped),
            "boat_data_kept_until": _iso(stopped + datetime.timedelta(days=RETENTION_DAYS)),
            "copy_sha256": receipt.get("copy_sha256"),
            "session": job.vm_session(vm),
            "note": "transcripts arrived and indexed on the hub; the VM is stopped and "
                    "may now expire. boat-archive never deletes it."}
    target = job.path / "vms" / vm / ARCHIVED
    part = target.with_name(f".{ARCHIVED}.part")
    part.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(part, target)
    return data


def _statuses(job: jobs.Job, vms: list[str], index_receipt: Path | None) -> dict[str, tuple[int, str]]:
    state = ship.State.open(writable=False)
    try:
        return {vm: boat_export.receipt_status(job, vm, state, index_receipt) for vm in vms}
    finally:
        state.close()


def run(job_id: str, vms: list[str], *, allow_empty: bool = False,
        wait: int = jobs.DEFAULT_WAIT, boat: boat_export.Boat | None = None,
        index_receipt: Path | None = None) -> int:
    """Copy back (if needed), confirm, then archive. 0 every VM archived;
    4 some arrived but await index confirmation; 1 refused or failed;
    2 bad input or config."""
    problem = boat_export._validate(job_id, vms)
    if problem:
        print(f"boat-archive: {problem}", file=sys.stderr)
        return jobs.EXIT_CONFIG
    job = jobs.load_job(config.jobs_dir() / job_id)
    boat = boat or boat_export.Boat()
    vms = list(dict.fromkeys(vms))

    status = _statuses(job, vms, index_receipt)
    todo = [vm for vm in vms if status[vm][0] == jobs.EXIT_UNCONFIRMED]
    if todo:
        print("boat-archive: copying back first: " + " ".join(todo))
        code = boat_export.run(job_id, todo, allow_empty=allow_empty, wait=wait,
                               boat=boat, index_receipt=index_receipt)
        if code == jobs.EXIT_CONFIG:
            return code
        status.update(_statuses(job, todo, index_receipt))

    done, waiting, refused = [], [], []
    for vm in vms:
        code, why = status[vm]
        if code != jobs.EXIT_OK:
            (waiting if code == boat_export.EXIT_AWAITING_INDEX else refused).append((vm, why))
            continue
        try:
            boat.stop(vm)
            info = boat.info(vm) if hasattr(boat, "info") else {}
        except boat_export.BoatError as exc:
            refused.append((vm, f"transcripts are safe, but the VM could not be "
                                f"confirmed stopped: {exc}"))
            continue
        data = _write_archived(job, vm, info)
        done.append(vm)
        print(f"boat-archive: {vm}: transcripts arrived and indexed; archived "
              f"(stopped; Boat keeps its data until {data['boat_data_kept_until']})")
    for vm, why in waiting:
        print(f"boat-archive: {vm}: NOT archived, left stopped: {why}", file=sys.stderr)
    for vm, why in refused:
        print(f"boat-archive: {vm}: NOT archived, left stopped: {why}", file=sys.stderr)
    if refused:
        return jobs.EXIT_UNCONFIRMED
    return boat_export.EXIT_AWAITING_INDEX if waiting else jobs.EXIT_OK


# ------------------------------------------------------------------- sweep

def job_vms(job: jobs.Job, named: dict[str, set[str]] | None = None) -> list[str]:
    """Every VM tied to ``job``: job.json ``vms``, exported copies, and Boat
    sandboxes named for the job (``named``: job id -> VM ids)."""
    found: list[str] = []
    listed = job.meta.get("vms")
    if isinstance(listed, list):
        for item in listed:
            vm = (item.get("id") or item.get("vm_id")) if isinstance(item, dict) else item
            if isinstance(vm, str):
                found.append(vm)
    found += [p.name for p in job.vm_dirs()]
    found += sorted((named or {}).get(job.job_id, set()))
    return [vm for vm in dict.fromkeys(found) if jobs.VM_ID.match(vm)]


def _named(boat) -> tuple[dict[str, set[str]], str]:
    """Boat sandboxes whose name (or labels) carry a job id."""
    try:
        rows = boat.list_all()
    except boat_export.BoatError as exc:
        return {}, f"could not list Boat sandboxes ({exc}); only recorded VMs were checked"
    out: dict[str, set[str]] = {}
    for row in rows:
        vm = row.get("id")
        labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
        text = " ".join([str(row.get("name") or "")] + [f"{k}={v}" for k, v in labels.items()])
        if isinstance(vm, str):
            for job_id in JOB_IN_NAME.findall(text):
                out.setdefault(job_id, set()).add(vm)
    return out, ""


def sweep(*, boat=None, index_receipt: Path | None = None, discover: bool = True,
          now: datetime.datetime | None = None) -> tuple[list[dict], list[str]]:
    """Alerts for job VMs that are stopped or gone with transcripts not
    confirmed copied. Returns (alerts, warnings); each alert is a dict with
    job, vm, state, urgent, text."""
    boat = boat or boat_export.Boat()
    now = now or _now()
    named, warning = _named(boat) if discover else ({}, "")
    warnings = [warning] if warning else []
    alerts: list[dict] = []
    for path in jobs.job_dirs(config.jobs_dir()):
        try:
            job = jobs.load_job(path)
        except jobs.JobError as exc:
            warnings.append(str(exc))
            continue
        vms = [vm for vm in job_vms(job, named) if not archived(job, vm)]
        if not vms:
            continue
        status = _statuses(job, vms, index_receipt)
        for vm in vms:
            code, why = status[vm]
            if code == jobs.EXIT_OK:
                continue          # copied and indexed; only the archive record is missing
            try:
                info = boat.info(vm)
                state = str(info.get("state") or "unknown")
            except boat_export.BoatError as exc:
                if any(h in str(exc).lower() for h in GONE_HINTS):
                    info, state = {}, "gone"
                else:
                    warnings.append(f"{job.job_id} {vm}: could not read its state ({exc})")
                    continue
            alert = _alert(job, vm, state, info, why, now)
            if alert:
                alerts.append(alert)
    return alerts, warnings


def _alert(job: jobs.Job, vm: str, state: str, info: dict, why: str,
           now: datetime.datetime) -> dict | None:
    fix = f"agent-history boat-archive --job {job.job_id} {vm}"
    base = {"job": job.job_id, "vm": vm, "state": state, "why": why,
            "repo": job.field("repo"), "branch": job.field("branch")}
    if state == "gone":
        return {**base, "urgent": True,
                "text": f"{job.job_id} {vm}: the VM no longer exists and its transcripts "
                        f"were never confirmed copied ({why})"}
    if info.get("dataExpiredAt"):
        return {**base, "urgent": True,
                "text": f"{job.job_id} {vm}: Boat expired this VM's data on "
                        f"{info['dataExpiredAt']}; transcripts not confirmed copied ({why})"}
    if state != boat_export.STOPPED:
        return None               # still running: the job may be using it
    stopped = _parse(info.get("stoppedAt"))
    if stopped is None:
        return {**base, "urgent": False,
                "text": f"{job.job_id} {vm}: stopped, transcripts not confirmed copied "
                        f"({why}). Run: {fix}"}
    until = stopped + datetime.timedelta(days=RETENTION_DAYS)
    left = (until - now).total_seconds() / 86400
    days = max(0, int(left))
    return {**base, "urgent": left <= URGENT_DAYS, "stopped_at": _iso(stopped),
            "data_kept_until": _iso(until),
            "text": f"{job.job_id} {vm}: stopped since {_iso(stopped)[:10]}, transcripts "
                    f"not confirmed copied ({why}); Boat keeps its data about {days} more "
                    f"day(s), until {_iso(until)[:10]}. Run: {fix}"}


def format_alerts(alerts: list[dict]) -> str:
    """The text a cron posts verbatim; "" when there is nothing to report."""
    if not alerts:
        return ""
    lines = [f"Boat VM transcripts not copied ({len(alerts)})"]
    for a in sorted(alerts, key=lambda a: (not a["urgent"], a["job"], a["vm"])):
        lines.append(("!! " if a["urgent"] else "- ") + a["text"])
    return "\n".join(lines)


def run_sweep(*, as_json: bool = False, discover: bool = True,
              index_receipt: Path | None = None, boat=None) -> int:
    root = config.jobs_dir()
    if not root.is_dir():
        print(f"boat-archive: jobs directory {root} does not exist "
              "(AGENT_HISTORY_JOBS_DIR)", file=sys.stderr)
        return jobs.EXIT_CONFIG
    alerts, warnings = sweep(boat=boat, index_receipt=index_receipt, discover=discover)
    for w in warnings:
        print(f"boat-archive: {w}", file=sys.stderr)
    if as_json:
        print(json.dumps({"alerts": alerts, "warnings": warnings}, indent=1))
    else:
        text = format_alerts(alerts)
        if text:
            print(text)
    return jobs.EXIT_OK
