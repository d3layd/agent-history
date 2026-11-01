"""`agent-history ship-jobs` — ship ``claude -p`` job transcripts to the hub.

The claude -p job runner keeps one directory per job::

    <jobs>/<job_id>/job.json          job_id, ask, duet_channel_id, studio_task_key, ...
    <jobs>/<job_id>/transcript.jsonl  the stream-json output of claude -p

and Claude Code writes the job's session file(s) under the job user's
``~/.claude/projects``. This module turns both into ordinary shipped Records
(the same v1 inbox lines `ship` sends, through the same upload-only key and
outbox), arranged so a parent agent can read a job back with the hub's
existing tools:

- every record of a job has ``session_id`` = the job id, so
  ``history_show(session=<job id>)`` resolves on any hub;
- every record's ``cwd`` contains the job id, for ``history_recall(cwd=...)``;
- one ``role: "job"`` header record per job carries the job id, owning Duet
  channel, Studio task key and the quoted ask *in its text*, so
  ``history_recall(<job id>, mode="text")`` finds the job;
- each wire line carries a ``job`` object with the same ids (hubs that do not
  know it ignore it), and each batch file is named ``<job_id>-...jsonl``.

``ship-jobs --final <job_id>`` is the runner's last step before it marks a job
done. It waits for the ship lock, ships the job with a seal record, and exits
0 only when every record of the job is confirmed delivered: rsync removes a
batch from the outbox only after the hub wrote it and its checksum matched.
See docs/design/claude-jobs-shipper.md.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterator

from . import config, ship, textproc, trigger
from .adapters import Record
from .adapters.claude_code import SKIP_PREFIXES, ClaudeCodeAdapter, _texts
from .textproc import MIN_CHARS, scrub

AGENT = "claude-code"
JOB_ID = re.compile(r"^cj_(\d{8}T\d{6}Z)_([0-9a-f]{6})$")

EXIT_OK = 0
EXIT_UNCONFIRMED = 1
EXIT_CONFIG = 2
EXIT_BUSY = 3

DEFAULT_WAIT = 300           # seconds ship-final waits for the ship lock
RETRY_DELAYS = (5, 15)       # between upload attempts in --final mode
MAX_ASK_CHARS = 4000

_START_KEYS = ("started_at", "start_time", "created_at", "queued_at",
               "submitted_at", "started", "created", "queued", "submitted")
_END_KEYS = ("finished_at", "completed_at", "ended_at", "done_at",
             "finished", "completed", "ended", "done")


class JobError(Exception):
    """A job directory that cannot be shipped as it stands."""


# ------------------------------------------------------------------ the job

@dataclasses.dataclass
class Job:
    job_id: str
    path: Path
    meta: dict

    def field(self, *names: str) -> str:
        for name in names:
            value = self.meta.get(name)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        return ""

    @property
    def channel(self) -> str:
        return self.field("duet_channel_id", "channel_id", "owning_channel")

    @property
    def task_key(self) -> str:
        return self.field("studio_task_key", "task_key")

    @property
    def transcript(self) -> Path:
        return self.path / "transcript.jsonl"

    def _time(self, keys: tuple[str, ...]) -> datetime.datetime | None:
        nested = self.meta.get("timestamps")
        pools = [self.meta] + ([nested] if isinstance(nested, dict) else [])
        for pool in pools:
            for key in keys:
                parsed = _parse_time(pool.get(key))
                if parsed:
                    return parsed
        return None

    def started(self) -> datetime.datetime:
        """When the job started: job.json if it says, else the stamp inside
        the job id, which is always there and never changes."""
        found = self._time(_START_KEYS)
        if found:
            return found
        stamp = JOB_ID.match(self.job_id).group(1)
        return datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc)

    def finished(self) -> datetime.datetime | None:
        return self._time(_END_KEYS)

    def cwd(self, real: str) -> str:
        """A cwd that contains the job id: the real one when it already does
        (the job's own worktree), else the job directory."""
        return real if real and self.job_id in real else str(self.path)


def load_job(path: Path) -> Job:
    job_id = path.name
    if not JOB_ID.match(job_id):
        raise JobError(f"{path.name}: not a job id (cj_<yyyymmddThhmmssZ>_<6 hex>)")
    try:
        meta = json.loads((path / "job.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JobError(f"{job_id}: no job.json") from None
    except (OSError, ValueError) as exc:
        raise JobError(f"{job_id}: unreadable job.json ({exc})") from None
    if not isinstance(meta, dict):
        raise JobError(f"{job_id}: job.json is not an object")
    if meta.get("job_id") not in (None, job_id):
        raise JobError(f"{job_id}: job.json names another job ({meta.get('job_id')!r})")
    return Job(job_id=job_id, path=path, meta=meta)


def job_dirs(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir()
                      if JOB_ID.match(p.name) and p.is_dir() and not p.is_symlink())
    except OSError:
        return []


# --------------------------------------------------------------- the records

@dataclasses.dataclass
class Collected:
    """Everything one job ships: records in shipping order and what the
    seal needs to describe them."""

    records: list[tuple[Record, dict]]
    sources: list[Path]
    sessions: list[str]
    result: str          # the transcript's result subtype, or "missing"
    worktree: str
    model: str


def _parse_time(value: object) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _transcript_lines(job: Job) -> list[dict]:
    try:
        handle = job.transcript.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            out.append(entry if isinstance(entry, dict) else {})
    return out


_CLAUDE_SID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _session_ids(job: Job, lines: list[dict]) -> list[str]:
    """The job's Claude Code session uuids: job.json first (the runner's
    ``session_id`` and ``claude_session_ids``), then the transcript's."""
    found: list[str] = []
    candidates = [job.meta.get("session_id")]
    for key in ("claude_session_ids", "session_ids"):
        listed = job.meta.get(key)
        candidates += listed if isinstance(listed, list) else []
    candidates += [entry.get("session_id") for entry in lines]
    for sid in candidates:
        if isinstance(sid, str) and _CLAUDE_SID.match(sid) and sid not in found:
            found.append(sid)
    return found


def _with_claude(extra: dict, record: Record, claude_sid: object) -> tuple[Record, dict]:
    """Attach the real Claude Code session uuid: a structured field on the
    wire line, and a ``claude`` link alias so a hub that knows the kind can
    resolve history_show(session=<uuid>)."""
    if not (isinstance(claude_sid, str) and _CLAUDE_SID.match(claude_sid)):
        return record, extra
    aliases = tuple(record.aliases) + (("claude", claude_sid),)
    return (dataclasses.replace(record, aliases=aliases),
            {**extra, "claude_session_id": claude_sid})


def _with_claude_list(extra: dict, record: Record, sids: list[str]) -> tuple[Record, dict]:
    """For header and seal records: every uuid of the conversation."""
    sids = [s for s in sids if _CLAUDE_SID.match(s)][:16]
    if not sids:
        return record, extra
    aliases = tuple(record.aliases) + tuple(("claude", s) for s in sids)
    return (dataclasses.replace(record, aliases=aliases),
            {**extra, "claude_session_id": sids[0], "claude_session_ids": sids})


def _session_files(root: Path, session_ids: list[str]) -> list[Path]:
    files: list[Path] = []
    if not root.is_dir():
        return files
    for sid in session_ids:
        files += sorted(root.glob(f"*/{sid}.jsonl"))
        files += sorted(root.glob(f"*/{sid}/**/*.jsonl"))  # subagent side chains
    seen: set[Path] = set()
    return [f for f in files if not (f in seen or seen.add(f))]


def _file_records(path: Path) -> list[Record]:
    """Messages of one Claude Code session file, parsed as the adapter does."""
    return list(ClaudeCodeAdapter()._read_transcript(path))


def collect(job: Job, projects: Path) -> Collected:
    """Every record of ``job``, header first, session files before the
    transcript (so the hub keeps their real timestamps for shared text)."""
    lines = _transcript_lines(job)
    sids = _session_ids(job, lines)
    session_files = _session_files(projects, sids)
    base = job.started()
    worktree = job.field("worktree", "cwd")
    model = ""
    out: list[tuple[Record, dict]] = []
    extra = header_object(job)
    latest = ""

    for path in session_files:
        for record in _file_records(path):
            worktree = worktree or record.cwd
            latest = max(latest, record.timestamp or "")
            out.append(_with_claude(extra, dataclasses.replace(
                record, session_id=job.job_id, cwd=job.cwd(record.cwd)),
                record.session_id))

    result = "missing"
    origin = str(job.transcript)
    for number, entry in enumerate(lines, 1):
        kind = entry.get("type")
        stamp = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else ""
        synthetic = _iso(base + datetime.timedelta(milliseconds=number))
        if kind == "system" and entry.get("subtype") == "init":
            model = model or str(entry.get("model") or "")
            worktree = worktree or str(entry.get("cwd") or "")
            continue
        if kind == "result":
            result = str(entry.get("subtype") or ("error" if entry.get("is_error") else "success"))
            finished = job.finished()
            when = (_iso(finished) if finished else latest) or synthetic
            out.append(_with_claude(extra, Record(
                agent=AGENT, origin=origin, session_id=job.job_id,
                cwd=job.cwd(worktree), timestamp=stamp or when, role="result",
                text=_result_text(entry)), entry.get("session_id")))
            continue
        if kind not in ("user", "assistant"):
            continue
        for text in _texts(entry.get("message") or {}):
            text = text.strip()
            if len(text) < MIN_CHARS or text.startswith(SKIP_PREFIXES):
                continue
            out.append(_with_claude(extra, Record(
                agent=AGENT, origin=origin, session_id=job.job_id,
                cwd=job.cwd(worktree), timestamp=stamp or synthetic, role=kind,
                text=scrub(text)), entry.get("session_id")))

    header = Record(agent=AGENT, origin=str(job.path / "job.json"),
                    session_id=job.job_id, cwd=job.cwd(worktree),
                    timestamp=_iso(base), role="job",
                    text=header_text(job, sids, worktree, model))
    sources = ([job.transcript] if job.transcript.is_file() else []) + session_files
    return Collected(records=[_with_claude_list(extra, header, sids)] + out,
                     sources=sources, sessions=sids, result=result,
                     worktree=worktree, model=model)


def header_object(job: Job, **more: str) -> dict:
    """The ``job`` object every wire line of this job carries."""
    obj = {"job_id": job.job_id, "duet_channel_id": job.channel,
           "studio_task_key": job.task_key, "kind": "job"}
    obj.update(more)
    return {"job": obj}


def header_text(job: Job, sessions: list[str], worktree: str, model: str) -> str:
    """The searchable text of the job's header record."""
    ask = job.field("ask", "prompt")
    if len(ask) > MAX_ASK_CHARS:
        ask = ask[:MAX_ASK_CHARS].rstrip() + " …"
    lines = [f"[claude-jobs job] {job.job_id}"]
    for label, value in (("Status", job.field("status")),
                         ("Duet channel", job.channel),
                         ("Studio task", job.task_key),
                         ("Repo", job.field("repo")),
                         ("Branch", job.field("branch")),
                         ("Worktree", worktree),
                         ("Claude session", ", ".join(sessions)),
                         ("Model", model)):
        if value:
            lines.append(f"{label}: {value}")
    if ask:
        lines += ["Ask:", ask]
    return "\n".join(lines)


def _result_text(entry: dict) -> str:
    facts = [str(entry.get("subtype") or "result")]
    if entry.get("is_error"):
        facts.append("is_error")
    for key, label in (("num_turns", "turns"), ("duration_ms", "ms"),
                       ("total_cost_usd", "usd")):
        if isinstance(entry.get(key), (int, float)):
            facts.append(f"{label}={entry[key]}")
    body = entry.get("result")
    head = "[claude -p result] " + " ".join(facts)
    return head + ("\n" + body.strip() if isinstance(body, str) and body.strip() else "")


def _digest(paths: list[Path], root: Path) -> str:
    """One sha256 over the source files (name and content), stable order."""
    total = hashlib.sha256()
    for path in sorted(paths, key=str):
        try:
            name = str(path.relative_to(root))
        except ValueError:
            name = path.name
        total.update(name.encode("utf-8") + b"\0")
        try:
            total.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            total.update(b"unreadable")
    return total.hexdigest()


def conversations(job: Job, found: Collected) -> list[str]:
    """Every hub session this job is filed under: the job itself, then one
    per Boat VM (piece B)."""
    vms = getattr(found, "vms", None) or {}
    return [job.job_id] + [f"{job.job_id}/vm/{vm}" for vm in vms]


def seal_record(job: Job, found: Collected, projects: Path) -> Record:
    """The final ship's statement of what it shipped. Deterministic for the
    same sources, so a repeated ship-final adds nothing."""
    counts = Counter("transcript" if r.origin == str(job.transcript)
                     else "header" if r.role == "job" else "session"
                     for r, _ in found.records)
    stamps = [r.timestamp for r, _ in found.records if r.timestamp]
    finished = job.finished()
    when = _iso(finished) if finished else (max(stamps) if stamps else _iso(job.started()))
    text = "\n".join([
        f"[claude-jobs final] {job.job_id} complete",
        f"Status: {job.field('status') or 'unknown'}",
        f"Records: {len(found.records)} (session files {counts['session']}, "
        f"transcript.jsonl {counts['transcript']}, header {counts['header']})",
        f"Transcript result: {found.result}",
        f"Sessions: {', '.join(conversations(job, found))}",
        f"Claude sessions: {', '.join(found.sessions) or 'none'}",
        f"Sources: {len(found.sources)} file(s), sha256 "
        f"{_digest(found.sources, projects if projects.is_dir() else job.path)}",
    ])
    return Record(agent=AGENT, origin=str(job.path / "job.json"),
                  session_id=job.job_id, cwd=job.cwd(found.worktree),
                  timestamp=when, role="job", text=text)


# ----------------------------------------------------------------------- run

def run(final: str | None = None, *, dry_run: bool = False,
        wait: int = DEFAULT_WAIT) -> int:
    dest = config.hub_ingest()
    if not dest and not dry_run:
        print("ship-jobs: AGENT_HISTORY_HUB_INGEST is not set (an rsync "
              "destination such as ahub@host:)", file=sys.stderr)
        return EXIT_CONFIG
    root = config.jobs_dir()
    if final is not None:
        if not JOB_ID.match(final):
            print(f"ship-jobs: {final!r} is not a job id "
                  "(cj_<yyyymmddThhmmssZ>_<6 hex>)", file=sys.stderr)
            return EXIT_CONFIG
        try:
            load_job(root / final)
        except JobError as exc:
            print(f"ship-jobs: {exc} (in {root})", file=sys.stderr)
            return EXIT_CONFIG
    if dry_run:
        return _run_locked(dest, root, final, True)
    config.ensure_data_home()
    timeout = max(1, int(wait)) if final else trigger.LOCK_TIMEOUT
    with trigger._exclusive(config.ship_lock_path(), timeout=timeout) as acquired:
        if not acquired:
            if final:
                print(f"ship-jobs: another ship held the lock for {timeout}s; "
                      f"{final} is not confirmed", file=sys.stderr)
                return EXIT_BUSY
            print("ship-jobs: another run is in progress; skipping")
            return EXIT_OK
        config.secure_file(config.ship_lock_path())
        return _run_locked(dest, root, final, False)


def _run_locked(dest: str | None, root: Path, final: str | None,
                dry_run: bool) -> int:
    state = ship.State.open(writable=not dry_run)
    projects = config.claude_projects_dir()
    try:
        outbox = config.outbox_dir()
        if not dry_run:
            outbox = ship._outbox()
            for stale in outbox.glob(".*.part"):
                stale.unlink(missing_ok=True)
            state.settle(outbox)

        targets = [root / final] if final else job_dirs(root)
        expected: set[str] = set()
        new = files = 0
        redacted: Counter[str] = Counter()
        shipped_jobs = 0
        for path in targets:
            try:
                job = load_job(path)
            except JobError as exc:
                print(f"ship-jobs: skipping {exc}", file=sys.stderr)
                if final:
                    return EXIT_CONFIG
                continue
            found = collect(job, projects)
            records = list(found.records)
            if final:
                records.append(_with_claude_list(
                    header_object(job), seal_record(job, found, projects),
                    found.sessions))
            batches = ship.Batches(state, outbox, dry_run, prefix=f"{job.job_id}-")
            for record, extra in records:
                if textproc.is_injection(record.text):
                    continue
                digest = batches.add(
                    agent=record.agent, origin=record.origin,
                    session_id=record.session_id, cwd=record.cwd,
                    timestamp=record.timestamp, role=record.role,
                    text=record.text, aliases=record.aliases, extra=extra)
                if final:
                    expected.add(digest)
            batches.flush()
            new += batches.new
            files += batches.files
            redacted.update(batches.redacted)
            shipped_jobs += 1

        verb = "would ship" if dry_run else "queued"
        print(f"ship-jobs: {new} new record(s) from {shipped_jobs} job(s) {verb}"
              + ("" if dry_run else f" in {files} batch(es)"))
        if redacted:
            print("  redacted: " + ", ".join(
                f"{n} {cls}" for cls, n in redacted.most_common()))
        if dry_run:
            return EXIT_OK

        pattern = f"{final}-*.jsonl" if final else "*.jsonl"
        ok = _upload(outbox, pattern, dest, attempts=1 + (len(RETRY_DELAYS) if final else 0))
        settled = state.settle(outbox)
        left = sorted(outbox.glob(pattern))
        print(f"ship-jobs: uploaded {settled} batch(es)"
              + (f"; {len(left)} left in the outbox for the next run" if left else ""))
        if not final:
            return EXIT_OK if ok and not left else EXIT_UNCONFIRMED

        missing = [d for d in expected if not state.shipped(d)]
        if ok and not left and not missing:
            print(f"ship-jobs: {final} confirmed: the hub holds all "
                  f"{len(expected)} record(s), seal included")
            return EXIT_OK
        print(f"ship-jobs: {final} NOT confirmed: {len(missing)} record(s) "
              f"unconfirmed, {len(left)} batch(es) still queued", file=sys.stderr)
        return EXIT_UNCONFIRMED
    finally:
        state.close()


def _upload(outbox: Path, pattern: str, dest: str, *, attempts: int) -> bool:
    """Upload the matching batches, retrying what is left. True when every
    attempt's files went up (or there was nothing to send)."""
    for attempt in range(attempts):
        files = sorted(outbox.glob(pattern))
        if not files:
            return True
        if attempt:
            time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
        try:
            ok = ship.upload(files, dest, config.hub_key())
        except Exception as exc:  # noqa: BLE001 - a transport must not lose state
            print(f"ship-jobs: upload failed: {exc}", file=sys.stderr)
            ok = False
        if ok:
            for batch in files:  # delivered, whether or not the transport removed them
                batch.unlink(missing_ok=True)
            return True
    return False
