"""OpenCode adapter.

OpenCode keeps everything in a single SQLite database. Conversation text lives
in `part` rows whose JSON `data` has type "text"; the message's role and
timestamps live in `message.data`; the working directory and title live on
`session`.

Only named columns are ever selected: the same database holds a
`control_account` table with live OAuth tokens, and `SELECT *` habits are how
those end up in an index.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "opencode"


class OpenCodeAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [p for p in config.opencode_db_paths() if p.is_file()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for path in self.roots():
            yield from self._read_db(path)

    def _read_db(self, path: Path) -> Iterator[Record]:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            sessions = {
                sid: (directory or "", title or "")
                for sid, directory, title in db.execute(
                    "SELECT id, directory, title FROM session"
                )
            }
            messages = {}
            for mid, data in db.execute("SELECT id, data FROM message"):
                info = _load(data)
                messages[mid] = (
                    info.get("role") or "",
                    ((info.get("time") or {}).get("created")),
                    ((info.get("path") or {}).get("cwd")),
                )

            rows = db.execute(
                "SELECT message_id, session_id, data FROM part ORDER BY id"
            )
            for message_id, session_id, data in rows:
                part = _load(data)
                if part.get("type") != "text":
                    continue
                role, created_ms, cwd = messages.get(message_id, ("", None, None))
                if role not in ("user", "assistant"):
                    continue
                text = (part.get("text") or "").strip()
                if len(text) < MIN_CHARS:
                    continue
                directory, _title = sessions.get(session_id, ("", ""))
                yield Record(
                    agent=AGENT,
                    origin=f"db:{path}#part",
                    session_id=session_id or "",
                    cwd=cwd or directory,
                    timestamp=_iso(created_ms),
                    role=role,
                    text=scrub(text),
                )
        except sqlite3.Error:
            return
        finally:
            db.close()


def _load(data) -> dict:
    try:
        parsed = json.loads(data)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso(epoch_ms) -> str:
    if not epoch_ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(epoch_ms / 1000).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
