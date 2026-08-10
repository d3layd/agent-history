"""Docs tree indexing — opt-in gating, pruning, and file selection."""
from __future__ import annotations

import os

import pytest

from agent_history.adapters.docs import MAX_FILE_BYTES, DocsAdapter


@pytest.fixture
def docs(isolated_env, monkeypatch):
    root = isolated_env / "ops-library"
    root.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_DOCS_DIRS", str(root))

    def _write(relpath: str, text: str):
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    _write.root = root
    return _write


def records():
    return list(DocsAdapter().records())


def test_disabled_without_the_env_var(isolated_env, monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_DOCS_DIRS", raising=False)
    assert DocsAdapter().available() is False


def test_markdown_and_txt_are_indexed_recursively(docs):
    docs("RUNBOOK.md", "How to restart the gateway after a crash.")
    docs("memory/2026-07-13.md", "Grew all three disks; fleet auto-recovered.")
    docs("notes/session-notes.txt", "Client call notes from the Tuesday sync.")
    got = records()
    assert len(got) == 3
    assert {r.role for r in got} == {"doc"}
    assert {r.agent for r in got} == {"docs"}
    assert all(r.session_id.startswith("doc:") for r in got)
    assert all(r.cwd == str(docs.root) for r in got)


def test_other_extensions_are_ignored(docs):
    docs("script.py", "print('code, not documentation, stays out')")
    docs("data.json", '{"not": "a document either, skip it"}')
    docs("README.md", "The one real document in this tree.")
    got = records()
    assert len(got) == 1
    assert "real document" in got[0].text


def test_vendored_and_hidden_directories_are_pruned(docs):
    docs("node_modules/left-pad/README.md", "Vendored readme that must stay out.")
    docs(".git/description", "not even the right extension")
    docs(".obsidian/config.md", "Hidden directory content that must stay out.")
    docs("real/guide.md", "A genuine document inside the tree.")
    got = records()
    assert len(got) == 1
    assert "genuine document" in got[0].text


def test_oversized_files_are_skipped(docs):
    docs("dump.txt", "x" * (MAX_FILE_BYTES + 1))
    docs("small.md", "A small note that should definitely be kept.")
    got = records()
    assert len(got) == 1
    assert "small note" in got[0].text


def test_tiny_files_are_skipped(docs):
    docs("stub.md", "todo")
    assert records() == []


def test_multiple_roots_via_pathsep(docs, isolated_env, monkeypatch):
    other = isolated_env / "second-tree"
    other.mkdir()
    (other / "notes.md").write_text("A document under the second root.")
    monkeypatch.setenv("AGENT_HISTORY_DOCS_DIRS",
                       os.pathsep.join([str(docs.root), str(other)]))
    docs("first.md", "A document under the first root.")
    texts = " ".join(r.text for r in records())
    assert "first root" in texts and "second root" in texts


def test_session_id_is_the_relative_path(docs):
    from pathlib import Path

    docs("memory/credentials/ondeck.md",
         "Portal notes that need a stable identifier.")
    assert records()[0].session_id == f"doc:{Path('memory/credentials/ondeck.md')}"


def test_missing_root_is_not_an_error(isolated_env, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_DOCS_DIRS",
                       str(isolated_env / "does-not-exist"))
    assert DocsAdapter().available() is False
    assert records() == []
