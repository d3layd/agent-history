"""Session notes: separate store, append-only, redacted, sourced, swept.

No network: uploads are a local copy, the summarizer is built in or a local
script, and the embedder is the conftest stub.
"""
from __future__ import annotations

import datetime
import io
import json
import shutil
import sqlite3
import sys
import types

import pytest

from agent_history import cli, config, embed, index, notes, ship, store, trigger
from agent_history.adapters.inbox import InboxAdapter
from conftest import msg

GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])
URL = "https://claude.ai/chat/0f6a7c2e-1234-4abc-8def-0123456789ab"
JOB = "cj_20261101T120000Z_a1b2c3"
LATER = datetime.datetime(2026, 1, 1, 1, 0, tzinfo=datetime.timezone.utc)

needs_ext = pytest.mark.skipif(not store.extensions_supported(),
                               reason="this Python cannot load SQLite extensions")


def code_source(**kw):
    return notes.make_source(session_id="sess-1", machine="testbox", **kw)


# ------------------------------------------------------------------ sources

def test_each_source_kind_is_accepted_with_its_link():
    ai = notes.make_source(chat_url=URL)
    assert ai == {"kind": "claude-ai", "url": URL}
    assert notes.show_target(ai)["session"] == "0f6a7c2e-1234-4abc-8def-0123456789ab"
    duet = notes.make_source(duet_channel_id="chan_7Hq2", duet_message_id="msg_991")
    assert duet == {"kind": "duet", "channel_id": "chan_7Hq2", "message_id": "msg_991"}
    assert notes.show_target(duet) is None
    job = code_source(job_id=JOB)
    assert job == {"kind": "claude-code", "machine": "testbox", "session_id": "sess-1",
                   "job_id": JOB}
    assert notes.show_target(job, {"start": "2026-01-01T00:00:00Z"}) == {
        "session": JOB, "around": "2026-01-01T00:00:00Z"}
    assert notes.show_target(code_source())["session"] == "sess-1"


@pytest.mark.parametrize("kw", [
    {},                                                        # no source
    {"chat_url": URL, "session_id": "s", "machine": "m"},      # two sources
    {"chat_url": "https://example.com/chat/x"},                # not claude.ai
    {"duet_channel_id": "chan_7Hq2"},                          # no message id
    {"session_id": "sess-1"},                                  # no machine
    {"session_id": "sess-1", "machine": "m", "job_id": "job-1"},  # bad job id
    {"session_id": "has space", "machine": "m"},
])
def test_a_note_without_a_resolvable_source_is_refused(kw):
    with pytest.raises(notes.NoteError):
        notes.make_source(**kw)


def test_build_redacts_every_text_field():
    note = notes.build(title=f"rotated {GH}", summary=f"the token {GH} leaked",
                       decisions=[f"revoke {GH}"], links=[f"https://x.test/?t={GH}"],
                       source=notes.make_source(duet_channel_id="c1", duet_message_id="m1"))
    assert GH not in json.dumps(note)
    assert notes.NOTE_ID.match(note["note_id"])
    assert note["v"] == 1 and note["kind"] == "note"
    assert "text" not in note, "a note must never parse as a transcript record"


def test_build_validates_sizes_and_corrections():
    src = code_source()
    with pytest.raises(notes.NoteError):
        notes.build(title="", summary="s", source=src)
    with pytest.raises(notes.NoteError):
        notes.build(title="t", summary="s" * 9000, source=src)
    with pytest.raises(notes.NoteError):
        notes.build(title="t", summary="s", source=src, corrects="rec-123")
    first = notes.build(title="t", summary="s", source=src)
    fix = notes.build(title="t2", summary="s2", source=src, corrects=first["note_id"])
    assert fix["corrects"] == first["note_id"]


# -------------------------------------------------------------------- store

def test_without_a_hub_notes_are_stored_locally_and_apart(fake_embed):
    note = notes.build(title="Login redirect loop fixed",
                       summary="Middleware redirected before the cookie was set.",
                       source=code_source(job_id=JOB), span_start="2026-01-01T00:00:00Z")
    assert notes.save(note) == "stored locally"
    assert notes.db_path().exists()
    assert not config.db_path().exists(), "the transcript index is untouched"
    (hit,) = notes.search("redirect cookie", mode="text")
    assert hit["type"] == "note" and hit["note_id"] == note["note_id"]
    assert hit["show"] == {"session": JOB, "around": "2026-01-01T00:00:00Z"}
    assert hit["source"]["job_id"] == JOB


