"""The indexer must never re-index its own injected recall (feedback loop)."""
from __future__ import annotations

from agent_history import inject, textproc
from agent_history.index import _pending
from conftest import msg


def test_markers_cover_both_injectors():
    assert textproc.is_injection(inject.HEADER)
    assert textproc.is_injection("AUTO-RECALLED CONTEXT (deterministic ...)")
    assert textproc.is_injection("  \n" + inject.HEADER)  # leading whitespace
    assert not textproc.is_injection("a normal question about the codebase")


def test_injected_context_is_not_indexed(transcripts, fake_embed):
    # A transcript that captured an injected recall block (as could happen if
    # an agent persisted it) plus a real message.
    transcripts("proj", "s", [
        msg("assistant", inject.HEADER + "\n--- claude-code/user ... old stuff"),
        msg("user", "a genuine question that absolutely should be indexed"),
    ])
    pending, _produced, _mut = _pending(set())
    texts = [p[1] for p in pending]
    assert any("genuine question" in t for t in texts)
    assert not any("Possibly relevant excerpts" in t for t in texts), \
        "injected recall must never be re-indexed"


def test_fleet_marker_also_skipped(transcripts, fake_embed):
    transcripts("proj", "s", [
        msg("user", "AUTO-RECALLED CONTEXT (deterministic): ## Karen ... tasks"),
        msg("user", "the actual thing the person typed this turn, indexable"),
    ])
    pending, _p, _m = _pending(set())
    texts = [p[1] for p in pending]
    assert len(texts) == 1
    assert "actual thing" in texts[0]
