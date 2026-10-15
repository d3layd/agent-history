"""Chunk stitching: secrets that straddle a chunk boundary, in `scrub` and in
`ship --backfill-from-index`, are redacted whole.

Chunks are 1500 chars with a 200 char overlap, so a secret longer than the
overlap can sit across a boundary with neither chunk holding all of it.
"""
from __future__ import annotations

import base64
import random

import pytest

from agent_history import config, index, redact, scrub, ship, store, stitch, textproc
from agent_history.stitch import Chunk
from conftest import msg
from test_ship import hub, shipped  # noqa: F401  (fixture)

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

OVERLAP = textproc.CHUNK_OVERLAP
MAX = textproc.MAX_CHUNK_CHARS


def _b64(n: int, seed: int) -> str:
    rng = random.Random(seed)
    raw = bytes(rng.randrange(256) for _ in range(n))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + "eyJ" + _b64(180, 1) + "." + _b64(32, 2))
PEM_BODY = "\n".join(_b64(48, 10 + i) for i in range(14))
PEM = ("-----BEGIN RSA PRIVATE KEY-----\n" + PEM_BODY
       + "\n-----END RSA PRIVATE KEY-----")


def _filler(n: int) -> str:
    words = "the quick brown fox jumps over a lazy dog and then rests ".split()
    out, i = "", 0
    while len(out) < n:
        out += words[i % len(words)] + " "
        i += 1
    return out[:n]


def straddling(secret: str, start: int) -> str:
    """Prose with ``secret`` starting at ``start``, then prose to a third chunk."""
    return _filler(start) + "\n" + secret + " " + _filler(2200)


def _spans_both(text: str, secret: str) -> None:
    s = text.index(secret)
    a, b = textproc.chunk_spans(text)[:2]
    # The scenario: neither of the first two windows holds the whole secret.
    assert not (a[0] <= s and s + len(secret) <= a[1])
    assert not (b[0] <= s and s + len(secret) <= b[1])


# --- unit: when chunks join ---------------------------------------------------

LONG = _filler(5000)


def _chunks(text, **over):
    out = []
    for i, (s, e) in enumerate(textproc.chunk_spans(text)):
        base = dict(id=100 + i, agent="a", origin="o", session_id="s", cwd="/w",
                    timestamp="t", role="user", text=text[s:e], machine="m",
                    session_uid=None)
        base.update(over)
        out.append(Chunk(**base))
    return out


def _join(rows):
    return list(stitch.stitch(rows))


def _sizes(rows):
    return [len(r.chunks) for r in _join(rows)]


def test_chunks_of_one_record_rejoin_exactly():
    (rec,) = _join(_chunks(LONG))
    assert rec.text == LONG and rec.stitched
    assert [(s, e) for _, s, e in rec.spans] == textproc.chunk_spans(LONG)
    assert [i for i, _, _ in rec.spans] == [100, 101, 102, 103]


def test_short_record_is_a_record_of_one():
    (rec,) = _join(_chunks("short"))
    assert rec.text == "short" and not rec.stitched


def test_refuses_on_overlap_mismatch():
    rows = _chunks(LONG)
    t = rows[1].text
    rows[1] = rows[1]._replace(text=t[:OVERLAP - 1] + "X" + t[OVERLAP:])
    assert _sizes(rows) == [1, 3]
    rows = _chunks(LONG)
    rows[1] = rows[1]._replace(text="X" + rows[1].text[1:])
    assert _sizes(rows) == [1, 3]


def test_refuses_on_id_gap():
    rows = _chunks(LONG)
    rows[2] = rows[2]._replace(id=rows[2].id + 7)
    assert _sizes(rows) == [2, 1, 1]


@pytest.mark.parametrize("field, value", [
    ("role", "assistant"), ("timestamp", "t2"), ("origin", "o2"),
    ("session_id", "s2"), ("machine", "m2"), ("agent", "b"),
])
def test_refuses_on_metadata_mismatch(field, value):
    rows = _chunks(LONG)
    rows[1] = rows[1]._replace(**{field: value})
    assert _sizes(rows) == [1, 1, 2]


def test_refuses_when_a_non_final_window_is_short():
    rows = _chunks(LONG)
    rows[0] = rows[0]._replace(text=rows[0].text[:-1])
    assert _sizes(rows)[0] == 1


def test_text_less_chunk_breaks_the_chain():
    rows = _chunks(LONG)
    rows[1] = rows[1]._replace(text=None)
    assert _sizes(rows) == [1, 2]


def _prov(rows, rid="r1"):
    return [c._replace(record_id=rid, ordinal=i, start=s, end=e)
            for i, ((s, e), c) in enumerate(zip(textproc.chunk_spans(LONG), rows))]


def _no_prov(rows):
    return [r._replace(record_id=None, ordinal=None, start=None, end=None)
            for r in rows]


