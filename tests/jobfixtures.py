"""Builders for claude -p job directories, shared by the job tests.

A job looks the way the runner leaves it: ``<jobs>/<job_id>/job.json`` and
``transcript.jsonl`` (stream-json), plus a Claude Code session file under the
projects directory. Nothing here touches a network.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

JOB = "cj_20261101T120000Z_a1b2c3"
SID = "5f2b8c1e-0000-4000-8000-00000000aaaa"
GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])


def stream(kind: str, text: str = "", **extra) -> dict:
    """One stream-json line as claude -p prints it (no timestamp)."""
    if kind == "init":
        return {"type": "system", "subtype": "init", "session_id": SID,
                "cwd": f"/var/lib/claude-jobs/worktrees/{JOB}",
                "model": "claude-opus-5-5", **extra}
    if kind == "result":
        return {"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 7, "duration_ms": 61000, "total_cost_usd": 0.42,
                "result": text, "session_id": SID, **extra}
    return {"type": kind, "session_id": SID,
            "message": {"role": kind, "content": [{"type": "text", "text": text}]},
            **extra}


def session_line(role: str, text: str, ts: str, sid: str = SID,
                 cwd: str = f"/var/lib/claude-jobs/worktrees/{JOB}") -> dict:
    return {"type": role, "sessionId": sid, "cwd": cwd, "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]}}


def make_job(root: Path, projects: Path, *, job_id: str = JOB, meta: dict | None = None,
             transcript: list[dict] | None = None,
             session: list[dict] | None = None, subagent: list[dict] | None = None,
             sid: str = SID) -> Path:
    path = root / job_id
    path.mkdir(parents=True, exist_ok=True)
    data = {"job_id": job_id, "ask": "Fix the login redirect loop on staging",
            "duet_channel_id": "chan_7Hq2", "studio_task_key": "ATR-31",
            "repo": "lvtd-dgtl/storm-check", "branch": f"job/{job_id}",
            "status": "running",
            "timestamps": {"created_at": "2026-11-01T12:00:00Z"}}
    data.update(meta or {})
    (path / "job.json").write_text(json.dumps(data), encoding="utf-8")
    lines = transcript if transcript is not None else [
        stream("init"),
        stream("user", "Fix the login redirect loop on staging, draft PR only"),
        stream("assistant", "Found it: the middleware redirects before the cookie is set."),
        stream("result", "Opened draft PR #12 with the middleware fix."),
    ]
    (path / "transcript.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    slug = projects / "-var-lib-claude-jobs-worktrees-job"
    slug.mkdir(parents=True, exist_ok=True)
    rows = session if session is not None else [
        session_line("user", "Fix the login redirect loop on staging, draft PR only",
                     "2026-11-01T12:00:01.000Z"),
        session_line("assistant", "Found it: the middleware redirects before the cookie is set.",
                     "2026-11-01T12:03:00.000Z"),
    ]
    (slug / f"{sid}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    if subagent:
        side = slug / sid / "subagents"
        side.mkdir(parents=True, exist_ok=True)
        (side / "agent-1.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in subagent), encoding="utf-8")
    return path


def landing_upload(landing: Path):
    """A transport that copies batches into ``landing`` and removes the
    sources, like rsync --remove-source-files."""
    def upload(files, dest, key):
        for f in files:
            shutil.copy(f, landing / f.name)
            f.unlink()
        return True
    return upload


def shipped_lines(landing: Path) -> list[dict]:
    out = []
    for f in sorted(landing.glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out
