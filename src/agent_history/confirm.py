"""`agent-history confirm-indexed --job <job_id>` — has the hub indexed a job?

`ship-final` (run as claude-jobs) proves arrival. This proves indexing: the
job's header, its seal, and every conversation the seal lists are findable by
job id, with the same functions the hub's MCP tools use (`search.search_rows`
behind `history_recall`, `show.rows_for` behind `history_show`).

It runs on Clamps' side, never as claude-jobs: jobs run arbitrary code, and a
read credential there would expose all of the user's history to every job.
The read path is the one Clamps already has: `AGENT_HISTORY_REMOTE` and a key
pinned to `agent-history serve-ssh` (search and show only, tenants fixed by
the hub's admin). Without a remote it reads the local index (on the hub, and
in tests).

Exit codes: 0 indexed · 1 not yet or partial · 2 config or credential error.
See docs/design/claude-jobs-shipper.md.
"""
from __future__ import annotations

import datetime
import getpass
import json
import os
import re
import sys
from pathlib import Path

from . import config, remote, search, show, store

EXIT_INDEXED = 0
EXIT_PENDING = 1
EXIT_CONFIG = 2

JOB_ID = re.compile(r"^cj_\d{8}T\d{6}Z_[0-9a-f]{6}$")
DENIED_USERS = ("claude-jobs",)
SEARCH_K = 50

# ssh / serve-ssh answers that mean "fix the setup", not "try again later".
_CONFIG_HINTS = ("permission denied", "rejected", "not permitted", "not set",
                 "must look like", "bad agent_history_remote", "cannot run ssh",
                 "host key verification", "no such identity", "invalid tenant",
                 "identity file", "no tenants")


class ConfigError(Exception):
    """No usable read path: fix configuration or credentials."""


class Pending(Exception):
    """The hub could not answer right now (timeout, connection refused)."""


# ------------------------------------------------------------------ readers

class LocalReader:
    """This machine's own index (the hub itself, or a test hub)."""

    where = "local"

    def search(self, query: str, *, cwd: str | None = None, k: int = SEARCH_K) -> list[dict]:
        if not config.db_path().exists():
            return []
        try:
            return search.search_rows(query, k=k, cwd=cwd, mode="text")
        except store.StoreError as exc:
            raise ConfigError(str(exc)) from None

    def show(self, session: str, *, limit: int = 1) -> list[dict]:
        if not config.db_path().exists():
            return []
        try:
            return show.rows_for(session, limit=limit)
        except store.StoreError as exc:
            raise ConfigError(str(exc)) from None


class RemoteReader:
    """The hub over the read-only ssh key (``serve-ssh``)."""

    where = "hub"

    def __init__(self, tenant: str | None = None) -> None:
        self.tenant = tenant
        try:
            remote.target()
        except remote.RemoteError as exc:
            raise ConfigError(str(exc)) from None

    def _query(self, command: list[str], *, empty_ok: bool) -> list[dict]:
        try:
            return remote.query_json(command)
        except remote.RemoteError as exc:
            reason = str(exc)
            low = reason.lower()
            if any(hint in low for hint in _CONFIG_HINTS):
                raise ConfigError(f"the hub refused the read: {reason}") from None
            if empty_ok and re.fullmatch(r"exit 1", reason):
                return []  # `show` exits 1 with nothing on stderr when nothing is indexed
            raise Pending(f"the hub did not answer: {reason}") from None

    def search(self, query: str, *, cwd: str | None = None, k: int = SEARCH_K) -> list[dict]:
        return self._query(remote.search_args(
            query, k=k, cwd=cwd, agent=None, machine=None, exclude_machine=None,
            mode="text", sort="relevance", full=True, as_json=True,
            tenant=self.tenant), empty_ok=False)

    def show(self, session: str, *, limit: int = 1) -> list[dict]:
        return self._query(remote.show_args(session, around=None, limit=limit,
                                            as_json=True, tenant=self.tenant),
                           empty_ok=True)


# -------------------------------------------------------------------- check

def _starts(rows: list[dict], prefix: str) -> list[dict]:
    return [r for r in rows if (r.get("text") or "").startswith(prefix)]


def _field(text: str, label: str) -> list[str]:
    for line in text.splitlines():
        if line.startswith(label + ":"):
            value = line.split(":", 1)[1].strip()
            if value and value != "none":
                return [v.strip() for v in value.split(",") if v.strip()]
    return []


