"""The hub side: inbox adapter, retiring batches after commit, hub mode."""
from __future__ import annotations

import json

import pytest

from agent_history import adapters, config, embed, index, store, trigger
from agent_history.adapters.inbox import InboxAdapter
from conftest import msg

GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def line(text, **over):
    rec = {"v": 1, "machine": "claimed", "agent": "claude-code",
           "origin": "/home/me/s.jsonl", "session_id": "s1", "cwd": "/work",
           "timestamp": "2026-01-01T00:00:00Z", "role": "user", "text": text}
    rec.update(over)
    return json.dumps(rec)


def drop(inbox, machine, name, *lines):
    d = inbox / machine
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    path = tmp_path / "inbox"
    path.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(path))
    return path


def test_inbox_adapter_is_off_without_the_env_var(monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_INBOX", raising=False)
    assert not InboxAdapter().available()
    assert "inbox" not in [a.name for a in adapters.available_adapters()]


def test_machine_comes_from_the_directory_not_the_file(inbox):
    drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))
    (rec,) = list(InboxAdapter().records())
    assert rec.machine == "laptop", "a machine field inside a batch must be ignored"
    assert rec.session_id == "s1" and rec.text.startswith("hello")


def test_malformed_lines_and_odd_directories_are_skipped(inbox, capsys):
    drop(inbox, "laptop", "b1.jsonl", "not json", line("fine record long enough"),
         json.dumps({"v": 2, "text": "wrong version"}),
         json.dumps({"v": 1, "text": 7}))
    drop(inbox, ".processed", "x.jsonl", line("already retired"))
    drop(inbox, "bad name", "x.jsonl", line("odd directory"))
    drop(inbox, "laptop", ".b2.jsonl.part", line("half uploaded"))
    recs = list(InboxAdapter().records())
    assert [r.text for r in recs] == ["fine record long enough"]
    assert "skipped 3 malformed" in capsys.readouterr().err


@needs_vec
def test_ingest_uses_the_directory_machine_and_retires_after_commit(inbox, fake_embed):
    f = drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))
    assert index.run() == 0
    db = store.open_for_read()
    assert db.execute("SELECT machine FROM chunks").fetchone()[0] == "laptop"
    assert not f.exists()
    assert (inbox / ".processed" / "laptop" / "b1.jsonl").exists()
    assert not InboxAdapter().has_pending()


@needs_vec
def test_batches_are_not_retired_before_the_commit(inbox, fake_embed, monkeypatch):
    f = drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))
    seen = {}
    real = index._release_consumed

    def spy(active):
        # By the time anything is retired the chunk must already be durable.
        seen["rows"] = store.open_for_read().execute(
            "SELECT count(*) FROM chunks").fetchone()[0]
        seen["still_there"] = f.exists()
        real(active)

    monkeypatch.setattr(index, "_release_consumed", spy)
    index.run()
    assert seen == {"rows": 1, "still_there": True}


@needs_vec
def test_a_crash_before_commit_rereads_without_duplicating(inbox, fake_embed, monkeypatch):
    f = drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))

    def boom(text, timeout=120):
        raise KeyboardInterrupt  # dies mid-embed, before the final commit

    real = embed.embed
    monkeypatch.setattr(embed, "embed", boom)
    with pytest.raises(KeyboardInterrupt):
        index.run()
    assert f.exists(), "an interrupted run must leave the batch where it was"

    monkeypatch.setattr(embed, "embed", real)
    index.run()
    index.run()
    db = store.open_for_read()
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


@needs_vec
def test_embed_failures_keep_the_batch(inbox, fake_embed, monkeypatch):
    f = drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))

    def down(text, timeout=120):
        raise embed.EmbedError("ollama is down")

    monkeypatch.setattr(embed, "embed", down)
    index.run()
    assert f.exists(), "a chunk that failed to embed must be retried, not retired"