def test_the_store_is_append_only(fake_embed):
    notes.save(notes.build(title="t", summary="a note that must not change",
                           source=code_source()))
    db, _ = notes.open_store(write=True)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE notes SET title = 'changed'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM notes")
    finally:
        db.close()


def test_the_same_note_twice_is_stored_once(fake_embed):
    note = notes.build(title="t", summary="delivered twice", source=code_source())
    db, vec = notes.open_store(write=True)
    try:
        assert notes.insert(db, note, "m", vec) is True
        assert notes.insert(db, note, "m", vec) is False
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    finally:
        db.close()


@needs_ext
def test_hybrid_search_uses_vectors_when_available(fake_embed):
    notes.save(notes.build(title="Boat VM export", summary="pulled transcripts before delete",
                           source=code_source()))
    hits = notes.search("Boat VM export", mode="hybrid")
    assert hits and hits[0]["title"] == "Boat VM export"
    assert any("Boat VM export" == t for t in fake_embed)


# ---------------------------------------------------------------- hub inbox

@pytest.fixture
def hub_inbox(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    (inbox / "claude-jobs" / "notes").mkdir(parents=True)
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(inbox))
    return inbox


def drop(inbox, machine, note):
    return notes.write_file(inbox / machine / "notes", note)


def test_hub_indexes_notes_into_their_own_store_and_retires_them(hub_inbox, fake_embed):
    note = notes.build(title="Job wrapped", summary="draft PR opened",
                       source=code_source(job_id=JOB))
    drop(hub_inbox, "claude-jobs", note)
    (hub_inbox / "claude-jobs" / "notes" / "bad.jsonl").write_text("not json\n{}\n")
    assert notes.inbox_pending()
    assert notes.index_inbox() == 0
    assert not notes.inbox_pending()
    assert len(list((hub_inbox / ".processed" / "claude-jobs" / "notes").glob("*"))) == 2
    (hit,) = notes.search("draft PR", mode="text")
    assert hit["machine"] == "claude-jobs"


def test_the_transcript_reader_never_sees_notes(hub_inbox, fake_embed, monkeypatch):
    drop(hub_inbox, "claude-jobs", notes.build(title="t", summary="only a note",
                                               source=code_source()))
    adapter = InboxAdapter()
    assert not adapter.has_pending()
    assert list(adapter.records()) == []
    # even a note line dropped where transcripts go is rejected by the old parser
    line = json.dumps(notes.build(title="t", summary="misfiled note", source=code_source()))
    (hub_inbox / "claude-jobs" / "misfiled.jsonl").write_text(line + "\n")
    assert list(InboxAdapter().records()) == []


def test_a_down_embedder_keeps_the_notes_batch(hub_inbox, fake_embed, monkeypatch):
    drop(hub_inbox, "claude-jobs", notes.build(title="t", summary="wait for ollama",
                                               source=code_source()))
    monkeypatch.setattr(notes, "_vectors_ready", lambda db, write: True)

    def down(text, timeout=120):
        raise embed.EmbedError("connection refused")

    monkeypatch.setattr(embed, "embed", down)
    assert notes.index_inbox() == 1
    assert notes.inbox_pending()


def test_hub_redacts_again_on_index(hub_inbox, fake_embed):
    raw = notes.build(title="t", summary="clean at write time", source=code_source())
    raw["summary"] = f"an old writer let {GH} through"
    drop(hub_inbox, "claude-jobs", raw)
    notes.index_inbox()
    db, _ = notes.open_store(write=False)
    try:
        assert GH not in json.dumps(db.execute("SELECT text, note FROM notes").fetchall())
    finally:
        db.close()


