"""Context injection — opt-in gating, precision filters, and hook safety."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from agent_history import config, embed, inject, store

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def put(db, text, *, session="old-session", ts="2026-01-01T00:00:00Z"):
    store.insert_chunk(
        db, agent="claude-code", origin="/t/x.jsonl", session_id=session,
        cwd="/w", timestamp=ts, role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def indexed(fake_embed):
    db, _ = store.open_for_write()
    put(db, "we split the auth middleware because session refresh runs first")
    return db


def payload(prompt, session="current-session"):
    return io.StringIO(json.dumps({
        "session_id": session, "cwd": "/w",
        "hook_event_name": "UserPromptSubmit", "prompt": prompt,
    }))


def hook(prompt, capsys, *, session="current-session"):
    code = inject.run_hook(payload(prompt, session))
    out = capsys.readouterr().out
    return code, out


def enable(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")


def test_enabled_by_default(indexed, capsys):
    """No flag, no env — injection fires because on is the default now."""
    _code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert out != ""
    assert "systemMessage" in json.loads(out)


def test_off_marker_disables(indexed, capsys):
    config.ensure_data_home()
    config.inject_off_path().touch()
    code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert (code, out) == (0, "")


def test_enabled_injects_a_matching_chunk(indexed, capsys, monkeypatch):
    enable(monkeypatch)
    # fake_embed is deterministic, so identical text embeds at distance ~0.
    code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert code == 0
    payload = json.loads(out)
    body = payload["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit"
    assert "auth middleware" in body["additionalContext"]
    assert "session=old-session" in body["additionalContext"]
    # user-visible notice lists which memory was pulled, with a snippet
    assert "systemMessage" in payload
    msg = payload["systemMessage"]
    assert "recalled 1 memory" in msg
    assert "auth middleware" in msg           # the snippet shows which memory
    assert "old-session" not in msg           # it's a snippet, not the raw block
    assert "/history:inject" in msg           # toggle stays discoverable


def test_system_message_pluralizes(fake_embed, capsys, monkeypatch):
    enable(monkeypatch)
    db, _ = store.open_for_write()
    for i in range(3):
        put(db, f"distinct indexed conversation number {i} about the auth project")
    _code, out = hook("tell me about the auth project conversations", capsys)
    msg = json.loads(out)["systemMessage"]
    assert "recalled 3 memories" in msg  # plural for >1
    # one list line per recalled memory (plus the header line)
    assert len([ln for ln in msg.splitlines() if ln.strip().startswith("·")]) == 3


def test_context_header_names_project_and_instructs_filtering(indexed, capsys,
                                                              monkeypatch):
    from agent_history import textproc
    enable(monkeypatch)
    _code, out = hook("why did we split the auth middleware", capsys)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "current project is /w" in ctx          # cwd from the hook payload
    assert "different project" in ctx               # instructed to filter
    assert textproc.is_injection(ctx)               # still loop-guarded


def test_env_one_overrides_off_marker(indexed, capsys, monkeypatch):
    config.ensure_data_home()
    config.inject_off_path().touch()          # disabled via marker
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")  # ...but env forces on
    _code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert out != ""


def test_env_zero_overrides_default_on(indexed, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "0")
    _code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert out == ""


def test_distance_cutoff_rejects_everything_when_tightened(indexed, capsys,
                                                           monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("AGENT_HISTORY_INJECT_MAX_DIST", "-1")
    _code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert out == ""


def test_own_session_chunks_are_excluded(indexed, capsys, monkeypatch):
    enable(monkeypatch)
    _code, out = hook("we split the auth middleware because session refresh runs first",
                      capsys, session="old-session")
    assert out == ""


def test_trivial_and_command_prompts_are_skipped(indexed, capsys, monkeypatch):
    enable(monkeypatch)
    for prompt in ("ok", "yes please", "go ahead.", "/history:search auth",
                   "!git status", "short one"):
        _code, out = hook(prompt, capsys)
        assert out == "", f"prompt {prompt!r} should not trigger retrieval"


def test_embed_failure_injects_nothing(indexed, capsys, monkeypatch):
    enable(monkeypatch)

    def _boom(*a, **k):
        raise embed.EmbedError("ollama down")

    monkeypatch.setattr(embed, "embed", _boom)
    code, out = hook("we split the auth middleware because session refresh runs first", capsys)
    assert (code, out) == (0, "")


def test_malformed_stdin_never_crashes(indexed, capsys, monkeypatch):
    enable(monkeypatch)
    assert inject.run_hook(io.StringIO("{not json")) == 0
    assert inject.run_hook(io.StringIO('"a bare string"')) == 0
    assert capsys.readouterr().out == ""


def test_missing_index_never_crashes(fake_embed, capsys, monkeypatch):
    enable(monkeypatch)
    code, out = hook("a long enough question about anything at all", capsys)
    assert (code, out) == (0, "")


def test_caps_limit_chunk_count(fake_embed, capsys, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("AGENT_HISTORY_INJECT_MAX_DIST", "1000")
    db, _ = store.open_for_write()
    for i in range(8):
        put(db, f"distinct indexed conversation number {i} about the project")
    _code, out = hook("tell me about the project conversations please", capsys)
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert context.count("session=") <= inject.MAX_CHUNKS


def test_near_duplicate_chunks_collapse(fake_embed, capsys, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("AGENT_HISTORY_INJECT_MAX_DIST", "1000")
    db, _ = store.open_for_write()
    same = "an identical long answer that was pasted into two sessions " * 3
    put(db, same, session="s-a")
    put(db, same + "x", session="s-b")  # differs only past the dedupe window
    _code, out = hook("tell me about that identical pasted answer", capsys)
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert context.count("session=") == 1


def test_toggle_round_trip(fake_embed, capsys):
    assert inject.set_enabled(True) == 0
    assert config.inject_enabled()
    assert inject.status() == 0
    assert inject.set_enabled(False) == 0
    assert not config.inject_enabled()
    capsys.readouterr()


def test_toggle_flips_state(fake_embed, capsys):
    assert config.inject_enabled()          # default on
    assert inject.toggle() == 0             # -> off
    assert not config.inject_enabled()
    assert inject.toggle() == 0             # -> on
    assert config.inject_enabled()
    capsys.readouterr()


def test_set_enabled_reports_env_override_on(fake_embed, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")   # forced on
    inject.set_enabled(False)                          # ask for off
    out = capsys.readouterr().out
    assert "forcing it on" in out                      # honest, not "inject: off"
    assert config.inject_enabled()                     # still on — env wins


def test_set_enabled_reports_env_override_off(fake_embed, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "0")   # forced off
    inject.set_enabled(True)                           # ask for on
    out = capsys.readouterr().out
    assert "forcing it off" in out
    assert not config.inject_enabled()                 # still off — env wins


def test_toggle_flips_marker_not_effective_under_override(fake_embed, capsys,
                                                          monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    before = config.inject_marker_pref()
    inject.toggle()
    assert config.inject_marker_pref() is not before  # marker flipped
    assert config.inject_enabled()                     # effective still forced on
    capsys.readouterr()
