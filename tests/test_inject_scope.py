"""Directory-aware injection scoping: prefer current dir, admit near-exact
matches from elsewhere, drop obviously-unrelated other-project chunks."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from agent_history import embed, inject, store

# _same_scope / _in_scope are pure — no index needed.


def row(cwd, distance):
    # (agent, origin, session_id, cwd, timestamp, role, text, distance)
    return ("claude-code", "/o", "s", cwd, "t", "assistant", "x", None, distance)


def test_same_scope_matches_dir_and_subdirs():
    assert inject._same_scope("/repo", "/repo")
    assert inject._same_scope("/repo/tests", "/repo")      # chunk in a subdir
    assert inject._same_scope("/repo", "/repo/tests")      # prompt in a subdir
    assert not inject._same_scope("/repo", "/other")
    assert not inject._same_scope("/repo", "/repo-two")    # not a path boundary
    assert not inject._same_scope("", "/repo")
    assert not inject._same_scope("/repo", "")


CUTOFF, CROSS = 17.0, 14.0


def test_same_dir_uses_the_normal_cutoff():
    assert inject._in_scope(row("/repo", 16.0), "/repo", CUTOFF, CROSS, False)
    assert not inject._in_scope(row("/repo", 18.0), "/repo", CUTOFF, CROSS, False)


def test_other_dir_needs_the_tight_cutoff():
    # 16.0 is fine for same-dir but too loose for a different project
    assert not inject._in_scope(row("/other", 16.0), "/repo", CUTOFF, CROSS, False)
    # ...but a near-exact match from another project still comes through
    assert inject._in_scope(row("/other", 13.0), "/repo", CUTOFF, CROSS, False)


def test_strict_hard_scopes_to_current_dir():
    assert inject._in_scope(row("/repo", 16.0), "/repo", CUTOFF, CROSS, True)
    # even a near-exact other-project match is refused under strict
    assert not inject._in_scope(row("/other", 1.0), "/repo", CUTOFF, CROSS, True)


def test_no_scope_falls_back_to_single_cutoff():
    assert inject._in_scope(row("/anything", 16.0), "", CUTOFF, CROSS, False)
    assert not inject._in_scope(row("/anything", 18.0), "", CUTOFF, CROSS, False)


# One integration test: identical text in another project is near-exact, so
# it still injects; the machinery end-to-end.

def _put(db, text, *, cwd, session):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd=cwd, timestamp="2026-01-01T00:00:00Z", role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.mark.skipif(not store.extensions_supported(),
                    reason="this Python cannot load SQLite extensions")
def test_near_exact_other_project_match_still_injects(fake_embed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    db, _ = store.open_for_write()
    shared = "the exact phrase that appears verbatim in another project too"
    _put(db, shared, cwd="/clients/other", session="other-1")
    payload = io.StringIO(json.dumps(
        {"session_id": "cur", "cwd": "/clients/mine", "prompt": shared}))
    inject.run_hook(payload)
    out = capsys.readouterr().out
    # identical text → distance ~0 → clears even the tight cross cutoff
    assert out != ""
    assert "another project" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