def test_trigger_on_the_hub_indexes_notes(hub_inbox, fake_embed, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    config.ensure_data_home()
    config.stamp_path().touch()
    drop(hub_inbox, "claude-jobs", notes.build(title="t", summary="indexed by the unit",
                                               source=code_source()))
    assert trigger._anything_changed(config.stamp_path())
    trigger.run("hub-test", if_changed=True, foreground=True)
    assert notes.search("indexed by the unit", mode="text")
    assert not config.db_path().exists() or not store.existing_hashes(
        store.open_for_read(check_model=False))


# ------------------------------------------------------------ hub shipping

@pytest.fixture
def landing(tmp_path, monkeypatch):
    out = tmp_path / "landing"
    out.mkdir()
    dests = []
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@example.invalid:")

    def upload(files, dest, key):
        dests.append(dest)
        for f in files:
            shutil.copy(f, out / f.name)
            f.unlink()
        return True

    monkeypatch.setattr(ship, "upload", upload)
    return types.SimpleNamespace(dir=out, dests=dests)


def test_with_a_hub_notes_ship_to_the_notes_directory(landing):
    note = notes.build(title="t", summary="goes to the hub", source=code_source())
    assert notes.save(note) == "queued for the hub"
    assert landing.dests == ["ahub@example.invalid:notes/"]
    (sent,) = landing.dir.glob("*.jsonl")
    assert notes.parse(sent.read_text()) is not None
    assert not notes.db_path().exists()


def test_an_unreachable_hub_keeps_notes_queued(landing, monkeypatch):
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    assert "kept" in notes.save(notes.build(title="t", summary="s", source=code_source()))
    assert len(list(notes.outbox_dir().glob("*.jsonl"))) == 1
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: [f.unlink() for f in files] or True)
    assert notes.ship_outbox() == 0
    assert not list(notes.outbox_dir().glob("*.jsonl"))


# -------------------------------------------------------------------- sweep

def session(transcripts, *texts, start_minute=0, name="s1", session_id="sess-1"):
    rows = []
    for n, text in enumerate(texts):
        role = "user" if n % 2 == 0 else "assistant"
        rows.append(msg(role, text, session=session_id,
                        timestamp=f"2026-01-01T00:{start_minute + n:02d}:00Z"))
    return transcripts("proj", name, rows)


