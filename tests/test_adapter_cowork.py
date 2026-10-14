"""Claude desktop Cowork parsing — session layout, metadata, unknown shapes.

Fixtures are synthetic and mirror the on-disk structure only:
<root>/<account>/<org>/local_<id>.json + local_<id>/.claude/projects/...
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_history import config
from agent_history.adapters import all_adapters
from agent_history.adapters.cowork import CoworkAdapter

ACCOUNT = "acct-0000"
ORG = "org-0000"


@pytest.fixture
def cowork(isolated_env):
    root = isolated_env / "cowork"

    def _write(
        session: str,
        entries: list[dict],
        meta: dict | str | None = None,
        cli: str = "cli-1",
        slug: str = "-sessions-quiet-test-name",
        parent: str = "",
        subpath: str = "",
    ) -> Path:
        org = root / ACCOUNT / ORG / parent if parent else root / ACCOUNT / ORG
        sdir = org / session
        d = sdir / ".claude/projects" / slug
        if subpath:
            d = d / subpath
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{cli}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        # The SDK event stream sits beside the transcript and must be ignored.
        (sdir / "audit.jsonl").write_text(json.dumps({
            "type": "user", "session_id": cli, "_audit_timestamp": "t",
            "message": {"role": "user",
                        "content": "audit stream copy that must not be indexed"},
        }) + "\n", encoding="utf-8")
        if meta is not None:
            body = meta if isinstance(meta, str) else json.dumps(meta)
            (org / f"{session}.json").write_text(body, encoding="utf-8")
        return path

    _write.root = root
    return _write


def meta(session="local_s1", cwd="/sessions/quiet-test-name", **extra):
    m = {"sessionId": session, "cliSessionId": "cli-1", "cwd": cwd,
         "processName": "quiet-test-name", "userSelectedFolders": [],
         "createdAt": 1775744624268, "lastActivityAt": 1775744654501,
         "title": "synthetic title", "isArchived": False}
    m.update(extra)
    return m


def line(role, content, session="cli-1", cwd="/sessions/quiet-test-name",
         ts="2026-04-09T14:23:46.549Z", **extra):
    entry = {"type": role, "sessionId": session, "cwd": cwd, "timestamp": ts,
             "uuid": "u1", "parentUuid": None, "isSidechain": False,
             "message": {"role": role, "content": content}}
    entry.update(extra)
    return entry


def records():
    return list(CoworkAdapter().records())


def test_user_and_assistant_text_is_kept(cowork):
    cowork("local_s1", [
        line("user", "draft the quarterly newsletter outline"),
        line("assistant", [
            {"type": "thinking", "thinking": "private reasoning not indexed"},
            {"type": "text", "text": "here is an outline with four sections"},
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
        ]),
        line("user", [{"type": "tool_result", "tool_use_id": "t1",
                       "content": "tool output is not conversation"}]),
    ], meta=meta())
    got = records()
    assert [r.role for r in got] == ["user", "assistant"]
    assert got[0].agent == "claude-cowork"
    assert got[0].session_id == "cli-1"
    assert got[0].cwd == "/sessions/quiet-test-name"
    assert got[0].timestamp == "2026-04-09T14:23:46.549Z"
    assert got[1].text == "here is an outline with four sections"


def test_audit_stream_is_not_indexed(cowork):
    cowork("local_s1", [line("user", "the only real message in this session")],
           meta=meta())
    texts = [r.text for r in records()]
    assert texts == ["the only real message in this session"]


def test_non_conversation_lines_are_skipped(cowork):
    cowork("local_s1", [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "cli-1",
         "timestamp": "t", "content": "queued copy of the prompt text"},
        {"type": "last-prompt", "lastPrompt": "a last prompt summary line",
         "sessionId": "cli-1"},
        {"type": "mode", "mode": "default", "sessionId": "cli-1"},
        {"type": "attachment", "sessionId": "cli-1",
         "attachment": {"type": "skill_listing"}},
        line("user", "<system-reminder> harness scaffolding text here"),
        line("user", "meta line that the harness injected", isMeta=True),
        line("user", "a genuine request that should be kept"),
    ], meta=meta())
    assert [r.text for r in records()] == ["a genuine request that should be kept"]


def test_selected_folder_replaces_sandbox_cwd(cowork):
    cowork("local_s1", [line("user", "summarise the notes in my folder")],
           meta=meta(userSelectedFolders=["C:\\Users\\someone\\Documents\\notes"]))
    assert records()[0].cwd == "C:\\Users\\someone\\Documents\\notes"


def test_subagent_transcripts_are_read(cowork):
    cowork("local_s1", [line("user", "kick off parallel research tasks")],
           meta=meta())
    cowork("local_s1", [
        line("user", "subagent prompt describing its own task",
             isSidechain=True, agentId="a1"),
    ], cli="agent-a1", subpath="cli-1/subagents", meta=meta())
    got = records()
    assert len(got) == 2
    assert {r.session_id for r in got} == {"cli-1"}


def test_agent_subfolder_session_is_found(cowork):
    cowork("local_ditto_x", [line("user", "the long-lived agent session text")],
           meta=meta(session="local_ditto_x", sessionType="agent"),
           parent="agent")
    assert [r.text for r in records()] == ["the long-lived agent session text"]


def test_session_id_falls_back_to_metadata_then_directory(cowork):
    entry = line("user", "a line that carries no session id")
    del entry["sessionId"]
    cowork("local_s1", [entry], meta=meta(session="local_s1"))
    cowork("local_s2", [entry], meta=None, cli="cli-2")
    got = sorted(r.session_id for r in records())
    assert got == ["local_s1", "local_s2"]


@pytest.mark.parametrize("bad_meta", [
    "{not json",
    json.dumps(["a", "list"]),
    json.dumps({"userSelectedFolders": "not-a-list", "sessionId": 7}),
    json.dumps({"userSelectedFolders": [None, 3, {"nope": 1}]}),
])
def test_unknown_metadata_shapes_are_tolerated(cowork, bad_meta):
    cowork("local_s1", [line("user", "transcript text survives bad metadata")],
           meta=bad_meta)
    got = records()
    assert len(got) == 1
    assert got[0].cwd == "/sessions/quiet-test-name"


def test_unknown_line_shapes_do_not_abort_the_file(cowork):
    path = cowork("local_s1", [line("user", "first message that is fine")],
                  meta=meta())
    with path.open("a", encoding="utf-8") as f:
        f.write("{broken json\n")
        f.write(json.dumps(["not", "a", "mapping"]) + "\n")
        f.write(json.dumps({"type": "user", "message": ["list", "message"]}) + "\n")
        f.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": {"unexpected": "dict"}}]}}) + "\n")
        f.write(json.dumps(line("user", "message after the odd lines")) + "\n")
    assert len(records()) == 2


def test_directories_without_a_transcript_tree_are_skipped(cowork):
    root = cowork.root
    # A session that errored before Claude Code started: metadata, no .claude.
    errored = root / ACCOUNT / ORG / "local_errored"
    errored.mkdir(parents=True)
    (errored / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    (root / ACCOUNT / ORG / "local_errored.json").write_text(
        json.dumps(meta(session="local_errored")), encoding="utf-8")
    # Bundled skills live beside the account folders and hold no dialogue.
    (root / "skills-plugin" / ORG / ACCOUNT / "skills").mkdir(parents=True)
    # Loose files at the org level that are not sessions.
    (root / ACCOUNT / ORG / "cowork_settings.json").write_text("{}", encoding="utf-8")
    assert records() == []
    assert CoworkAdapter().available() is True


def test_unavailable_without_directory(isolated_env):
    assert CoworkAdapter().available() is False


def test_env_override_points_at_other_roots(cowork, isolated_env, monkeypatch):
    other = isolated_env / "elsewhere/cowork2"
    d = other / ACCOUNT / ORG / "local_o1" / ".claude/projects/-sessions-x"
    d.mkdir(parents=True)
    (d / "cli-9.jsonl").write_text(
        json.dumps(line("user", "a session under the overridden root")) + "\n",
        encoding="utf-8")
    monkeypatch.setenv(
        "AGENT_HISTORY_COWORK_DIRS",
        os.pathsep.join([str(cowork.root), str(other)]))
    cowork("local_s1", [line("user", "a session under root one")], meta=meta())
    texts = " ".join(r.text for r in records())
    assert "root one" in texts and "overridden root" in texts


def test_registered_with_the_other_adapters():
    assert "claude-cowork" in [a.name for a in all_adapters()]


def test_wsl_discovery_uses_the_cowork_marker(monkeypatch, tmp_path):
    """From WSL the Windows profile is selected by the Cowork directory
    itself, so a profile that never ran Claude Code is still found."""
    monkeypatch.delenv("AGENT_HISTORY_COWORK_DIRS", raising=False)
    monkeypatch.setattr(config, "WINDOWS", False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    seen = {}

    def fake_homes(_users_dir=None, marker=".claude/projects"):
        seen["marker"] = marker
        return [tmp_path / "Users" / "dana"]

    monkeypatch.setattr(config, "wsl_windows_homes", fake_homes)
    dirs = config.cowork_dirs()
    assert seen["marker"] == "AppData/Roaming/Claude/local-agent-mode-sessions"
    assert dirs == [tmp_path / "Users/dana/AppData/Roaming/Claude/"
                    "local-agent-mode-sessions"]


def test_macos_default_location(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HISTORY_COWORK_DIRS", raising=False)
    monkeypatch.setattr(config, "WINDOWS", False)
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.cowork_dirs() == [
        tmp_path / "Library/Application Support/Claude/local-agent-mode-sessions"]