def check(job_id: str, reader) -> dict:
    """Every check, by job id. Raises ConfigError / Pending from the reader."""
    hits = reader.search(job_id, cwd=job_id)
    headers = _starts(hits, f"[claude-jobs job] {job_id}")
    seals = _starts(hits, f"[claude-jobs final] {job_id}")
    vm_headers = _starts(hits, f"[claude-jobs vm] {job_id} vm ")
    latest = max(seals, key=lambda r: (r.get("timestamp") or "",
                                       len(_field(r.get("text") or "", "Sessions"))),
                 default=None)
    seal_text = (latest or {}).get("text") or ""
    sessions = _field(seal_text, "Sessions") or [job_id]
    claude_ids = _field(seal_text, "Claude sessions")

    checks: dict[str, bool] = {"header": bool(headers), "seal": bool(latest)}
    rows = reader.show(job_id, limit=1)
    checks[f"show {job_id}"] = bool(rows) and rows[0].get("session_id") == job_id
    for session in sessions:
        if session == job_id:
            continue
        vm = session.split("/vm/", 1)[1] if "/vm/" in session else session
        vm_rows = reader.show(session, limit=1)
        checks[f"show {session}"] = bool(vm_rows) and vm_rows[0].get("session_id") == session
        checks[f"vm header {vm}"] = bool(_starts(vm_headers, f"[claude-jobs vm] {job_id} vm {vm}"))
    lookup: dict[str, bool] = {}
    for uuid in claude_ids:
        found = reader.search(uuid, k=10)
        checks[f"recall {uuid}"] = any(r.get("session_id") in sessions for r in found)
        by_show = reader.show(uuid, limit=1)
        lookup[uuid] = bool(by_show) and by_show[0].get("session_id") in sessions

    missing = [name for name, ok in checks.items() if not ok]
    if not missing:
        state = "indexed"
    elif not any(checks.values()):
        state = "pending"
    else:
        state = "partial"
    return {"job_id": job_id, "state": state, "where": reader.where,
            "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "sessions": sessions, "claude_session_ids": claude_ids,
            "checks": checks, "missing": missing,
            # history_show(session=<uuid>) needs the hub's `claude` alias kind;
            # reported, not gated, so an older hub does not block a job.
            "claude_lookup": lookup}


def _write_receipt(path: str, result: dict) -> None:
    target = Path(path)
    part = target.with_name(f".{target.name}.part")
    part.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(part, target)


def run(job_id: str, *, as_json: bool = False, receipt: str | None = None,
        tenant: str | None = None, reader=None) -> int:
    def fail(code: int, state: str, why: str) -> int:
        result = {"job_id": job_id, "state": state, "error": why}
        if as_json:
            print(json.dumps(result, indent=1))
        print(f"confirm-indexed: {why}", file=sys.stderr)
        return code

    if not JOB_ID.match(job_id or ""):
        return fail(EXIT_CONFIG, "error", f"{job_id!r} is not a job id")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry
        user = ""
    if user in DENIED_USERS:
        return fail(EXIT_CONFIG, "error",
                    f"refusing to run as {user}: that user stays upload-only")
    try:
        reader = reader or (RemoteReader(tenant) if config.remote_url() else LocalReader())
        if isinstance(reader, LocalReader) and not config.db_path().exists() \
                and not config.hub_mode() and tenant is None:
            raise ConfigError("no hub configured (AGENT_HISTORY_REMOTE) and no "
                              "local index to read")
        result = check(job_id, reader)
    except ConfigError as exc:
        return fail(EXIT_CONFIG, "error", str(exc))
    except Pending as exc:
        return fail(EXIT_PENDING, "pending", str(exc))

    if receipt:
        try:
            _write_receipt(receipt, result)
        except OSError as exc:
            return fail(EXIT_CONFIG, "error", f"cannot write the receipt: {exc}")
    if as_json:
        print(json.dumps(result, indent=1))
    ok = sum(result["checks"].values())
    summary = (f"{job_id}: {result['state']} ({ok}/{len(result['checks'])} checks"
               + (f"; missing: {', '.join(result['missing'])}" if result["missing"] else "")
               + ")")
    # With --json, stdout is the JSON alone; the one-line summary goes to stderr.
    indexed = result["state"] == "indexed"
    print(f"confirm-indexed: {summary}",
          file=sys.stdout if indexed and not as_json else sys.stderr)
    return EXIT_INDEXED if result["state"] == "indexed" else EXIT_PENDING