def test_sweep_notes_an_idle_session_once(transcripts, fake_embed):
    session(transcripts, "Fix the flaky upload test please",
            "Done: the retry window was too short; raised it to 30s.")
    assert notes.sweep(summarizer="last-message", now=LATER) == 0
    (hit,) = notes.search("retry window", mode="text")
    assert hit["title"] == "Fix the flaky upload test please"
    assert hit["source"] == {"kind": "claude-code", "machine": "testbox",
                             "session_id": "sess-1"}
    assert hit["span"] == {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:01:00Z"}
    assert hit["author"] == "sweep:last-message"
    assert notes.sweep(summarizer="last-message", now=LATER) == 0
    assert len(notes.search("retry window flaky upload", mode="text")) == 1


def test_an_active_session_waits_unless_it_ended(transcripts, fake_embed):
    session(transcripts, "Still working on the migration plan", "Half way through it.")
    soon = datetime.datetime(2026, 1, 1, 0, 3, tzinfo=datetime.timezone.utc)
    notes.sweep(summarizer="last-message", now=soon, idle_minutes=5)
    assert not notes.search("migration plan", mode="text")
    notes.sweep(summarizer="last-message", now=soon, session="sess-1")  # SessionEnd
    assert notes.search("migration plan", mode="text")


def test_a_resumed_session_gets_a_note_for_only_what_is_new(transcripts, fake_embed):
    path = session(transcripts, "Plan the Boat export", "Plan written.")
    notes.sweep(summarizer="last-message", now=LATER)
    with path.open("a") as fh:
        for n, (role, text) in enumerate([("user", "Resume: now ship it to the hub"),
                                          ("assistant", "Shipped and confirmed.")]):
            fh.write(json.dumps(msg(role, text, timestamp=f"2026-01-01T00:3{n}:00Z")) + "\n")
    notes.sweep(summarizer="last-message", now=LATER)
    hits = notes.search("ship hub confirmed resume", mode="text")
    resumed = [h for h in hits if h["title"].startswith("Resume")]
    assert len(resumed) == 1
    assert resumed[0]["span"] == {"start": "2026-01-01T00:30:00Z",
                                  "end": "2026-01-01T00:31:00Z"}


def test_sweep_links_a_claude_p_job(transcripts, fake_embed, tmp_path, monkeypatch):
    jobs = tmp_path / "jobs" / JOB
    jobs.mkdir(parents=True)
    (jobs / "job.json").write_text(json.dumps({"job_id": JOB}))
    (jobs / "transcript.jsonl").write_text(json.dumps({"type": "system", "session_id": "sess-1"}) + "\n")
    monkeypatch.setenv("AGENT_HISTORY_JOBS_DIR", str(tmp_path / "jobs"))
    session(transcripts, "Job ask: tidy the README", "Opened draft PR #7 with the README tidy-up.")
    notes.sweep(summarizer="last-message", now=LATER)
    (hit,) = notes.search("draft PR", mode="text")
    assert hit["source"]["job_id"] == JOB
    assert hit["show"]["session"] == JOB


def test_an_external_summarizer_sees_only_redacted_text(transcripts, fake_embed, tmp_path):
    seen = tmp_path / "seen.json"
    script = tmp_path / "summ.py"
    script.write_text(
        "import json, sys\n"
        "span = json.load(sys.stdin)\n"
        f"open({str(seen)!r}, 'w').write(json.dumps(span))\n"
        "print(json.dumps({'title': 'Token rotation', 'summary': 'Rotated a leaked token.',"
        " 'decisions': ['rotate quarterly']}))\n")
    session(transcripts, f"I pasted {GH} by mistake", "Rotate it now; done.")
    cmd = f"{sys.executable} {script}"
    assert notes.sweep(summarizer=cmd, now=LATER) == 0
    span = json.loads(seen.read_text())
    assert GH not in json.dumps(span) and span["source"]["kind"] == "claude-code"
    (hit,) = notes.search("Token rotation", mode="text")
    assert "rotate quarterly" in hit["excerpt"]


def test_a_failing_summarizer_writes_nothing_and_retries_later(transcripts, fake_embed, tmp_path):
    session(transcripts, "Something worth noting today", "Indeed it was.")
    bad = f"{sys.executable} -c \"import sys; sys.exit(3)\""
    assert notes.sweep(summarizer=bad, now=LATER) == 1
    assert not notes.search("worth noting", mode="text")
    assert notes.sweep(summarizer="last-message", now=LATER) == 0
    assert notes.search("worth noting", mode="text")


def test_sweep_needs_a_summarizer(transcripts):
    assert notes.sweep(now=LATER) == 2


def test_sweep_dry_run_writes_nothing(transcripts, capsys):
    session(transcripts, "A session to preview", "Previewed.")
    assert notes.sweep(summarizer="last-message", now=LATER, dry_run=True) == 0
    assert "would note sess-1" in capsys.readouterr().out
    assert not notes.state_path().exists() and not notes.db_path().exists()


def test_session_end_hook_starts_a_background_sweep(monkeypatch):
    started = []
    monkeypatch.setattr(notes.subprocess, "Popen",
                        lambda argv, **kw: started.append((argv, kw)))
    assert notes.hook_session_end(io.StringIO(json.dumps(
        {"session_id": "abc-123", "hook_event_name": "SessionEnd"}))) == 0
    (argv, kw), = started
    assert argv[-4:] == ["notes", "sweep", "--session", "abc-123"]
    assert notes.hook_session_end(io.StringIO("not json")) == 0
    assert notes.hook_session_end(io.StringIO(json.dumps({"session_id": "bad id;rm"}))) == 0
    assert len(started) == 1


# ---------------------------------------------------------------------- CLI

def test_cli_add_and_search(fake_embed, capsys):
    assert cli.main(["notes", "add", "--title", "Hub notes shipped",
                     "--summary", "Notes now live apart from transcripts.",
                     "--decision", "default notes=exclude", "--chat-url", URL]) == 0
    assert cli.main(["notes", "search", "apart from transcripts", "--mode", "text"]) == 0
    out = capsys.readouterr().out
    assert "Hub notes shipped" in out and "show: 0f6a7c2e" in out
    assert cli.main(["notes", "add", "--title", "t", "--summary", "s"]) == 2


def test_purge_and_reindex_do_not_reach_notes(fake_embed, transcripts):
    """The transcript index can be rebuilt; notes are a separate file."""
    notes.save(notes.build(title="t", summary="survives a reindex", source=code_source()))
    transcripts("proj", "s", [msg("user", "a transcript message for the index")])
    if store.extensions_supported():
        index.run()
        index.run(reindex=True, force=True)
    assert notes.search("survives a reindex", mode="text")
