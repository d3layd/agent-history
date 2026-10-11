"""Show the conversation around a search hit.

Search returns isolated chunks; the question they raise is "what was said
around that?". `show` answers it from the index itself rather than the source
transcript — deliberately, because for a mature index most transcripts no
longer exist on disk, and the index is the only surviving copy.

The target is whatever a search result printed: an origin (file path or
db:... reference) or a session id.
"""
from __future__ import annotations

import json
import sys

from . import config, remote, store

PER_CHUNK_CHARS = 2000


def rows_for(target: str, *, around: str | None = None,
             limit: int = 40) -> list[dict]:
    """Chunks of the conversation `target` belongs to, oldest first."""
    limit = max(1, limit)  # limit=0 would make rows[-limit:] return everything
    db = store.open_for_read(check_model=False)  # show never embeds
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
            "cwd": cwd,
            "timestamp": timestamp,
            "role": role,
            "text": text,
            "machine": machine or config.machine_id(),
        }
        for (agent, origin, session_id, cwd, timestamp, role, text,
             machine) in rows
    ]


def run(target: str, *, around: str | None = None, limit: int = 40,
        as_json: bool = False, where: str | None = None) -> int:
    """``where`` is None (local first, then the hub), "local", or "hub"."""
    configured = config.remote_url()
    if where == "hub" and not configured:
        print("error: --hub needs AGENT_HISTORY_REMOTE=ssh://user@host",
              file=sys.stderr)
        return 1
    rows: list[dict] = []
    if where == "local" or not configured or (
            where is None and config.db_path().exists()):
        rows = rows_for(target, around=around, limit=limit)
    if not rows and configured and where != "local":
        # A hit from another machine is not in the local index; the hub has it.
        return remote.passthrough(remote.show_args(
            target, around=around, limit=limit, as_json=as_json))
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
    print(f"{first['agent']}  session={first['session_id']}  "
          f"cwd={first['cwd'] or '?'}{elsewhere}")
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
