"""`serve-ssh`: the allowlist behind a pinned ssh key, and tenant isolation.

Every request goes through the real parser; nothing here spawns a process.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from agent_history import cli, config, embed, index, serve_ssh, store

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

REJECTED = serve_ssh.REJECTED


def put(db, text, *, session, machine="pc"):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp="2026-01-01T00:00:00Z", role="user", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text), machine=machine)
    db.commit()


@pytest.fixture
def tenants(tmp_path, monkeypatch, fake_embed):
    """Two tenant indexes under one root, each with a chunk of its own."""
    root = tmp_path / "tenants"
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(root))
    data = {
        "dennis": ("dennis ledger rotation plan for the shared vault", "sess-dennis"),
        "soaring-eagle": ("eagle lexi onboarding plan for the new client", "sess-eagle"),
    }
    for name, (text, session) in data.items():
        with config.tenant_home(name):
            db, _ = store.open_for_write()
            put(db, text, session=session)
            db.close()
    return data


@pytest.fixture
def no_side_effects(monkeypatch):
    """A rejected request must reach neither a shell nor a mutating command."""
    def fail(*a, **k):
        raise AssertionError("must not run")

    for name in ("run", "Popen", "call", "check_output", "check_call"):
        monkeypatch.setattr("subprocess." + name, fail)
    monkeypatch.setattr(os, "system", fail)
    monkeypatch.setattr(index, "run", fail)
    monkeypatch.setattr(index, "run_reembed", fail)


def go(command, tenants_arg=None):
    return serve_ssh.run(tenants_arg, command)


# --- the allowlist ---------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "", "   ", "index", "reindex --force", "reembed", "purge --all --yes",
    "purge --cwd /", "scrub --yes", "serve", "docs add /", "docs list",
    "inject run", "strict on", "ship", "ship --dry-run", "doctor", "sources",
    "datadir", "trigger x", "serve-ssh", "serve-ssh --tenants dennis",
    "; rm -rf /", "$(id)", "`id`", "| cat /etc/passwd", "&& index",
    "bash -c id", "/usr/bin/env search x", "SEARCH x",
    "search", "search --nonsense x", "search 'unterminated", "show",
    "search -k notanumber x", "search --mode bogus x", "search -h",
])
def test_everything_but_valid_search_and_show_is_rejected(command, no_side_effects, capsys):
    assert go(command) != 0
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err.startswith("serve-ssh: rejected")
    assert len(cap.err.strip().splitlines()) == 1, "a short message, not a usage dump"


def test_shell_metacharacters_in_a_query_are_just_text(no_side_effects, tenants, capsys):
    # sshd hands over a string; nothing here ever runs a shell, so these are
    # words to search for.
    for command in ("search 'x; touch /tmp/pwned'", "search x && index",
                    "search '$(id)'", "search 'a | b'"):
        assert go(command + " --mode text", "dennis") == 0
    capsys.readouterr()


def test_the_request_comes_from_ssh_original_command(monkeypatch, no_side_effects, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "index")
    assert cli.main(["serve-ssh"]) == REJECTED
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND")
    assert cli.main(["serve-ssh"]) == REJECTED, "an interactive login gets nothing"


def test_a_hub_with_its_own_remote_never_forwards(monkeypatch, tenants, capsys):
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://other@elsewhere")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("forwarded"))
    with config.tenant_home("dennis"):
        assert go("search ledger --mode text") == 0


def test_plain_mode_searches_this_hubs_index(tenants, capsys):
    with config.tenant_home("dennis"):
        assert go("search ledger --mode text") == 0
    assert "dennis ledger" in capsys.readouterr().out


def test_plain_mode_has_no_tenants(tenants, capsys):
    assert go("search ledger --tenant dennis") == REJECTED


# --- tenancy ------------------------------------------------------------------------

@pytest.mark.parametrize("arg", [
    "", "Dennis", "../dennis", "dennis/../x", "a/b", "-x", ".hidden", "dennis,",
    ",dennis", "dennis,,x", "dennis,dennis", "x" * 42, "dennis;id", "den nis",
    "dennis,../soaring-eagle", "/etc", "..",
])
def test_invalid_tenant_names_are_rejected(arg, tenants, capsys):
    assert go("search ledger", arg) == REJECTED
    assert capsys.readouterr().out == ""


def test_a_tenant_dir_that_escapes_the_root_is_rejected(tenants, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "tenants" / "evil").symlink_to(outside)
    assert go("search ledger", "evil") == REJECTED


def test_a_client_key_cannot_search_another_tenants_history(tenants, capsys):
    assert go("search ledger --mode text", "soaring-eagle") == 0
    assert "dennis" not in capsys.readouterr().out.replace("(no matches)", "")


def test_naming_another_tenant_explicitly_is_refused(tenants, capsys):
    assert go("search ledger --mode text --tenant dennis", "soaring-eagle") == REJECTED
    cap = capsys.readouterr()
    assert cap.out == "" and "not permitted" in cap.err
    assert go("search ledger --tenant=dennis --json", "soaring-eagle") == REJECTED
    assert go("show sess-dennis --tenant dennis", "soaring-eagle") == REJECTED


def test_a_client_key_cannot_show_another_tenants_session(tenants, capsys):
    assert go("show sess-dennis", "soaring-eagle") == 1
    cap = capsys.readouterr()
    assert "nothing indexed" in cap.out and "ledger" not in cap.out + cap.err
    assert go("show /t/sess-dennis.jsonl", "soaring-eagle") == 1
    assert go("show sess-eagle", "soaring-eagle") == 0
    assert "onboarding" in capsys.readouterr().out


def test_the_tenant_list_cannot_come_from_the_client(tenants, capsys):
    # The client controls only SSH_ORIGINAL_COMMAND; `--tenants` is not an
    # option of search or show, and serve-ssh is not an allowed command.
    assert go("search ledger --tenants dennis", "soaring-eagle") == REJECTED
    assert go("serve-ssh --tenants dennis", "soaring-eagle") == REJECTED
    assert go("search ledger --tena dennis", "soaring-eagle") == REJECTED
    capsys.readouterr()


def test_a_multi_tenant_key_merges_and_labels(tenants, capsys):
    assert go("search plan --mode text", "dennis,soaring-eagle") == 0
    out = capsys.readouterr().out
    assert "[dennis]" in out and "[soaring-eagle]" in out
    assert "dennis ledger" in out and "eagle lexi" in out


def test_a_multi_tenant_key_json_carries_the_tenant(tenants, capsys):
    assert go("search plan --mode text --json", "dennis,soaring-eagle") == 0
    got = json.loads(capsys.readouterr().out)
    assert {r["tenant"] for r in got} == {"dennis", "soaring-eagle"}
    assert all(r["score"] for r in got), "results are fused across tenants"


def test_the_tenant_flag_narrows_within_the_allowed_list(tenants, capsys):
    assert go("search plan --mode text --tenant dennis", "dennis,soaring-eagle") == 0
    out = capsys.readouterr().out
    assert "dennis ledger" in out and "eagle" not in out
    assert "[dennis]" not in out, "one tenant involved: nothing to label"


def test_a_single_tenant_key_sees_no_labels(tenants, capsys):
    go("search plan --mode text", "soaring-eagle")
    assert "[" not in capsys.readouterr().out


def test_a_tenant_with_nothing_yet_is_skipped(tenants, capsys):
    assert go("search plan --mode text", "dennis,not-shipped-yet") == 0
    assert "dennis ledger" in capsys.readouterr().out


def test_show_finds_the_tenant_and_refuses_ambiguity(tenants, capsys):
    assert go("show sess-eagle", "dennis,soaring-eagle") == 0
    assert "tenant=soaring-eagle" in capsys.readouterr().out
    for name in ("dennis", "soaring-eagle"):
        with config.tenant_home(name):
            db, _ = store.open_for_write()
            put(db, f"shared session id, tenant {name} text", session="same-id")
    assert go("show same-id", "dennis,soaring-eagle") == 1
    assert "pass --tenant" in capsys.readouterr().err
    assert go("show same-id --tenant dennis", "dennis,soaring-eagle") == 0
    assert "tenant dennis text" in capsys.readouterr().out