def test_provenance_joins_by_offsets_where_ids_are_not_consecutive():
    rows = [r._replace(id=r.id * 3) for r in _prov(_chunks(LONG))]
    assert _sizes(_no_prov(rows)) == [1, 1, 1, 1]   # the heuristic refuses
    (rec,) = _join(rows)
    assert rec.text == LONG
    assert rec.head_anchored and rec.tail_anchored


def test_provenance_refuses_ordinal_gap_and_record_id_mismatch():
    rows = [r._replace(id=r.id * 3) for r in _prov(_chunks(LONG))]
    gap = list(rows)
    gap[2] = gap[2]._replace(ordinal=5)
    assert _sizes(gap) == [2, 1, 1]
    other = list(rows)
    other[2] = other[2]._replace(record_id="r2")
    assert _sizes(other) == [2, 1, 1]


def test_provenance_uses_the_heuristic_when_text_no_longer_matches_offsets():
    rows = _prov(_chunks(LONG))
    # Chunk 3 was rewritten to a length that disagrees with its offsets; the
    # overlap with chunk 2 is untouched, so consecutive ids still join.
    rows[3] = rows[3]._replace(text=rows[3].text + "!")
    (rec,) = _join(rows)
    assert rec.text == LONG + "!"
    assert rec.head_anchored


# --- unit: the fragment safety net -------------------------------------------

TOK = "Zm9vYmFyQmF6" * 4  # 48 chars, mixed case + digits


def test_guard_redacts_runs_touching_either_edge():
    assert stitch.guard_edges("hello " + TOK) == "hello [REDACTED:fragment]"
    assert stitch.guard_edges(TOK + " world") == "[REDACTED:fragment] world"
    assert stitch.guard_edges(TOK) == "[REDACTED:fragment]"
    assert stitch.guard_edges(TOK + " mid " + TOK) == \
        "[REDACTED:fragment] mid [REDACTED:fragment]"


def test_guard_leaves_ordinary_text_alone():
    sha = "a3f5c2d9e1b84706fa3b9c2d1e0f4a5b6c7d8e9f"
    assert len(sha) == 40
    prose = "just ordinary words " * 10
    assert stitch.guard_edges(prose) == prose
    mid = "commit " + sha + " fixed the bug"          # a git SHA mid-chunk
    assert stitch.guard_edges(mid) == mid
    both = "see " + sha + " and " + sha + " there"
    assert stitch.guard_edges(both) == both
    assert stitch.guard_edges("x " + TOK[:39]) == "x " + TOK[:39]   # too short
    slug = "my-very-long-kebab-case-identifier-for-a-thing-here"
    assert stitch.guard_edges(slug) == slug                         # one class
    assert stitch.guard_edges("=" * 60) == "=" * 60


def test_guard_is_idempotent_and_respects_flags():
    once = stitch.guard_edges(TOK + " x " + TOK)
    assert stitch.guard_edges(once) == once
    assert stitch.guard_edges(TOK + " x", head=False) == TOK + " x"
    assert stitch.guard_edges("x " + TOK, tail=False) == "x " + TOK


# --- index-level: scrub and backfill -------------------------------------------

def _drop_provenance():
    db, _ = store.open_for_write()
    db.execute("UPDATE chunks SET record_id=NULL, chunk_ordinal=NULL, "
               "char_start=NULL, char_end=NULL")
    db.commit()
    db.close()


def _texts():
    db = store.open_for_read()
    out = [r[0] for r in db.execute("SELECT text FROM chunks ORDER BY id")]
    db.close()
    return out


def _fragments(secret: str) -> list[str]:
    """Distinctive 24-char pieces of ``secret`` that must never survive."""
    return [secret[i:i + 24] for i in range(30, len(secret) - 30, 40)]


def _index_unredacted(transcripts, monkeypatch, text, name="s"):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    path = transcripts("proj", name, [msg("user", text, session=f"sess-{name}")])
    index.run()
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "on")
    return path


CASES = [("jwt", JWT, 1250, "jwt"), ("pem", PEM, 800, "private_key")]


@needs_vec
@pytest.mark.parametrize("provenance", [True, False])
@pytest.mark.parametrize("name, secret, start, cls", CASES, ids=[c[0] for c in CASES])
def test_scrub_redacts_a_secret_across_a_boundary(
        transcripts, fake_embed, monkeypatch, name, secret, start, cls, provenance):
    text = straddling(secret, start)
    _spans_both(text, secret)
    _index_unredacted(transcripts, monkeypatch, text)
    if not provenance:
        _drop_provenance()
    # Per-chunk redaction (what scrub did before) leaves fragments behind.
    old = [redact.redact(t)[0] for t in _texts()]
    assert any(f in t for t in old for f in _fragments(secret))

    assert scrub.run(yes=True) == 0
    texts = _texts()
    for t in texts:
        for f in _fragments(secret):
            assert f not in t
    assert any("[REDACTED:%s]" % cls in t for t in texts)
    assert texts[0].startswith(_filler(100)) and len(texts) == 3


