"""Codex CLI adapter.

Codex writes one JSONL rollout file per session under
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl. The first line
is a `session_meta` entry carrying the session id and cwd; conversation text
lives in `response_item` entries whose payload is a `message`.

Assistant text is duplicated as both `event_msg`/`agent_message` and
`response_item`/`message` — only `response_item` is read, so nothing is
indexed twice.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "codex"

# Harness scaffolding that Codex injects as user-role messages.
SKIP_PREFIXES = (
    "<permissions",
    "<environment_context",
    "<user_instructions",
    "<turn_aborted",
    "<ide_context",
    "<app-context",
    "# AGENTS.md",
)


class CodexAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [d for d in config.codex_session_dirs() if d.is_dir()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for path in self._rollout_files():
            yield from self._read_rollout(path)

    def _rollout_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.rglob("rollout-*.jsonl"))
        return files

    def _read_rollout(self, path: Path) -> Iterator[Record]:
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
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = entry.get("type")

                if kind == "session_meta":
                    session_id = payload.get("id") or session_id
                    cwd = payload.get("cwd") or cwd
                    continue
                # Each turn re-states the cwd; a session can move around.
                if kind == "turn_context":
                    cwd = payload.get("cwd") or cwd
                    continue
                if kind != "response_item" or payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue  # developer/system carry instructions, not conversation

                for block in payload.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") not in ("input_text", "output_text"):
                        continue
                    text = (block.get("text") or "").strip()
                    if len(text) < MIN_CHARS or text.startswith(SKIP_PREFIXES):
                        continue
                    yield Record(
                        agent=AGENT,
                        origin=str(path),
                        session_id=session_id,
                        cwd=cwd,
                        timestamp=entry.get("timestamp", ""),
                        role=role,
                        text=scrub(text),
                    )
