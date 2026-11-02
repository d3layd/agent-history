"""Show the conversation around a search hit.

Search returns isolated chunks; the question they raise is "what was said
around that?". `show` answers it from the index itself rather than the source
transcript — deliberately, because for a mature index most transcripts no
longer exist on disk, and the index is the only surviving copy.

The target is whatever a search result printed: an origin (file path or
db:... reference), a session uid, or any id the session is known by — the
raw source id (what `claude --resume` takes) or a linked alias such as a
remote-control `session_…` id. Linked sessions are shown as one.
"""
from __future__ import annotations

import json
import sys

from . import config, remote, sessions, store

PER_CHUNK_CHARS = 2000


def rows_for(target: str, *, around: str | None = None,
             limit: int = 40) -> list[dict]:
    """Chunks of the conversation `target` belongs to, oldest first."""
    limit = max(1, limit)  # limit=0 would make rows[-limit:] return everything
    db = store.open_for_read(check_model=False)  # show never embeds
    uid = sessions.resolve_session(db, target)
    if uid:
        rows = store.session_rows(db, session_uid=uid)
    else:
        # An origin path, or an id this index cannot pin to one session
        # (a pseudo id shared by several files): match it as stored.
        rows = store.session_rows(db, origin=target, session_id=target)
    if not rows:
        return []

    if around and len(rows) > limit:
        # Centre the window on the chunk nearest the given timestamp.
        centre = max(
            range(len(rows)),
            key=lambda i: _prefix_len(rows[i][4] or "", around),
        )
        start = max(0, min(centre - limit // 2, len(rows) - limit))
        rows = rows[start:start + limit]
    elif len(rows) > limit:
        rows = rows[-limit:]  # the end of a conversation is usually the point

    return [
        {
            "agent": agent,
            "file": origin,
            "session_id": session_id,
            "session_uid": session_uid,
            "cwd": cwd,
            "timestamp": timestamp,
            "role": role,
            "text": text,
            "machine": machine or config.machine_id(),
        }
        for (agent, origin, session_id, cwd, timestamp, role, text,
             machine, session_uid) in rows
    ]


def run(target: str, *, around: str | None = None, limit: int = 40,
        as_json: bool = False, where: str | None = None,
        tenant: str | None = None) -> int:
    """``where`` is None (local first, then the hub), "local", or "hub"."""
    configured = config.remote_url()
    if where == "hub" and not configured:
        print("error: --hub needs AGENT_HISTORY_REMOTE=ssh://user@host",
              file=sys.stderr)
        return 1
    if tenant and (not configured or where == "local"):
        print("error: --tenant selects an index on a hub; set "
              "AGENT_HISTORY_REMOTE", file=sys.stderr)
        return 1
    rows: list[dict] = []
    if where == "local" or not configured or (
            where is None and not tenant and config.db_path().exists()):
        rows = rows_for(target, around=around, limit=limit)
    if not rows and configured and where != "local":
        # A hit from another machine is not in the local index; the hub has it.
        return remote.passthrough(remote.show_args(
            target, around=around, limit=limit, as_json=as_json, tenant=tenant))
    return render(target, rows, as_json=as_json)


def render(target: str, rows: list[dict], *, as_json: bool,
           tenant: str | None = None) -> int:
    """Print a conversation. ``tenant`` is labelled when a hub serves several."""
    if not rows:
        print(f"(nothing indexed for {target!r} — pass an origin path or "
              "session id exactly as search printed it)")
        return 1

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0

    first = rows[0]
    elsewhere = (f"  machine={first['machine']}"
                 if first["machine"] != config.machine_id() else "")
    if tenant:
        elsewhere += f"  tenant={tenant}"
    print(f"{first['agent']}  session={first['session_id']}  "
          f"cwd={first['cwd'] or '?'}{elsewhere}")
    if first.get("session_uid"):
        print(f"uid: {first['session_uid']}")
    print(f"origin: {first['file']}")
    print()
    for row in rows:
        stamp = (row["timestamp"] or "")[:19]
        print(f"[{stamp}] {row['role']}:")
        for line in row["text"][:PER_CHUNK_CHARS].splitlines():
            print(f"  {line}")
        if len(row["text"]) > PER_CHUNK_CHARS:
            print("  [... chunk truncated — use --json for full text]")
        print()
    return 0


def _prefix_len(a: str, b: str) -> int:
    """Shared-prefix length — a proximity proxy that works because ISO 8601
    timestamps compare lexicographically."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n