@needs_vec
def test_batches_that_add_nothing_are_still_retired(inbox, fake_embed):
    drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))
    index.run()
    again = drop(inbox, "laptop", "b2.jsonl", line("hello from the laptop, long enough"))
    index.run()
    assert not again.exists()
    assert (inbox / ".processed" / "laptop" / "b2.jsonl").exists()


@needs_vec
def test_same_batch_name_twice_does_not_clobber_the_archive(inbox, fake_embed):
    drop(inbox, "laptop", "b1.jsonl", line("first version of this record text"))
    index.run()
    drop(inbox, "laptop", "b1.jsonl", line("second version of this record text"))
    index.run()
    kept = sorted((inbox / ".processed" / "laptop").iterdir())
    assert len(kept) == 2


def test_trigger_sees_unread_batches_as_a_change(inbox, tmp_path):
    config.ensure_data_home()
    stamp = config.stamp_path()
    stamp.touch()
    assert trigger._anything_changed(stamp) is False
    old = drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))
    import os
    os.utime(old, (1, 1))  # older than the stamp: still pending, still a change
    assert trigger._anything_changed(stamp) is True


# --- hub mode ---------------------------------------------------------------

def test_hub_mode_runs_only_the_inbox(inbox, transcripts, monkeypatch):
    transcripts("proj", "s", [msg("user", "a local transcript the hub must ignore")])
    assert "claude-code" in [a.name for a in adapters.available_adapters()]
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    assert [a.name for a in adapters.available_adapters()] == ["inbox"]


@needs_vec
def test_hub_mode_ignores_local_transcripts_when_indexing(
        inbox, transcripts, fake_embed, monkeypatch):
    transcripts("proj", "s", [msg("user", "a local transcript the hub must ignore")])
    drop(inbox, "laptop", "b1.jsonl", line("hello from the laptop, long enough"))
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    index.run()
    db = store.open_for_read()
    assert [r[0] for r in db.execute("SELECT machine FROM chunks")] == ["laptop"]


@needs_vec
@pytest.mark.parametrize("setting", ["off", "0", "false"])
def test_hub_mode_forces_redaction(inbox, fake_embed, monkeypatch, setting):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", setting)
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    assert config.redact_enabled() is True
    # A shipper that failed to redact must not get a secret into the hub index.
    drop(inbox, "laptop", "b1.jsonl", line(f"leaky record, token is {GH} oops"))
    index.run()
    texts = [r[0] for r in store.open_for_read().execute("SELECT text FROM chunks")]
    assert texts and all(GH not in t for t in texts)
    assert "[REDACTED:github_token]" in texts[0]


def test_hub_without_an_inbox_says_so(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    monkeypatch.delenv("AGENT_HISTORY_INBOX", raising=False)
    assert index.run() == 1
    assert "AGENT_HISTORY_INBOX" in capsys.readouterr().err


# --- tenancy: ingest of one tenant never lands in another -----------------------

@needs_vec
def test_ingest_stays_inside_its_tenant(tmp_path, fake_embed, monkeypatch):
    tenants = tmp_path / "tenants"
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tenants))
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    inboxes = tmp_path / "hubinbox"
    drop(inboxes / "dennis", "pc", "b1.jsonl", line("dennis private plan, long enough"))
    drop(inboxes / "soaring-eagle", "lexi", "b1.jsonl",
         line("client plan for soaring eagle, long enough"))

    for tenant in ("dennis", "soaring-eagle"):
        monkeypatch.setenv("AGENT_HISTORY_INBOX", str(inboxes / tenant))
        with config.tenant_home(tenant):
            assert index.run() == 0

    seen = {}
    for tenant in ("dennis", "soaring-eagle"):
        with config.tenant_home(tenant):
            db = store.open_for_read()
            seen[tenant] = [r[0] for r in db.execute("SELECT text FROM chunks")]
    assert seen == {"dennis": ["dennis private plan, long enough"],
                    "soaring-eagle": ["client plan for soaring eagle, long enough"]}
