"""Strict/compliance profile: injection defaults off and scopes per-project."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from agent_history import config, embed, inject, store

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def put(db, text, *, cwd, session):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd=cwd, timestamp="2026-01-01T00:00:00Z", role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def two_projects(fake_embed):
    db, _ = store.open_for_write()
    put(db, "the auth middleware split so session refresh runs first",
        cwd="/clients/acme", session="acme-1")
    put(db, "the auth middleware split so session refresh runs first, cui variant",
        cwd="/clients/cui", session="cui-1")
    return db


def hook(prompt, cwd, session="current"):
    payload = io.StringIO(json.dumps(
        {"session_id": session, "cwd": cwd, "prompt": prompt}))
    return inject.run_hook(payload)


def strict_on():
    config.ensure_data_home()
    config.strict_path().touch()


def test_injection_off_by_default_under_strict(two_projects, capsys):
    strict_on()
    code = hook("tell me about the auth middleware split", cwd="/clients/acme")
    assert (code, capsys.readouterr().out) == (0, "")


def test_strict_injection_scopes_to_current_project(two_projects, capsys, monkeypatch):
    strict_on()
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")  # explicitly on under strict
    hook("tell me about the auth middleware split", cwd="/clients/acme")
    out = capsys.readouterr().out
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "/clients/acme" in ctx
    assert "cui variant" not in ctx, "strict must not pull the other project's chunk"


def test_non_strict_injects_across_projects(two_projects, capsys):
    # default (non-strict): cross-project retrieval is the normal behaviour
    hook("tell me about the auth middleware split", cwd="/clients/acme")
    out = capsys.readouterr().out
    assert out != ""  # on by default, and not project-scoped


def test_strict_enabled_reflects_marker_and_env(isolated_env, monkeypatch):
    assert not config.strict_enabled()
    config.ensure_data_home(); config.strict_path().touch()
    assert config.strict_enabled()
    monkeypatch.setenv("AGENT_HISTORY_STRICT", "0")  # env overrides marker
    assert not config.strict_enabled()


def test_toggle_semantics_flip_between_modes(fake_embed, monkeypatch, capsys):
    # normal: default on
    assert config.inject_enabled()
    inject.set_enabled(False)
    assert not config.inject_enabled()
    inject.set_enabled(True)
    assert config.inject_enabled()
    # strict: default off, on-marker enables
    config.strict_path().touch()
    assert not config.inject_enabled()
    inject.set_enabled(True)
    assert config.inject_enabled()
    inject.set_enabled(False)
    assert not config.inject_enabled()
    capsys.readouterr()
