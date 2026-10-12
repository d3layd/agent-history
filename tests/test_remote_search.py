"""Searching a hub over ssh: command construction, merge, and fail-open.

`subprocess.run` is mocked; no test opens a connection.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess

import pytest

from agent_history import cli, config, embed, remote, search, show, store

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


class Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class Calls(list):
    """The ssh calls made so far; ``answer`` is what the next one returns."""
    answer = Done("[]")


@pytest.fixture
def ssh(monkeypatch):
    calls = Calls()

    def fake_run(argv, **kw):
        calls.append({"argv": argv, **kw})
        if isinstance(calls.answer, Exception):
            raise calls.answer
        return calls.answer

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://reader@hub.example.invalid:2222")
    return calls


def put(db, text, *, session="s1", ts="2026-01-01T00:00:00Z", machine="testbox"):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp=ts, role="user", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text), machine=machine)
    db.commit()


def hub_hit(text, machine="server", ts="2026-01-02T00:00:00Z"):
    return {"agent": "hermes", "file": "db:/x#messages", "session_id": "h1",
            "cwd": "/srv", "timestamp": ts, "role": "assistant", "text": text,
            "machine": machine, "distance": None, "score": 0.02}


@pytest.fixture
def local_index(fake_embed):
    db, _ = store.open_for_write()
    put(db, "local first deploy pipeline notes", session="l1")
    put(db, "local second deploy pipeline notes", session="l2")
    return db


# --- command construction -----------------------------------------------------

def test_ssh_command_is_quoted_and_option_safe(ssh, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REMOTE_KEY", "/home/me/.ssh/hub_ed25519")
    query = 'rm -rf /; echo "$HOME" $(id) \'x\''
    command = remote.search_args(
        query, k=5, cwd="/my proj", agent=None, machine=None,
        exclude_machine=["a b"], mode="text", sort="newest", full=False,
        as_json=True)
    argv = remote.ssh_argv(command)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[argv.index("-i") + 1] == "/home/me/.ssh/hub_ed25519"
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-3] == "--" and argv[-2] == "reader@hub.example.invalid"
    # One argument, so the far side splits it back into exactly our words.
    assert argv[-1] == shlex.join(command)
    assert shlex.split(argv[-1]) == command
    assert command[-2:] == ["--", query]


@pytest.mark.parametrize("url", [
    "ssh://-oProxyCommand=x@host", "ssh://user@-host", "http://host", "host",
    "ssh://", "ssh://user@host:notaport"])
def test_bad_remote_urls_are_refused(monkeypatch, url):
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", url)
    with pytest.raises(remote.RemoteError):
        remote.ssh_argv(["search", "x"])


def test_query_json_rejects_garbage(ssh):
    ssh.answer = Done("not json")
    with pytest.raises(remote.RemoteError):
        remote.query_json(["search", "x"])
    ssh.answer = Done("{}")
    with pytest.raises(remote.RemoteError):
        remote.query_json(["search", "x"])


# --- modes --------------------------------------------------------------------

def test_local_only_when_no_remote_is_configured(local_index, capsys):
    assert search.run("deploy pipeline", mode="text") == 0
    assert "local first" in capsys.readouterr().out


def test_hub_only_when_there_is_no_local_index(ssh, capsys):
    assert search.run("deploy pipeline", mode="text", k=3) == 0
    (call,) = ssh
    # output goes straight through: not captured
    assert "capture_output" not in call
    sent = shlex.split(call["argv"][-1])
    assert sent[0] == "search" and sent[-2:] == ["--", "deploy pipeline"]
    assert "--json" not in sent


def test_merge_interleaves_by_rank_and_labels_the_machine(ssh, local_index, capsys):
    local = [r["text"] for r in search.search_rows("deploy pipeline", mode="text")]
    older = "2025-12-01T00:00:00Z"  # equal ranks tie; the newer chunk goes first
    ssh.answer = Done(json.dumps([
        hub_hit("hub first deploy pipeline notes", ts=older),
        hub_hit("hub second deploy pipeline notes", ts=older),
    ]))
    assert search.run("deploy pipeline", mode="text", k=4) == 0
    out = capsys.readouterr().out
    wanted = [local[0], "hub first deploy pipeline notes", local[1],
              "hub second deploy pipeline notes"]
    order = [out.index(t) for t in wanted]
    assert order == sorted(order), "RRF over disjoint lists interleaves by rank"
    assert "@server" in out and "@testbox" not in out

    sent = shlex.split(ssh[0]["argv"][-1])
    assert "--json" in sent
    assert sent[sent.index("--exclude-machine") + 1] == "testbox", \
        "the hub is asked to leave out this machine's own chunks"


def test_merge_respects_k(ssh, local_index, capsys):
    ssh.answer = Done(json.dumps([hub_hit("hub deploy pipeline notes")]))
    search.run("deploy pipeline", mode="text", k=2, as_json=True)
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_merge_json_has_fused_scores_and_machine(ssh, local_index, capsys):
    ssh.answer = Done(json.dumps([hub_hit("hub deploy pipeline notes")]))
    search.run("deploy pipeline", mode="text", as_json=True)
    got = json.loads(capsys.readouterr().out)
    assert {r["machine"] for r in got} == {"testbox", "server"}
    assert all(r["score"] for r in got)


def test_merge_drops_duplicates(ssh, local_index, capsys):
    same = hub_hit("local first deploy pipeline notes", machine="testbox")
    same.update(file="/t/l1.jsonl", session_id="l1", timestamp="2026-01-01T00:00:00Z",
                agent="claude-code")
    ssh.answer = Done(json.dumps([same]))
    search.run("deploy pipeline", mode="text", as_json=True)
    texts = [r["text"] for r in json.loads(capsys.readouterr().out)]
    assert texts.count("local first deploy pipeline notes") == 1


@pytest.mark.parametrize("answer", [
    Done("", returncode=255, stderr="ssh: connect to host: Connection refused\n"),
    subprocess.TimeoutExpired("ssh", 60),
    FileNotFoundError("ssh"),
    Done("garbage"),
])
def test_unreachable_hub_fails_open_with_one_line(ssh, local_index, capsys, answer):
    ssh.answer = answer
    assert search.run("deploy pipeline", mode="text") == 0
    cap = capsys.readouterr()
    assert "local first" in cap.out
    warning = [l for l in cap.err.splitlines() if l.strip()]
    assert len(warning) == 1 and warning[0].startswith("warning: hub unavailable")


def test_local_flag_never_calls_the_hub(ssh, local_index, capsys):
    assert search.run("deploy pipeline", mode="text", where="local") == 0
    assert ssh == []


def test_hub_flag_never_searches_locally(ssh, local_index, capsys):
    search.run("deploy pipeline", mode="text", where="hub")
    assert len(ssh) == 1 and "capture_output" not in ssh[0]


def test_hub_flag_without_a_remote_is_an_error(local_index, capsys):
    assert search.run("x", where="hub") == 1
    assert "AGENT_HISTORY_REMOTE" in capsys.readouterr().err


def test_asking_for_this_machine_skips_the_hub(ssh, local_index, capsys):
    search.run("deploy pipeline", mode="text", machine="testbox")
    assert ssh == []


def test_tenant_is_passed_to_the_hub_and_refused_locally(ssh, local_index, capsys):
    ssh.answer = Done("[]")
    search.run("deploy pipeline", mode="text", tenant="dennis")
    assert shlex.split(ssh[0]["argv"][-1])[shlex.split(ssh[0]["argv"][-1]).index("--tenant") + 1] == "dennis"
    assert search.run("x", where="local", tenant="dennis") == 1


def test_cli_flags_reach_search(ssh, local_index, capsys):
    ssh.answer = Done(json.dumps([hub_hit("hub deploy pipeline notes")]))
    assert cli.main(["search", "--mode", "text", "--machine", "server",
                     "deploy", "pipeline"]) == 0
    sent = shlex.split(ssh[0]["argv"][-1])
    assert sent[sent.index("--machine") + 1] == "server"
    assert sent[-1] == "deploy pipeline"


# --- show ---------------------------------------------------------------------

def test_show_prefers_local_and_falls_back_to_the_hub(ssh, local_index, capsys):
    assert show.run("l1") == 0
    assert ssh == []
    show.run("a-session-only-the-hub-has")
    (call,) = ssh
    assert shlex.split(call["argv"][-1])[0] == "show"
    assert shlex.split(call["argv"][-1])[-1] == "a-session-only-the-hub-has"


def test_show_hub_flag_goes_straight_to_the_hub(ssh, local_index):
    show.run("l1", where="hub")
    assert len(ssh) == 1


def test_remote_settings_are_read_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://u@h")
    monkeypatch.setenv("AGENT_HISTORY_REMOTE_KEY", "/k")
    assert config.remote_url() == "ssh://u@h" and config.remote_key() == "/k"
