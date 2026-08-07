"""Source adapters.

Each adapter knows how one agent stores its transcripts and turns that into a
stream of `Record`s. Adding support for another agent means adding one module
here and registering it below — the indexer and search layer never change.

Transcripts come in two shapes in the wild: append-only JSONL files, and SQLite
databases. `Record.origin` accommodates both — a file path, or `db:<path>#<table>`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class Record:
    """One message, normalised across agents."""

    agent: str
    origin: str
    session_id: str
    cwd: str
    timestamp: str  # ISO 8601
    role: str
    text: str


@runtime_checkable
class Adapter(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this agent's data exists on this machine."""

    def roots(self) -> list[Path]:
        """Directories and files to watch for changes."""

    def records(self) -> Iterator[Record]:
        """Every indexable message this agent has stored."""


def all_adapters() -> list[Adapter]:
    from .claude_code import ClaudeCodeAdapter

    return [ClaudeCodeAdapter()]


def available_adapters() -> list[Adapter]:
    return [a for a in all_adapters() if a.available()]
