"""The `docs add/remove/list` command and persisted-root merging."""
from __future__ import annotations

import os

from agent_history import cli, config


def test_add_list_remove_round_trip(isolated_env, capsys):
    tree = isolated_env / "runbooks"
    tree.mkdir()

    assert cli._cmd_docs("add", str(tree)) == 0
    assert tree.resolve() in config.docs_dirs()

    assert cli._cmd_docs("list", None) == 0
    out = capsys.readouterr().out
    assert str(tree.resolve()) in out and "[persisted]" in out

    assert cli._cmd_docs("remove", str(tree)) == 0
    assert tree.resolve() not in config.docs_dirs()


def test_add_rejects_a_missing_directory(isolated_env, capsys):
    assert cli._cmd_docs("add", str(isolated_env / "nope")) == 1
    assert "not a directory" in capsys.readouterr().err


def test_add_is_idempotent(isolated_env, capsys):
    tree = isolated_env / "runbooks"
    tree.mkdir()
    cli._cmd_docs("add", str(tree))
    assert cli._cmd_docs("add", str(tree)) == 0
    assert "already added" in capsys.readouterr().out
    stored = config.docs_dirs_file().read_text().splitlines()
    assert stored.count(str(tree.resolve())) == 1


def test_env_and_persisted_roots_merge(isolated_env, monkeypatch, capsys):
    env_tree = isolated_env / "from-env"
    env_tree.mkdir()
    added_tree = isolated_env / "from-file"
    added_tree.mkdir()
    cli._cmd_docs("add", str(added_tree))
    monkeypatch.setenv("AGENT_HISTORY_DOCS_DIRS", str(env_tree))
    roots = config.docs_dirs()
    assert env_tree in roots and added_tree.resolve() in roots

    cli._cmd_docs("list", None)
    out = capsys.readouterr().out
    assert "[env]" in out and "[persisted]" in out


def test_remove_of_env_root_explains_itself(isolated_env, monkeypatch, capsys):
    env_tree = isolated_env / "from-env"
    env_tree.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_DOCS_DIRS", str(env_tree))
    assert cli._cmd_docs("remove", str(env_tree)) == 1
    assert "AGENT_HISTORY_DOCS_DIRS" in capsys.readouterr().err


def test_actions_needing_a_path_fail_cleanly(isolated_env, capsys):
    assert cli._cmd_docs("add", None) == 1
    assert cli._cmd_docs("remove", None) == 1


def test_missing_persisted_root_is_flagged_in_list(isolated_env, capsys):
    tree = isolated_env / "was-here"
    tree.mkdir()
    cli._cmd_docs("add", str(tree))
    tree.rmdir()
    cli._cmd_docs("list", None)
    assert "MISSING" in capsys.readouterr().out


def test_persisted_file_supports_multiple_roots(isolated_env):
    a = isolated_env / "a"
    b = isolated_env / "b"
    a.mkdir(), b.mkdir()
    cli._cmd_docs("add", str(a))
    cli._cmd_docs("add", str(b))
    roots = config.docs_dirs()
    assert a.resolve() in roots and b.resolve() in roots
    assert os.pathsep not in config.docs_dirs_file().read_text()