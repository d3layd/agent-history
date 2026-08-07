"""Automatic indexing: the stamp check, the lock, and logging.

Detaching is not exercised here — spawning a real background process in a test
is slow and flaky. Everything downstream of it is covered.
"""
from __future__ import annotations

import pytest

from agent_history import config, trigger
from conftest import msg


def test_indexes_and_writes_to_the_log(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    assert trigger.run("TestHook", foreground=True) == 0
    log = config.log_path().read_text(encoding="utf-8")
    assert "TestHook trigger" in log
    assert "Embedding" in log


def test_log_never_contains_transcript_text(transcripts, fake_embed):
    secret = "the quick brown fox jumped over the lazy dog repeatedly"
    transcripts("proj", "s", [msg("user", secret)])
    trigger.run("TestHook", foreground=True)
    assert secret not in config.log_path().read_text(encoding="utf-8")


def test_stamp_is_written(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    trigger.run("TestHook", foreground=True)
    assert config.stamp_path().exists()


def test_if_changed_does_nothing_when_nothing_changed(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    trigger.run("first", foreground=True)
    before = config.log_path().read_text(encoding="utf-8")

    assert trigger.run("second", if_changed=True, foreground=True) == 0
    assert config.log_path().read_text(encoding="utf-8") == before, \
        "an unchanged run must not even open the indexer"


def test_if_changed_runs_when_a_transcript_is_newer(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "the original message in the file")])
    trigger.run("first", foreground=True)

    # A new transcript is newer than the stamp written above.
    transcripts("proj", "s2", [msg("user", "a brand new message appears now")])
    trigger.run("second", if_changed=True, foreground=True)
    assert "second trigger" in config.log_path().read_text(encoding="utf-8")


def test_if_changed_runs_when_no_stamp_exists(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    assert trigger.run("cold", if_changed=True, foreground=True) == 0
    assert "cold trigger" in config.log_path().read_text(encoding="utf-8")


def test_a_second_run_backs_off_while_locked(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    config.ensure_data_home()
    with trigger._exclusive(config.lock_path()) as held:
        assert held
        # The lock is ours; a concurrent trigger must give up rather than
        # corrupt the single SQLite file both would write.
        assert trigger.run("blocked", foreground=True) == 0
    assert not config.log_path().exists() or \
        "blocked trigger" not in config.log_path().read_text(encoding="utf-8")


def test_lock_is_released_afterwards(tmp_path):
    lock = tmp_path / ".lock"
    with trigger._exclusive(lock) as first:
        assert first
    with trigger._exclusive(lock) as second:
        assert second, "the lock must not leak between runs"


def test_indexing_failure_is_logged_not_raised(transcripts, monkeypatch, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    from agent_history import index

    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(index, "run", boom)
    # A hook that raises would surface a traceback in the editor.
    assert trigger.run("TestHook", foreground=True) == 1
    assert "simulated failure" in config.log_path().read_text(encoding="utf-8")


@pytest.mark.skipif(config.WINDOWS, reason="Windows has no POSIX mode bits")
def test_log_and_stamp_are_owner_only(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    trigger.run("TestHook", foreground=True)
    assert config.log_path().stat().st_mode & 0o777 == 0o600
    assert config.stamp_path().stat().st_mode & 0o777 == 0o600
