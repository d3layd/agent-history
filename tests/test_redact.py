"""Secret redaction at ingest, and `scrub` for indexes built before it.

Fixture secrets are assembled at runtime so no token-shaped literal sits in
the source tree (secret scanners would rightly object).
"""
from __future__ import annotations

import pytest

from agent_history import index, redact, scrub, store, textproc
from conftest import msg

R = "".join  # build fake secrets from parts

GH = R(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])
GH_PAT = R(["github", "_pat_", "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKL"])
ANTHROPIC = R(["sk-", "ant-", "api03-", "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0"])
OPENAI = R(["sk-", "proj-", "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4"])
AWS = R(["AK", "IA", "QWERTYUIOPASDFGH"])
SLACK = R(["xo", "xb-", "123456789012-abcdefABCDEF"])
STRIPE = R(["sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc"])
GOOGLE = R(["AI", "za", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"])
JWT = R(["ey", "JhbGciOiJIUzI1NiJ9.", "ey", "JzdWIiOiIxMjM0NTY3ODkwIn0.",
         "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"])
PEM = R(["-----BEGIN RSA PRIVATE KEY-----\nMIIEow", "IBAAKCAQEAx\nabc123\n",
         "-----END RSA PRIVATE KEY-----"])


@pytest.mark.parametrize("text, cls", [
    (f"token is {GH} ok", "github_token"),
    (f"GH={GH_PAT}", "github_token"),
    (f"key {ANTHROPIC}", "anthropic_key"),
    (f"OPENAI_API_KEY={OPENAI}", "openai_key"),
    (f"aws {AWS} here", "aws_access_key"),
    (f"slack {SLACK}", "slack_token"),
    (f"stripe {STRIPE}", "stripe_key"),
    (f"maps {GOOGLE}", "google_api_key"),
    (f"cookie {JWT}", "jwt"),
    (f"before\n{PEM}\nafter", "private_key"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "bearer"),
    ("DATABASE_URL=postgres://app:s3cr3tP4ss@db:5432/main", "url_password"),
    ('{"password": "hunter2hunter"}', "assignment"),
    ("mysql --password=Tr0ub4dor&3 -u root", "assignment"),
    ("client_secret: aB3dE5gH7jK9mN1p", "assignment"),
])
def test_each_class_is_redacted(text, cls):
    out, counts = redact.redact(text)
    assert f"[REDACTED:{cls}]" in out
    assert counts[cls] >= 1


def test_only_the_secret_part_is_replaced():
    out, _ = redact.redact("DATABASE_URL=postgres://app:s3cr3tP4ss@db:5432/main")
    assert out == "DATABASE_URL=postgres://app:[REDACTED:url_password]@db:5432/main"
    out, _ = redact.redact('"password": "hunter2hunter"')
    assert out == '"password": "[REDACTED:assignment]"'


def test_specific_class_beats_the_generic_ones():
    # An Anthropic key also looks like an OpenAI key and a generic assignment.
    out, counts = redact.redact(f"ANTHROPIC_API_KEY={ANTHROPIC}")
    assert out == "ANTHROPIC_API_KEY=[REDACTED:anthropic_key]"
    assert set(counts) == {"anthropic_key"}


@pytest.mark.parametrize("text", [
    "commit 8112c54e9f0a1b2c3d4e5f60718293a4b5c6d7e8 fixed it",
    "request id 3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "the password is required on first login",
    "set a password: please choose a strong one",
    "token = req.headers.authorization",
    "max_tokens=4096",
    "password: z.string().min(8)",
    "secret=${SECRET}",
    "API_KEY=<your key here>",
    "password=xxxxxxxx",
    "sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "base64 aGVsbG8gd29ybGQ=",
    "sudo: linuxuser : PWD=/home/linuxuser/apps ; USER=root",
    "MYSQL_ROOT_PASSWORD=${DB_ROOT_PASSWORD:?DB_ROOT_PASSWORD must be set}",
    "export GITHUB_TOKEN=GH_TOKEN_FROM_VAULT",
    "tokens embedded in URLs (https://user:token@github.com) leak",
    "postgresql://agent:***@localhost:5432/crm",
])
def test_near_misses_survive(text):
    assert redact.redact(text)[0] == text


def test_redaction_is_idempotent():
    once, _ = redact.redact(f"a {GH} b postgres://u:p4ssw0rdX@h/db")
    twice, counts = redact.redact(once)
    assert twice == once and not counts


def test_a_secret_cut_by_a_chunk_boundary_is_redacted_in_both_chunks(monkeypatch):
    monkeypatch.setattr(textproc, "MAX_CHUNK_CHARS", 60)
    monkeypatch.setattr(textproc, "CHUNK_OVERLAP", 10)
    text = "x" * 40 + " " + GH + " " + "y" * 40
    pieces = index.redacted_chunks(text)
    assert len(pieces) > 1
    assert not any(GH[:12] in p for p in pieces), "no part of the token may survive"
    assert sum("[REDACTED:github_token]" in p for p in pieces) >= 2


def test_boundaries_do_not_move_when_redaction_turns_on(monkeypatch):
    monkeypatch.setattr(textproc, "MAX_CHUNK_CHARS", 60)
    monkeypatch.setattr(textproc, "CHUNK_OVERLAP", 10)
    text = "a" * 20 + " " + GH + " " + "b" * 150
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    plain = index.redacted_chunks(text)
    monkeypatch.delenv("AGENT_HISTORY_REDACT")
    red = index.redacted_chunks(text)
    assert len(plain) == len(red)
    # Chunks that never held the secret are byte-identical, so their hashes
    # (and embeddings) are reused rather than duplicated.
    assert plain[-1] == red[-1]


# --- end to end ------------------------------------------------------------

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def _texts():
    db = store.open_for_read()
    return [row[0] for row in db.execute("SELECT text FROM chunks")]


def _fts_hits(term):
    db = store.open_for_read()
    return db.execute(
        "SELECT count(*) FROM fts_chunks WHERE fts_chunks MATCH ?", (f'"{term}"',)
    ).fetchone()[0]


@needs_vec
def test_secrets_never_reach_the_index_or_the_embedder(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", f"push failed, my token is {GH} please fix")])
    assert index.run() == 0
    assert all(GH not in t for t in _texts())
    assert all(GH not in call for call in fake_embed), "embedder must not see it"
    assert any("[REDACTED:github_token]" in t for t in _texts())


@needs_vec
def test_redaction_can_be_turned_off(transcripts, fake_embed, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    transcripts("proj", "s", [msg("user", f"push failed, my token is {GH} please fix")])
    index.run()
    assert any(GH in t for t in _texts())


@needs_vec
def test_last_run_counts_are_recorded(transcripts, fake_embed):
    import json
    transcripts("proj", "s", [msg("user", f"key {AWS} and url postgres://u:pa55wordZ@h/d")])
    index.run()
    db = store.open_for_read()
    data = json.loads(store.get_meta(db, "redact_last"))
    assert data["enabled"] is True
    assert data["counts"] == {"aws_access_key": 1, "url_password": 1}


def _index_unredacted(transcripts, monkeypatch, text, name="s"):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    transcripts("proj", name, [msg("user", text)])
    index.run()
    monkeypatch.delenv("AGENT_HISTORY_REDACT")


@needs_vec
def test_scrub_previews_without_changing_anything(transcripts, fake_embed, monkeypatch, capsys):
    _index_unredacted(transcripts, monkeypatch, f"old session leaked {GH} here")
    calls = len(fake_embed)
    assert scrub.run(yes=False) == 0
    assert "1 chunk(s) contain secrets" in capsys.readouterr().out
    assert any(GH in t for t in _texts())
    assert len(fake_embed) == calls


@needs_vec
def test_scrub_rewrites_text_fts_and_vector(transcripts, fake_embed, monkeypatch):
    _index_unredacted(transcripts, monkeypatch, f"old session leaked {GH} here")
    db = store.open_for_read()
    (rowid,) = db.execute("SELECT id FROM chunks").fetchone()
    assert _fts_hits(GH) == 1
    assert scrub.run(yes=True) == 0
    texts = _texts()
    assert texts == ["old session leaked [REDACTED:github_token] here"]
    assert _fts_hits(GH) == 0 and _fts_hits("REDACTED") == 1
    db = store.open_for_read()
    assert db.execute("SELECT id FROM chunks").fetchone()[0] == rowid, "id kept"
    assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 1
    assert fake_embed[-1] == texts[0], "the vector must come from the new text"


@needs_vec
def test_scrub_covers_orphaned_chunks(transcripts, fake_embed, monkeypatch):
    path = None
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    path = transcripts("proj", "gone", [msg("user", f"deleted later {GH} text")])
    index.run()
    monkeypatch.delenv("AGENT_HISTORY_REDACT")
    path.unlink()
    assert scrub.run(yes=True) == 0
    assert all(GH not in t for t in _texts())


@needs_vec
def test_reindex_after_scrub_converges_without_duplicates(transcripts, fake_embed, monkeypatch):
    _index_unredacted(transcripts, monkeypatch, f"old session leaked {GH} here")
    scrub.run(yes=True)
    index.run()
    db = store.open_for_read()
    assert store.stats(db)["total"] == 1


@needs_vec
def test_scrub_dedups_when_the_redacted_copy_already_exists(transcripts, fake_embed, monkeypatch):
    _index_unredacted(transcripts, monkeypatch, f"old session leaked {GH} here")
    index.run()  # redaction on: the redacted copy is indexed alongside
    db = store.open_for_read()
    assert store.stats(db)["total"] == 2
    assert scrub.run(yes=True) == 0
    db = store.open_for_read()
    assert store.stats(db)["total"] == 1
    assert _fts_hits(GH) == 0


@needs_vec
def test_scrub_is_idempotent(transcripts, fake_embed, monkeypatch, capsys):
    _index_unredacted(transcripts, monkeypatch, f"old session leaked {GH} here")
    scrub.run(yes=True)
    calls = len(fake_embed)
    capsys.readouterr()
    assert scrub.run(yes=True) == 0
    assert "no secrets found" in capsys.readouterr().out
    assert len(fake_embed) == calls
