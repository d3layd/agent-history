"""Hermes adapter.

Hermes keeps one state.db per profile (~/.hermes, ~/.hermes-<profile>). Its
sessions/ directories are a trap: they hold only a gateway routing index and
error dumps, so a file-based adapter pointed there would silently index
nothing. The transcripts live in SQLite — `messages` joined to `sessions`.

`messages.content` is usually plain text but sometimes a JSON string (blocks,
tool payloads), so text is pulled out with a recursive walk. Timestamps are
epoch floats. Only named columns are selected — `sessions` carries a
`system_prompt` column that has no business in a search index.

The schema has drifted across Hermes versions (`sessions.cwd` and
`sessions.title` are not always present), so optional columns are probed
rather than assumed.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "hermes"


class HermesAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return config.hermes_state_dbs()

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for path in self.roots():
            yield from self._read_db(path)

    def _read_db(self, path: Path) -> Iterator[Record]:
        profile = path.parent.name  # .hermes-ops, .hermes-family, ...
        try:
            # Percent-encode: '#', '?' or '%' in a path would otherwise be
            # parsed as URI syntax and open the wrong file.
            db = sqlite3.connect(
                f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            optional = {"cwd", "title"} & _columns(db, "sessions")
            cols = "id, started_at" + "".join(f", {c}" for c in sorted(optional))
            sessions: dict[str, str] = {}
            for row in db.execute(f"SELECT {cols} FROM sessions"):
                extras = dict(zip(sorted(optional), row[2:]))
                sessions[row[0]] = extras.get("cwd") or ""

            rows = db.execute(
                "SELECT session_id, role, content, timestamp FROM messages "
                "WHERE role IN ('user', 'assistant') AND content IS NOT NULL "
                "ORDER BY id"
            )
            for session_id, role, content, epoch in rows:
                for text in _texts(content):
                    text = text.strip()
                    if len(text) < MIN_CHARS:
                        continue
                    yield Record(
                        agent=AGENT,
                        origin=f"db:{path}#messages",
                        session_id=session_id or "",
                        cwd=sessions.get(session_id) or profile,
                        timestamp=_iso(epoch),
                        role=role,
                        text=scrub(text),
                    )
        except sqlite3.Error:
            return
        finally:
            db.close()


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _texts(content) -> Iterator[str]:
    """Yield conversational text from a content value of unknown shape."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        stripped = content.lstrip()
        if stripped.startswith(("[", "{")):
            try:
                yield from _walk(json.loads(content))
                return
            except ValueError:
                pass
        yield content


def _walk(node) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        # Message blocks put prose under "text" or nested "content";
        # everything else in the dict is metadata.
        for key in ("text", "content"):
            if key in node:
                yield from _walk(node[key])


def _iso(epoch) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(epoch)).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
