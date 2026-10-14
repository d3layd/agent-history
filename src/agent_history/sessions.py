"""List the most recent sessions in the index.

"What were the last N sessions?" otherwise means hand-written SQL. A session
is a session id; its last-active time is its newest chunk, and its summary
line is the first thing the user asked in it.

Memory files, docs and subagent transcripts are left out by default: they
share the session machinery without being conversations someone had, and
memory files alone would crowd the list (each edit bumps its timestamp).
"""
from __future__ import annotations

import datetime
import json

from . import search, store

PROMPT_CHARS = 100


def recent(*, limit: int = 20, cwd: str | None = None,
           agent: str | None = None, include_pseudo: bool = False) -> list[dict]:
    """The `limit` most recently active sessions, newest first."""
    limit = max(1, limit)
    db = store.open_for_read(check_model=False)  # listing never embeds
    summaries = store.session_summaries(
        db, cwd=cwd, agent=agent, include_pseudo=include_pseudo)
    # Adapters store a mix of UTC-suffixed and local-naive timestamps, so
    # order by the normalised instant, not the string (same rule as search).
    summaries.sort(key=lambda row: search._instant(row[3] or ""), reverse=True)
    summaries = summaries[:limit]

    prompts = store.first_prompts(
        db, [row[1] for row in summaries], include_pseudo=include_pseudo)
    return [
        {
            "last_active": timestamp,
            "agent": agent_name,
            "machine": machine,
            "session_id": session_id,
            "cwd": session_cwd,
            "first_prompt": prompts.get(session_id),
        }
        for agent_name, session_id, session_cwd, timestamp, machine in summaries
    ]


def run(*, limit: int = 20, cwd: str | None = None, agent: str | None = None,
        include_pseudo: bool = False, as_json: bool = False) -> int:
    rows = recent(limit=limit, cwd=cwd, agent=agent,
                  include_pseudo=include_pseudo)

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print("(no sessions)")
        return 0

    surfaces = [_surface(row) for row in rows]
    width = max(len(s) for s in surfaces)
    for row, surface in zip(rows, surfaces):
        print(f"{_local(row['last_active'])}  {surface:<{width}}  "
              f"{row['session_id']}  {row['cwd'] or '?'}  "
              f"{_one_line(row['first_prompt'])}")
    return 0


def _surface(row: dict) -> str:
    """agent, plus @machine when the index records one (0.17 hub)."""
    if row["machine"]:
        return f"{row['agent']}@{row['machine']}"
    return row["agent"]


def _local(timestamp: str | None) -> str:
    """A stored timestamp as local 'YYYY-MM-DD HH:MM', whatever its flavour."""
    instant = search._instant(timestamp or "")
    if instant == float("-inf"):
        return (timestamp or "?")[:16].ljust(16)
    return datetime.datetime.fromtimestamp(instant).strftime("%Y-%m-%d %H:%M")


def _one_line(text: str | None) -> str:
    if not text:
        return "(no user prompt indexed)"
    flat = " ".join(text.split())
    if len(flat) > PROMPT_CHARS:
        return flat[:PROMPT_CHARS - 1] + "…"
    return flat
