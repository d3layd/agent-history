"""Reach a hub over ssh — the only network path agent-history has.

`AGENT_HISTORY_REMOTE=ssh://user@host[:port]` makes `search` and `show` run on
the hub. Nothing new listens anywhere: the hub's sshd runs the command, and the
key it accepts for this should be pinned to `command="agent-history serve-ssh"`
(see ``serve_ssh``), so the key can search and nothing else.

The command travels as one shell-quoted string — ssh joins its arguments with
spaces and the far side splits them again, so quoting is what keeps a query
like ``rm -rf; echo "$HOME"`` a query.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import urllib.parse

from . import config

CONNECT_TIMEOUT = 10   # seconds to open the connection
QUERY_TIMEOUT = 60     # seconds for a whole hub query before giving up


class RemoteError(RuntimeError):
    """The hub could not be reached or did not answer usefully."""


def target() -> tuple[str, str | None]:
    """(``user@host`` or ``host``, port) from AGENT_HISTORY_REMOTE."""
    url = config.remote_url()
    if not url:
        raise RemoteError("AGENT_HISTORY_REMOTE is not set")
    try:
        parsed = urllib.parse.urlparse(url)
        host, user, port = parsed.hostname, parsed.username, parsed.port
    except ValueError as exc:
        raise RemoteError(f"bad AGENT_HISTORY_REMOTE {url!r}: {exc}") from exc
    if parsed.scheme != "ssh" or not host:
        raise RemoteError(
            f"AGENT_HISTORY_REMOTE must look like ssh://user@host[:port], got {url!r}")
    # A leading '-' would be read by ssh as an option (-oProxyCommand=...).
    for part in (host, user or ""):
        if part.startswith("-") or any(c.isspace() or ord(c) < 32 for c in part):
            raise RemoteError(f"bad AGENT_HISTORY_REMOTE {url!r}")
    return (f"{user}@{host}" if user else host), (str(port) if port else None)


def ssh_argv(command: list[str]) -> list[str]:
    """The full ssh invocation that runs ``command`` on the hub."""
    destination, port = target()
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    key = config.remote_key()
    if key:
        argv += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if port:
        argv += ["-p", port]
    return [*argv, "--", destination, shlex.join(command)]


def search_args(query: str, *, k: int, cwd: str | None, agent: str | None,
                machine: str | None, exclude_machine: list[str] | None,
                mode: str, sort: str, full: bool, as_json: bool) -> list[str]:
    """The ``search`` command line to run on the hub for these options."""
    out = ["search", "-k", str(k), "--mode", mode, "--sort", sort]
    for flag, value in (("--cwd", cwd), ("--agent", agent), ("--machine", machine)):
        if value:
            out += [flag, value]
    for name in exclude_machine or ():
        out += ["--exclude-machine", name]
    if full:
        out.append("--full")
    if as_json:
        out.append("--json")
    return [*out, "--", query]


def show_args(target_: str, *, around: str | None, limit: int,
              as_json: bool) -> list[str]:
    out = ["show", "--limit", str(limit)]
    if around:
        out += ["--around", around]
    if as_json:
        out.append("--json")
    return [*out, "--", target_]


def passthrough(command: list[str]) -> int:
    """Run ``command`` on the hub with its output going straight to ours."""
    try:
        argv = ssh_argv(command)
    except RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL,
                              timeout=QUERY_TIMEOUT * 2).returncode
    except subprocess.TimeoutExpired:
        print("error: the hub did not answer in time", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot run ssh: {exc}", file=sys.stderr)
        return 1


def query_json(command: list[str]) -> list[dict]:
    """Run a ``--json`` command on the hub and return the parsed list.

    Raises RemoteError for anything short of a clean answer, so callers can
    fail open with one line rather than a traceback.
    """
    argv = ssh_argv(command)
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              timeout=QUERY_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RemoteError("timed out") from exc
    except OSError as exc:
        raise RemoteError(f"cannot run ssh: {exc}") from exc
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines()
        reason = lines[-1] if lines else f"exit {done.returncode}"
        raise RemoteError(reason)
    try:
        data = json.loads(done.stdout)
    except ValueError as exc:
        raise RemoteError("unreadable answer") from exc
    if not isinstance(data, list):
        raise RemoteError("unexpected answer")
    return [row for row in data if isinstance(row, dict) and "text" in row]
