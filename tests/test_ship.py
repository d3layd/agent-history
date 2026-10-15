"""`ship`: redacted Records out, idempotent, and nothing lost on a failed upload.

The transport is swapped for a copy into a local directory, so no test opens a
network connection or runs rsync.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import stat

import pytest

from agent_history import config, index, sessions, ship, store, trigger
from conftest import msg

GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A fake hub: uploads are copied into this directory."""
    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@example.invalid:")

    def upload(files, dest, key):
        for f in files:
            shutil.copy(f, landing / f.name)
            f.unlink()
        return True

    monkeypatch.setattr(ship, "upload", upload)
    return landing


def shipped(landing):
    out = []
    for f in sorted(landing.glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_ship_sends_records_with_the_documented_fields(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    assert ship.run() == 0
    (rec,) = shipped(hub)
    assert rec == {
        "v": 1, "machine": "testbox", "agent": "claude-code",
        "origin": rec["origin"], "session_id": "sess-1", "cwd": "/work",
        "timestamp": "2026-01-01T00:00:00Z", "role": "user",
        "text": "a message worth shipping to the hub",
        "session_uid": sessions.derive_uid("claude-code", "testbox", "sess-1"),
    }


def test_ship_redacts_even_when_local_redaction_is_off(transcripts, hub, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    assert config.redact_enabled() is False
    transcripts("proj", "s", [msg("user", f"push failed, my token is {GH} please fix")])
    ship.run()
    (rec,) = shipped(hub)
    assert GH not in rec["text"]
    assert "[REDACTED:github_token]" in rec["text"]
    # nothing unredacted is left behind in the outbox either
    assert not list(config.outbox_dir().glob("*"))


def test_ship_is_idempotent(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    ship.run()
    ship.run()
    assert len(shipped(hub)) == 1
    transcripts("proj", "s2", [msg("user", "a second message, shipped later on")])
    ship.run()
    assert len(shipped(hub)) == 2


def test_injected_recall_is_not_shipped(transcripts, hub):
    transcripts("proj", "s", [msg(
        "user", "Possibly relevant excerpts from past agent sessions: something long")])
    ship.run()
    assert shipped(hub) == []


def test_failed_upload_keeps_everything_and_retries(transcripts, hub, monkeypatch, capsys):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    good = ship.upload
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    assert ship.run() == 1
    assert shipped(hub) == []
    assert len(list(config.outbox_dir().glob("*.jsonl"))) == 1

    # Still failing: the same record is not written into a second batch.
    assert ship.run() == 1
    assert len(list(config.outbox_dir().glob("*.jsonl"))) == 1

    monkeypatch.setattr(ship, "upload", good)
    assert ship.run() == 0
    assert len(shipped(hub)) == 1
    assert not list(config.outbox_dir().glob("*.jsonl"))
    # and it is now remembered as shipped
    ship.run()
    assert len(shipped(hub)) == 1


def test_a_raising_transport_loses_nothing(transcripts, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    good = ship.upload

    def boom(files, dest, key):
        raise RuntimeError("network down")

    monkeypatch.setattr(ship, "upload", boom)
    assert ship.run() == 1
    monkeypatch.setattr(ship, "upload", good)
    assert ship.run() == 0
    assert len(shipped(hub)) == 1


def test_state_is_only_marked_shipped_after_the_upload(transcripts, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    ship.run()
    db = sqlite3.connect(config.ship_state_path())
    assert db.execute("SELECT count(*) FROM shipped").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM queued").fetchone()[0] == 1


def test_batches_are_capped_and_immutably_named(transcripts, hub, monkeypatch):
    monkeypatch.setattr(ship, "BATCH_LINES", 3)
    transcripts("proj", "s", [msg("user", f"message number {i} in a long session",
                                  timestamp=f"2026-01-01T00:00:{i:02d}Z")
                              for i in range(7)])
    ship.run()
    files = sorted(hub.glob("*.jsonl"))
    assert [len(f.read_text().splitlines()) for f in files] == [3, 3, 1]
    assert len({f.name for f in files}) == 3
    assert all(f.name.endswith(".jsonl") and "-" in f.name for f in files)


@pytest.mark.skipif(config.WINDOWS, reason="Windows has no POSIX mode bits")
def test_outbox_and_state_are_owner_only(transcripts, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    ship.run()
    assert stat.S_IMODE(config.outbox_dir().stat().st_mode) == 0o700
    (batch,) = config.outbox_dir().glob("*.jsonl")
    assert stat.S_IMODE(batch.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.ship_state_path().stat().st_mode) == 0o600


def test_dry_run_reports_without_writing_or_uploading(transcripts, hub, capsys, monkeypatch):
    transcripts("proj", "s", [msg("user", f"a message with a token {GH} inside")])
    monkeypatch.setattr(ship, "upload", lambda *a: pytest.fail("uploaded"))
    assert ship.run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "1 new record(s) would ship" in out
    assert "1 github_token" in out
    assert not config.outbox_dir().exists()
    assert not config.ship_state_path().exists()


def test_ship_needs_a_destination(transcripts, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_HISTORY_HUB_INGEST", raising=False)
    assert ship.run() == 1
    assert "AGENT_HISTORY_HUB_INGEST" in capsys.readouterr().err


def test_an_overlapping_run_is_a_noop(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    config.ensure_data_home()
    with trigger._exclusive(config.ship_lock_path()) as held:
        assert held
        assert ship.run() == 0
    assert shipped(hub) == []


def test_record_hash_fields_are_unambiguous():
    a = ship.record_hash("a", "b", "c", "d", "e", "f")
    assert a == ship.record_hash("a", "b", "c", "d", "e", "f")
    assert a != ship.record_hash("a", "bc", "", "d", "e", "f")
    assert a != ship.record_hash("a", "b", "c", "d", "e", "g")


def test_rsync_command_line(monkeypatch, tmp_path):
    seen = {}

    class Done:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(ship.subprocess, "run", fake_run)
    f = tmp_path / "a.jsonl"
    assert ship.upload([f], "ahub@host:", "/home/me/.ssh/hub key")
    argv = seen["argv"]
    assert argv[0] == "rsync" and "--remove-source-files" in argv
    ssh = argv[argv.index("-e") + 1]
    assert "BatchMode=yes" in ssh and "'/home/me/.ssh/hub key'" in ssh
    assert argv[-2:] == [str(f), "ahub@host:"]
    assert not ship.upload([f], "-oProxyCommand=x", None)


# --- backfill ---------------------------------------------------------------

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


@needs_vec
def test_backfill_exports_orphaned_chunks_redacted_with_a_watermark(
        transcripts, fake_embed, hub, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    path = transcripts("proj", "gone", [msg("user", f"deleted later, token {GH} here")])
    index.run()
    path.unlink()
    db = store.open_for_read()
    assert any(GH in r[0] for r in db.execute("SELECT text FROM chunks")), \
        "the local index keeps the credential"

    ship.run(backfill=True)
    (rec,) = shipped(hub)
    assert GH not in rec["text"] and "[REDACTED:github_token]" in rec["text"]
    assert rec["machine"] == "testbox"

    # Watermark: a second backfill exports nothing, a new chunk exports once.
    ship.run(backfill=True)
    assert len(shipped(hub)) == 1
    transcripts("proj", "new", [msg("user", "a brand new message to index now")])
    index.run()
    ship.run(backfill=True)
    assert len(shipped(hub)) == 2


@needs_vec
def test_backfill_skips_chunks_merged_in_from_other_machines(
        transcripts, fake_embed, hub):
    db, _ = store.open_for_write()
    store.insert_chunk(
        db, agent="codex", origin="/o", session_id="s", cwd="/w",
        timestamp="2026-01-01T00:00:00Z", role="user",
        text="someone else's history, not mine to re-ship", content_hash="h",
        vector=[0.0] * 8, machine="elsewhere")
    db.commit()
    ship.run(backfill=True)
    assert shipped(hub) == []


@needs_vec
def test_backfill_does_not_resend_what_ship_already_sent(
        transcripts, fake_embed, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    index.run()
    ship.run()
    ship.run(backfill=True)
    assert len(shipped(hub)) == 1


@needs_vec
def test_trigger_ships_after_indexing(transcripts, fake_embed, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    assert trigger.run("hook", foreground=True) == 0
    assert len(shipped(hub)) == 1
