"""Semantic search over the index."""
from __future__ import annotations

import json

from . import embed, store

PREVIEW_CHARS = 600
JSON_PREVIEW_CHARS = 500


def run(
    query: str,
    *,
    k: int = 8,
    cwd: str | None = None,
    agent: str | None = None,
    full: bool = False,
    as_json: bool = False,
) -> int:
    db = store.open_for_read()
    vector = embed.pack(embed.embed(query, timeout=60))
    rows = store.query(db, vector, k, cwd=cwd, agent=agent)

    if as_json:
        print(json.dumps([_as_dict(row, full) for row in rows], indent=2))
        return 0

    if not rows:
        print("(no matches)")
        return 0

    for position, row in enumerate(rows, 1):
        _print_row(position, row, full)
    return 0


def _as_dict(row: tuple, full: bool) -> dict:
    agent, origin, session_id, cwd, timestamp, role, text, distance = row
    return {
        "agent": agent,
        "file": origin,
        "session_id": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
        "role": role,
        "text": text if full else text[:JSON_PREVIEW_CHARS],
        "distance": distance,
    }


def _print_row(position: int, row: tuple, full: bool) -> None:
    agent, origin, _session, cwd, timestamp, role, text, distance = row
    print(
        f"#{position}  dist={distance:.2f}  {timestamp[:16]}  "
        f"{agent}/{role}  {cwd or '?'}"
    )
    print(f"    file: {origin}")
    if full:
        print("    ---")
        for line in text.splitlines():
            print(f"    {line}")
    else:
        print(f"    {text[:PREVIEW_CHARS].replace(chr(10), ' ')}")
    print()
