"""Docs adapter — arbitrary documentation trees, named explicitly.

Transcripts answer "what was said"; a docs tree answers "what was written
down" — runbooks, ops notes, session summaries, the reference material that
agents and people keep next to their work. Indexing both into the same store
means one search spans them.

Unlike every other adapter there is nothing to auto-detect: a documentation
tree could be anywhere, and guessing would mean silently reading directories
the user never offered. So this adapter is entirely opt-in — it activates only
for the roots named in `AGENT_HISTORY_DOCS_DIRS` (os.pathsep-separated).

Trees are walked rather than globbed so that vendored and generated
directories can be pruned — a naive recursive glob over a project tree would
inhale every `node_modules/**/README.md` it can find.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "docs"

SUFFIXES = {".md", ".markdown", ".txt"}

# Directories that hold generated or vendored content, never the user's docs.
PRUNE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".cache",
}

# A "document" larger than this is a log or a dump, and embedding it would
# cost hours for content that chunk-level search serves poorly anyway.
MAX_FILE_BYTES = 2 * 1024 * 1024


class DocsAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [d for d in config.docs_dirs() if d.is_dir()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for root in self.roots():
            for path in _walk(root):
                yield from self._read_doc(root, path)

    def _read_doc(self, root: Path, path: Path) -> Iterator[Record]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            mtime = path.stat().st_mtime
        except OSError:
            return
        if len(text) < MIN_CHARS:
            return
        yield Record(
            agent=AGENT,
            origin=str(path),
            session_id=f"doc:{path.relative_to(root)}",
            cwd=str(root),
            timestamp=datetime.datetime.fromtimestamp(mtime).isoformat(),
            role="doc",
            text=scrub(text),
        )


def _walk(root: Path) -> Iterator[Path]:
    """Documentation files under `root`, oldest-path-first, pruned."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if Path(name).suffix.lower() in SUFFIXES:
                yield Path(dirpath) / name
