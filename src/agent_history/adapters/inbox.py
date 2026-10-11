"""Hub inbox adapter.

On a hub, machines do not hand over transcripts; `agent-history ship` uploads
already-redacted Records as JSONL batches, and sshd (rrsync) drops them into
``<inbox>/<machine>/``. That layout is the whole protocol, so the transport
can change without touching this reader.

The machine is the *directory name*, which the hub's authorized_keys pins per
key; a ``machine`` field inside a batch is ignored, so one machine cannot file
history under another's name.

The inbox is a queue, so this adapter is the one that retires what it read:
after the indexer has committed, ``release()`` moves the consumed batches to
``<inbox>/.processed/<machine>/``. Until then a crash just means the same
batches are read again, and content-hash dedup makes that free. Pruning
``.processed`` is left to the operator (it is the hub's only raw copy of what
was shipped).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

from .. import config
from . import Record

AGENT = "inbox"
PROCESSED = ".processed"
_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class InboxAdapter:
    name = AGENT

    def __init__(self) -> None:
        self._consumed: list[tuple[str, Path]] = []

    def roots(self) -> list[Path]:
        root = config.inbox_dir()
        return [root] if root else []

    def available(self) -> bool:
        root = config.inbox_dir()
        return bool(root) and root.is_dir()

    def _batches(self) -> list[tuple[str, Path]]:
        """(machine, file) for every unprocessed batch, oldest name first."""
        root = config.inbox_dir()
        out: list[tuple[str, Path]] = []
        if not root:
            return out
        try:
            machines = sorted(p for p in root.iterdir()
                              if _MACHINE.match(p.name) and p.is_dir()
                              and not p.is_symlink())
        except OSError:
            return out
        for directory in machines:
            try:
                files = sorted(p for p in directory.glob("*.jsonl")
                               if p.is_file() and not p.is_symlink()
                               and not p.name.startswith("."))
            except OSError:
                continue
            out += [(directory.name, f) for f in files]
        return out

    def has_pending(self) -> bool:
        """Whether unread batches exist — the hub's "something changed"."""
        return bool(self._batches())

    def records(self) -> Iterator[Record]:
        for machine, path in self._batches():
            yield from self._read(machine, path)

    def _read(self, machine: str, path: Path) -> Iterator[Record]:
        bad = 0
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for line in handle:
                record = _parse(line, machine)
                if record is None:
                    bad += line.strip() != ""
                    continue
                yield record
        if bad:
            print(f"inbox: skipped {bad} malformed line(s) in {machine}/{path.name}",
                  file=sys.stderr)
        # Only a fully read batch is eligible to be retired.
        self._consumed.append((machine, path))

    def release(self) -> None:
        """Move batches read so far out of the way. Call after the commit."""
        root = config.inbox_dir()
        consumed, self._consumed = self._consumed, []
        if not root:
            return
        for machine, path in consumed:
            target = root / PROCESSED / machine
            try:
                target.mkdir(parents=True, exist_ok=True)
                if not config.WINDOWS:
                    (root / PROCESSED).chmod(0o700)
                    target.chmod(0o700)
                destination = target / path.name
                n = 0
                while destination.exists():  # same name shipped twice
                    n += 1
                    destination = target / f"{path.name}.{n}"
                os.replace(path, destination)
            except OSError as exc:
                print(f"inbox: could not retire {machine}/{path.name}: {exc}",
                      file=sys.stderr)


def _parse(line: str, machine: str) -> Record | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    def field(name: str) -> str:
        value = data.get(name)
        return value if isinstance(value, str) else ""

    return Record(
        agent=field("agent") or "unknown",
        origin=field("origin"),
        session_id=field("session_id"),
        cwd=field("cwd"),
        timestamp=field("timestamp"),
        role=field("role") or "unknown",
        text=text,
        machine=machine,
    )
