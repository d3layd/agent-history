"""OpenClaw session parsing — heartbeat noise, trajectory siblings, markdown."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_history.adapters.openclaw import OpenClawAdapter


@pytest.fixture
def openclaw(isolated_env):
    root = isolated_env / "openclaw"

    def _write(agent: str, name: str, entries: list[dict]) -> Path:
        d = root / "agents" / agent / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    _write.root = root
    return _write


def header(session_id="oc-sess-1", cwd="/home/bot/workspace"):
    return {"type": "session", "version": 3, "id": session_id,
            "timestamp": "2026-08-09T02:14:58.870Z", "cwd": cwd}


def message(role, content, ts="2026-08-09T02:15:10.000Z"):
    return {"type": "message", "id": "m1", "timestamp": ts,
            "message": {"role": role, "timestamp": ts, "content": content}}


def records():
    return list(OpenClawAdapter().records())


def test_string_and_block_content_are_both_read(openclaw):
    openclaw("main", "s1", [
        header(),
        message("user", "please check the client onboarding checklist"),
        message("assistant", [
            {"type": "text", "text": "the checklist is missing the CRM step"},
        ]),
    ])
    got = records()
    assert [r.role for r in got] == ["user", "assistant"]
    assert got[0].agent == "openclaw"
    assert got[0].session_id == "oc-sess-1"
    assert got[0].cwd == "/home/bot/workspace"
    assert got[0].timestamp == "2026-08-09T02:15:10.000Z"


def test_heartbeat_noise_is_skipped(openclaw):
    openclaw("main", "s1", [
        header(),
        message("user", "[OpenClaw heartbeat poll] checking in as scheduled"),
        message("assistant", "HEARTBEAT_OK all systems nominal right now"),
        message("user", "[Queued messages while agent was busy] earlier text"),
        message("user", "a genuine client question worth keeping"),
    ])
    got = records()
    assert len(got) == 1
    assert got[0].text.startswith("a genuine client question")


def test_non_message_line_types_are_ignored(openclaw):
    openclaw("main", "s1", [
        header(),
        {"type": "custom_message", "timestamp": "t",
         "message": {"role": "user", "content": "an injected custom message"}},
        {"type": "model_change", "model": "some-model"},
        message("assistant", "only this ordinary reply should be indexed"),
    ])
    assert len(records()) == 1


def test_trajectory_siblings_are_never_read(openclaw):
    openclaw("main", "s1", [
        header(),
        message("user", "the dialogue copy that should be indexed once"),
    ])
    openclaw("main", "s1.trajectory", [
        header(),
        message("user", "the trajectory duplicate that must be skipped"),
    ])
    got = records()
    assert len(got) == 1
    assert "indexed once" in got[0].text


def test_session_id_falls_back_to_file_stem(openclaw):
    path = openclaw("mgmt", "584613b2-topic-1535833713772990535",
                    [message("user", "a session file missing its header line")])
    assert records()[0].session_id == path.stem


def test_workspace_markdown_is_indexed(openclaw):
    ws = openclaw.root / "workspace-ops"
    (ws / "memory" / "credentials").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("Durable operational notes live here.")
    (ws / "memory" / "credentials" / "ondeck.md").write_text(
        "On Deck portal login notes for Allison's account.")
    got = records()
    assert {r.role for r in got} == {"memory"}
    assert len(got) == 2
    assert any("On Deck" in r.text for r in got)
    assert all(r.session_id.startswith("memory:") for r in got)


def test_env_override_points_at_other_roots(openclaw, isolated_env, monkeypatch):
    other = isolated_env / "elsewhere/openclaw2"
    d = other / "agents" / "main" / "sessions"
    d.mkdir(parents=True)
    (d / "x.jsonl").write_text(
        json.dumps(message("user", "a session under the overridden root")) + "\n")
    monkeypatch.setenv(
        "AGENT_HISTORY_OPENCLAW_DIRS",
        os.pathsep.join([str(openclaw.root), str(other)]))
    openclaw("main", "s1", [header(), message("user", "a session under root one")])
    texts = " ".join(r.text for r in records())
    assert "root one" in texts and "overridden root" in texts


def test_unavailable_without_directory(isolated_env):
    assert OpenClawAdapter().available() is False


def test_malformed_lines_do_not_abort_the_file(openclaw):
    path = openclaw("main", "s1", [header(), message("user", "first fine message here")])
    with path.open("a", encoding="utf-8") as f:
        f.write("{broken\n")
        f.write(json.dumps(message("user", "message after the broken line")) + "\n")
    assert len(records()) == 2
