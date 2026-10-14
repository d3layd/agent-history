"""Claude desktop Cowork (local agent mode) adapter.

Cowork runs an embedded Claude Code inside a sandbox VM and keeps each
session's state on the host under Claude desktop's app-data directory:

    local-agent-mode-sessions/
      <account-uuid>/<org-uuid>/
        local_<uuid>.json            session metadata (title, cwd, timestamps)
        local_<uuid>/
          .claude/projects/<slug>/<cli-session-uuid>.jsonl   transcript
          .claude/projects/<slug>/<cli-session-uuid>/subagents/agent-*.jsonl
          audit.jsonl                SDK event stream, HMAC-signed
          outputs/  uploads/         files the session produced or was given
        agent/local_ditto_<uuid>.json + agent/local_ditto_<uuid>/   (same shape)
      skills-plugin/...              bundled skills, no conversation

The transcripts are ordinary Claude Code JSONL, so they are parsed by the
Claude Code reader. `audit.jsonl` repeats the same dialogue as SDK stream
events and is not read, so nothing is indexed twice.

The in-transcript cwd is the sandbox's view (`/sessions/<name>`, or the
session's own outputs folder), meaningless on the host. When the metadata
names a folder the user mounted into the session, that is used instead.

This layout is undocumented and has changed between Claude desktop releases
(the metadata gains fields release to release). Anything that does not match
the shape above is skipped, never raised. Verified 2026-10-14 against Claude
desktop sessions written between 2026-02 and 2026-06.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import config
from . import Record
from .claude_code import read_transcript

AGENT = "claude-cowork"

# Session directories sit two levels below the root (account/org), plus one
# more for the long-lived `agent/` session. Bounded globs rather than rglob:
# outputs/ and uploads/ can hold arbitrarily many files, and on a WSL mount
# every directory entry is a round trip.
_SESSION_GLOBS = ("*/*/local_*", "*/*/*/local_*")


class CoworkAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [d for d in config.cowork_dirs() if d.is_dir()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for session in self._session_dirs():
            yield from self._read_session(session)

    def _session_dirs(self) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for root in self.roots():
            for pattern in _SESSION_GLOBS:
                for path in sorted(root.glob(pattern)):
                    if path in seen or not (path / ".claude/projects").is_dir():
                        continue
                    seen.add(path)
                    out.append(path)
        return out

    def _read_session(self, session: Path) -> Iterator[Record]:
        meta = _metadata(session.with_name(session.name + ".json"))
        session_id = _str(meta.get("sessionId")) or session.name
        cwd = _selected_folder(meta)
        try:
            files = sorted((session / ".claude/projects").rglob("*.jsonl"))
        except OSError:
            return
        for path in files:
            yield from read_transcript(path, agent=AGENT,
                                       session_id=session_id, cwd=cwd)


def _metadata(path: Path) -> dict:
    """The session's metadata JSON, or {} if absent or not the expected shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _selected_folder(meta: dict) -> str:
    """The first host folder the user mounted into the session, if any."""
    folders = meta.get("userSelectedFolders")
    if not isinstance(folders, list):
        return ""
    for folder in folders:
        if isinstance(folder, str) and folder:
            return folder
        if isinstance(folder, dict):
            path = folder.get("path")
            if isinstance(path, str) and path:
                return path
    return ""


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""
