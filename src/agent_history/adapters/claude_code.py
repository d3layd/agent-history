"""Claude Code adapter.

Claude Code writes one JSONL file per session under ~/.claude/projects/<slug>/,
one JSON object per line. Subagent transcripts live in nested directories, so
the scan is recursive.

Project memory files (~/.claude/projects/<slug>/memory/*.md) are indexed too:
they are hand-written durable notes, and they are frequently the most useful
thing a search can surface.

A session driven through remote control has a second identity, the
``cse_…`` / ``session_…`` pair of the cloud bridge. Claude Code records it in
the transcript (a ``bridge-session`` line) and in ~/.claude/sessions/*.json
(``bridgeSessionId``); either is hard evidence, and every record of that
session carries it as an alias so the index can link the two.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "claude-code"

# Harness-generated text that is noise in a search index.
SKIP_PREFIXES = (
    "<local-command",
    "<command-",
    "<task-notification>",
    "<system-reminder>",
    "[{'tool_use_id'",
)


class ClaudeCodeAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        roots = [config.claude_projects_dir(), *config.extra_dirs()]
        roots += [home / ".claude/projects" for home in config.wsl_windows_homes()]
        # Preserve order while dropping duplicates and absent paths.
        seen: set[Path] = set()
        out: list[Path] = []
        for root in roots:
            if root.exists() and root not in seen:
                seen.add(root)
                out.append(root)
        return out

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        self._bridges = self._session_bridges()
        for path in self._transcript_files():
            yield from self._read_transcript(path)
        for path in self._memory_files():
            yield from self._read_memory(path)

    # ------------------------------------------------------------------ files

    def _transcript_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.rglob("*.jsonl"))
        return _dedupe(files)

    def _session_bridges(self) -> dict[str, str]:
        """sessionId -> bridgeSessionId from ~/.claude/sessions/*.json.

        Those files describe running sessions; one names its remote-control
        bridge once the session is connected to it.
        """
        out: dict[str, str] = {}
        for root in self.roots():
            directory = root.parent / "sessions"
            if root.name != "projects" or not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                sid, bridge = data.get("sessionId"), data.get("bridgeSessionId")
                if isinstance(sid, str) and _bridge_kind(bridge):
                    out[sid] = bridge
        return out

    def _memory_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.glob("*/memory/*.md"))
        return _dedupe(files)

    # ---------------------------------------------------------------- parsing

    def _read_transcript(self, path: Path) -> Iterator[Record]:
        # Records are held until the file is read: the bridge-session line
        # can come after messages that belong to the same session.
        out: list[Record] = []
        bridges = dict(getattr(self, "_bridges", {}))
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isMeta"):
                    continue
                role = entry.get("type")
                if role == "bridge-session":
                    bridge = entry.get("bridgeSessionId")
                    if entry.get("sessionId") and _bridge_kind(bridge):
                        bridges[entry["sessionId"]] = bridge
                    continue
                if role not in ("user", "assistant"):
                    continue
                for text in _texts(entry.get("message") or {}):
                    text = text.strip()
                    if len(text) < MIN_CHARS:
                        continue
                    if text.startswith(SKIP_PREFIXES):
                        continue
                    out.append(Record(
                        agent=AGENT,
                        origin=str(path),
                        session_id=entry.get("sessionId") or "",
                        cwd=entry.get("cwd") or "",
                        timestamp=entry.get("timestamp", ""),
                        role=role,
                        text=scrub(text),
                    ))
        for record in out:
            bridge = bridges.get(record.session_id)
            if bridge:
                record = dataclasses.replace(
                    record, aliases=((_bridge_kind(bridge), bridge),))
            yield record

    def _read_memory(self, path: Path) -> Iterator[Record]:
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
            cwd=path.parent.parent.name,  # the project slug
            timestamp=datetime.datetime.fromtimestamp(mtime).isoformat(),
            role="memory",
            text=scrub(text),
        )


def _bridge_kind(bridge_id: object) -> str | None:
    """Alias kind of a remote-control id: ``cse`` or ``bridge``; None if it
    is not one."""
    if isinstance(bridge_id, str):
        if bridge_id.startswith("cse_") and len(bridge_id) > 4:
            return "cse"
        if bridge_id.startswith("session_") and len(bridge_id) > 8:
            return "bridge"
    return None


def _texts(message: dict) -> Iterator[str]:
    """Yield the text of a message, whether it is a plain string or blocks."""
    content = message.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield block.get("text") or ""


def _dedupe(paths: list[Path]) -> list[Path]:
    """Drop duplicates that arrive via different roots or symlinks."""
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out
