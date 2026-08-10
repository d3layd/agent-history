"""Codex rollout parsing — session_meta, turn_context, and event duplication."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_history.adapters.codex import CodexAdapter


@pytest.fixture
def rollouts(isolated_env, monkeypatch):
    root = isolated_env / "codex"

    def _write(name: str, entries: list[dict]) -> Path:
        day = root / "2026/08/09"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-{name}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    return _write


def meta(session_id="sess-codex-1", cwd="/repo"):
    return {"timestamp": "2026-08-09T10:00:00.000Z", "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd}}


def message(role, text, kind="input_text", ts="2026-08-09T10:01:00.000Z"):
    return {"timestamp": ts, "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": kind, "text": text}]}}


def records():
    return list(CodexAdapter().records())


def test_user_and_assistant_messages_are_kept(rollouts):
    rollouts("a", [
        meta(),
        message("user", "let's fix the flaky websocket reconnect test"),
        message("assistant", "the reconnect timer never fires under fake timers",
                kind="output_text"),
    ])
    got = records()
    assert [r.role for r in got] == ["user", "assistant"]
    assert got[0].agent == "codex"
    assert got[0].session_id == "sess-codex-1"
    assert got[0].cwd == "/repo"
    assert got[0].timestamp == "2026-08-09T10:01:00.000Z"


def test_developer_and_system_roles_are_dropped(rollouts):
    rollouts("a", [
        meta(),
        message("developer", "<permissions instructions> sandboxing rules apply"),
        message("system", "some base instructions that are not conversation"),
        message("user", "an actual question from the person typing"),
    ])
    assert [r.role for r in records()] == ["user"]


def test_event_msg_duplicates_are_not_read(rollouts):
    """Assistant text arrives as both event_msg and response_item; only the
    response_item form must be indexed."""
    rollouts("a", [
        meta(),
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "agent_message",
                     "message": "the same assistant text arrives twice"}},
        message("assistant", "the same assistant text arrives twice",
                kind="output_text"),
    ])
    assert len(records()) == 1


def test_turn_context_updates_cwd(rollouts):
    rollouts("a", [
        meta(cwd="/first"),
        message("user", "a message from the starting directory"),
        {"timestamp": "t", "type": "turn_context", "payload": {"cwd": "/second"}},
        message("user", "a message after changing directory"),
    ])
    got = records()
    assert [r.cwd for r in got] == ["/first", "/second"]


def test_harness_scaffolding_prefixes_are_dropped(rollouts):
    rollouts("a", [
        meta(),
        message("user", "<environment_context> shell: bash, os: linux"),
        message("user", "<user_instructions> injected AGENTS.md content here"),
        message("user", "a genuine request that should be kept"),
    ])
    got = records()
    assert len(got) == 1
    assert got[0].text.startswith("a genuine request")


def test_session_id_falls_back_to_filename_without_meta(rollouts):
    path = rollouts("no-meta", [message("user", "a rollout missing its meta line")])
    got = records()
    assert got[0].session_id == path.stem


def test_malformed_lines_do_not_abort_the_file(rollouts):
    path = rollouts("a", [meta(), message("user", "first message that is fine")])
    with path.open("a", encoding="utf-8") as f:
        f.write("{broken json\n")
        f.write(json.dumps(message("user", "message after the broken line")) + "\n")
    assert len(records()) == 2


def test_unavailable_without_directory(isolated_env):
    assert CodexAdapter().available() is False


def test_only_rollout_files_are_scanned(rollouts, isolated_env):
    rollouts("a", [meta(), message("user", "content in a real rollout file")])
    other = isolated_env / "codex/2026/08/09/notes.jsonl"
    other.write_text(json.dumps(message("user", "not a rollout file at all")) + "\n")
    assert len(records()) == 1
