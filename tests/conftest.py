"""Shared fixtures.

Every test runs against a temporary data directory and a stubbed embedder, so
the suite never touches a real index and never needs Ollama running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Environment variables that would otherwise leak a developer's real setup in.
_LEAKY = [
    "AGENT_HISTORY_HOME",
    "AGENT_HISTORY_PROJECTS",
    "AGENT_HISTORY_EXTRA_DIRS",
    "AGENT_HISTORY_MODEL",
    "AGENT_HISTORY_NO_WSL",
    "AGENT_HISTORY_ALLOW_REMOTE",
    "CLAUDE_HISTORY_HOME",
    "CLAUDE_HISTORY_PROJECTS",
    "CLAUDE_HISTORY_EXTRA_DIRS",
    "CLAUDE_HISTORY_MODEL",
    "OLLAMA_URL",
    "XDG_DATA_HOME",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every path at tmp_path and clear inherited configuration."""
    for name in _LEAKY:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_HISTORY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_HISTORY_PROJECTS", str(tmp_path / "projects"))
    # WSL detection reads a real absolute path; keep it out of unit tests.
    monkeypatch.setenv("AGENT_HISTORY_NO_WSL", "1")
    (tmp_path / "projects").mkdir()
    return tmp_path


@pytest.fixture
def fake_embed(monkeypatch):
    """Deterministic stand-in for Ollama.

    Returns a stable 8-dimensional vector derived from the text, so identical
    text embeds identically and the dimension is small enough to eyeball.
    """
    from agent_history import embed as embed_module

    calls: list[str] = []

    def _embed(text: str, timeout: int = 120) -> list[float]:
        calls.append(text)
        h = abs(hash(text))
        return [((h >> (i * 4)) & 0xF) / 15.0 for i in range(8)]

    monkeypatch.setattr(embed_module, "embed", _embed)
    monkeypatch.setattr(embed_module, "probe_dimension", lambda: 8)
    # store.py and index.py both imported the module, not the function.
    return calls


def write_transcript(root: Path, project: str, name: str, messages: list[dict]) -> Path:
    """Write a Claude Code style JSONL transcript and return its path."""
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")
    return path


def msg(role: str, text: str, **extra) -> dict:
    """Build one transcript line."""
    entry = {
        "type": role,
        "sessionId": extra.pop("session", "sess-1"),
        "cwd": extra.pop("cwd", "/work"),
        "timestamp": extra.pop("timestamp", "2026-01-01T00:00:00Z"),
        "message": {"content": [{"type": "text", "text": text}]},
    }
    entry.update(extra)
    return entry


@pytest.fixture
def transcripts(isolated_env):
    """Helper bound to the isolated projects directory."""
    root = isolated_env / "projects"

    def _write(project: str, name: str, messages: list[dict]) -> Path:
        return write_transcript(root, project, name, messages)

    _write.root = root
    return _write
