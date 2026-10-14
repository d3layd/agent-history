"""OpenClaw adapter.

OpenClaw writes one JSONL file per session under
~/.openclaw/agents/<agent>/sessions/. A `type=="session"` header line carries
the session id and cwd; `type=="message"` lines carry `message.role` and
`message.content` (a plain string or a list of `{"type": "text"}` blocks).
`*.trajectory.jsonl` siblings duplicate the dialogue with tool traffic and are
skipped.

Workspace markdown (MEMORY.md, memory/ tree) is indexed too — same rationale
as Claude Code project memory: hand-written durable notes surface first.

Format verified 2026-08-10 against live files on openclaw-soaring-eagle
(OpenClaw 2026.4.26, session header `version: 3`).
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "openclaw"

# Heartbeat traffic is the bulk of long-lived sessions and pure noise in a
# search index.
SKIP_PREFIXES = (
    "[OpenClaw heartbeat poll]",
    "Heartbeat check:",
    "HEARTBEAT_OK",
    "[Queued messages while agent was busy]",
)


class OpenClawAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [d for d in config.openclaw_dirs() if d.exists()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for root in self.roots():
            for path in self._session_files(root):
                yield from self._read_transcript(path)
            for path in self._workspace_markdown(root):
                yield from self._read_markdown(path)

    # ------------------------------------------------------------------ files

    def _transcript_files(self) -> list[Path]:
        """Every session transcript across all roots (used by coverage)."""
        return [p for root in self.roots() for p in self._session_files(root)]

    def _session_files(self, root: Path) -> list[Path]:
        return [p for p in sorted(root.glob("agents/*/sessions/*.jsonl"))
                if not p.name.endswith(".trajectory.jsonl")]

    # ---------------------------------------------------------------- parsing

    def _read_transcript(self, path: Path) -> Iterator[Record]:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        session_id = path.stem
        cwd = ""
        with handle as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                if kind == "session":
                    session_id = entry.get("id") or session_id
                    cwd = entry.get("cwd") or cwd
                    continue
                if kind != "message":
                    continue
                message = entry.get("message") or {}
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                for text in _texts(message):
                    text = text.strip()
                    if len(text) < MIN_CHARS:
                        continue
                    if text.startswith(SKIP_PREFIXES):
                        continue
                    yield Record(
                        agent=AGENT,
                        origin=str(path),
                        session_id=session_id,
                        cwd=cwd,
                        timestamp=entry.get("timestamp")
                        or message.get("timestamp", ""),
                        role=role,
                        text=scrub(text),
                    )

    def _workspace_markdown(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for workspace in sorted(root.glob("workspace*")):
            if not workspace.is_dir():
                continue
            files.extend(sorted(workspace.glob("*.md")))
            memory = workspace / "memory"
            if memory.is_dir():
                files.extend(sorted(memory.rglob("*.md")))
        return files

    def _read_markdown(self, path: Path) -> Iterator[Record]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            mtime = path.stat().st_mtime
        except OSError:
            return
        if len(text) < MIN_CHARS:
            return
        yield Record(
            agent=AGENT,
            origin=str(path),
            session_id=f"memory:{path.stem}",
            cwd=str(path.parent),
            timestamp=datetime.datetime.fromtimestamp(mtime).isoformat(),
            role="memory",
            text=scrub(text),
        )


def _texts(message: dict) -> Iterator[str]:
    """Yield message text, whether a plain string or a list of blocks."""
    content = message.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    yield text