@needs_vec
@pytest.mark.parametrize("provenance", [True, False])
def test_scrub_is_idempotent_and_deletes_nothing_extra(
        transcripts, fake_embed, monkeypatch, provenance):
    _index_unredacted(transcripts, monkeypatch, straddling(JWT, 1250))
    _index_unredacted(transcripts, monkeypatch, "an ordinary message " * 5, name="o")
    if not provenance:
        _drop_provenance()
    before = _texts()
    assert scrub.run(yes=True) == 0
    after = _texts()
    assert len(after) == len(before)          # rewrote in place, deleted none
    embeds = len(fake_embed)
    assert scrub.run(yes=True) == 0
    assert _texts() == after and len(fake_embed) == embeds


@needs_vec
def test_scrub_falls_back_per_chunk_when_it_cannot_stitch(
        transcripts, fake_embed, monkeypatch):
    _index_unredacted(transcripts, monkeypatch, straddling(JWT, 1250))
    _drop_provenance()
    db, _ = store.open_for_write()
    mid = db.execute("SELECT id FROM chunks ORDER BY id LIMIT 1 OFFSET 1").fetchone()[0]
    db.execute("UPDATE chunks SET role='assistant' WHERE id=?", (mid,))
    db.commit()
    db.close()
    assert scrub.run(yes=True) == 0
    # Not stitched, so the first chunk's cut-off JWT is caught only by the
    # edge net: its final dot-segment is a long run touching the window end.
    assert "[REDACTED:fragment]" in _texts()[0]


@needs_vec
@pytest.mark.parametrize("provenance", [True, False])
@pytest.mark.parametrize("name, secret, start, cls", CASES, ids=[c[0] for c in CASES])
def test_backfill_redacts_a_secret_across_a_boundary(
        transcripts, fake_embed, hub, monkeypatch, name, secret, start, cls,
        provenance):
    text = straddling(secret, start)
    path = _index_unredacted(transcripts, monkeypatch, text)
    path.unlink()
    if not provenance:
        _drop_provenance()
    assert ship.run(backfill=True) == 0
    (rec,) = shipped(hub)                      # one whole record, not chunks
    for f in _fragments(secret):
        assert f not in rec["text"]
    assert rec["text"] == redact.redact(text)[0]


@needs_vec
@pytest.mark.parametrize("provenance", [True, False])
def test_backfill_equals_ship_so_the_hub_can_dedup(
        transcripts, fake_embed, hub, monkeypatch, provenance):
    text = straddling(JWT, 1250)
    _index_unredacted(transcripts, monkeypatch, text)
    if not provenance:
        _drop_provenance()
    assert ship.run() == 0                     # from the transcript on disk
    (disk,) = shipped(hub)
    # Same record hash means the same text, so nothing new is sent.
    assert ship.run(backfill=True) == 0
    assert len(shipped(hub)) == 1
    # And with fresh state, the backfill record carries the same content.
    config.ship_state_path().unlink()
    for f in hub.glob("*.jsonl"):
        f.unlink()
    ship.run(backfill=True)
    (back,) = shipped(hub)
    for key in ("agent", "origin", "session_id", "timestamp", "role", "text",
                "machine"):
        assert back[key] == disk[key]


@needs_vec
def test_backfill_keeps_the_machine_filter_and_watermark_across_records(
        transcripts, fake_embed, hub, monkeypatch):
    _index_unredacted(transcripts, monkeypatch, straddling(JWT, 1250), name="a")
    db, _ = store.open_for_write()
    store.insert_chunk(
        db, agent="codex", origin="/o", session_id="s", cwd="/w",
        timestamp="2026-01-01T00:00:00Z", role="user",
        text="someone else's history", content_hash="h",
        vector=[0.0] * 8, machine="elsewhere")
    db.commit()
    db.close()
    ship.run(backfill=True)
    assert len(shipped(hub)) == 1
    ship.run(backfill=True)
    assert len(shipped(hub)) == 1
    _index_unredacted(transcripts, monkeypatch, "a later message to index", name="b")
    ship.run(backfill=True)
    assert len(shipped(hub)) == 2


@needs_vec
@pytest.mark.parametrize("provenance", [True, False])
def test_stitch_coverage_on_an_index_built_with_the_real_chunker(
        transcripts, fake_embed, monkeypatch, provenance):
    rng = random.Random(7)
    texts = []
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    for i in range(24):
        n = rng.choice([50, 900, 1499, 1500, 1501, 1700, 2900, 3100, 6000])
        rr = random.Random(i)
        t = f"R{i} " + " ".join(
            "".join(rr.choice("abcdefghij") for _ in range(6)) for _ in range(n // 7 + 1))
        t = t[:n].strip()
        texts.append(t)
        transcripts("proj", f"s{i}", [msg("user", t, session=f"sess-{i}")])
    index.run()
    if not provenance:
        _drop_provenance()
    db = store.open_for_read()
    rows = [Chunk(*r) for r in db.execute(
        f"SELECT {stitch.COLUMNS} FROM chunks ORDER BY id")]
    db.close()
    assert sorted(r.text for r in _join(rows)) == sorted(texts)
