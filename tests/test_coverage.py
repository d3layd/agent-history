"""`doctor --coverage`: on-disk sources versus the index, and torn transcripts."""
from __future__ import annotations

import json
import os
import time

import pytest

from agent_history import cli, config, coverage, index, store
from conftest import msg

SESSION = "d2a0a46d-16c2-52f8-846c-dcf0fdc07f81"
TEXT = "a conversation turn long enough to be indexed"

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def _suspect(report: dict, name: str) -> dict:
    hits = [s for s in report["suspects"] if s["path"].endswith(name)]
    assert len(hits) == 1, report["suspects"]
    return hits[0]


def test_zero_byte_transcript_is_flagged_with_its_bridge_id(isolated_env):
    project = isolated_env / "projects" / "-home-me"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(b"")
    # Claude Code's per-process metadata, beside projects/.
    sessions = isolated_env / "sessions"
    sessions.mkdir()
    (sessions / "10992.json").write_text(json.dumps({
        "pid": 10992, "sessionId": SESSION,
        "bridgeSessionId": "session_01JMAa72eTQ5jd3fMh6bD2dN",
    }))

    report = coverage.collect()

    hit = _suspect(report, f"{SESSION}.jsonl")
    assert hit["problem"] == "empty"
    assert hit["bridge_session_id"] == "session_01JMAa72eTQ5jd3fMh6bD2dN"


def test_truncated_last_line_is_flagged(transcripts):
    path = transcripts("-work", "torn", [msg("user", TEXT)])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assistant", "message": {"content": "cut of')

    hit = _suspect(coverage.collect(), "torn.jsonl")
    assert hit["problem"] == "truncated"


def test_healthy_transcript_is_not_a_suspect(transcripts):
    # Big enough not to count as short, and ending in a complete line.
    transcripts("-work", SESSION, [msg("user", TEXT * 20)] * 5)
    assert coverage.collect()["suspects"] == []


def test_sidecar_logs_are_not_called_short(transcripts):
    transcripts("-work/x/subagents/workflows/wf", "journal",
                [{"type": "started"}])
    assert coverage.collect()["suspects"] == []


def test_without_an_index_every_surface_warns(transcripts):
    transcripts("-work", SESSION, [msg("user", TEXT)])
    report = coverage.collect()
    assert report["index_present"] is False
    (surface,) = report["surfaces"]
    assert surface["warning"] == "no index"


@needs_vec
def test_lag_is_detected_when_source_outruns_the_index(transcripts, fake_embed):
    path = transcripts("-work", SESSION, [
        msg("user", TEXT, timestamp="2026-01-01T00:00:00Z")])
    written = coverage._epoch("2026-01-01T00:00:00Z")
    os.utime(path, (written, written))  # last write == last indexed turn
    assert index.run() == 0

    report = coverage.collect(lag_hours=6)
    (surface,) = report["surfaces"]
    assert surface["indexed"] == 1
    assert surface["warning"] is None

    # A day of new turns that never reached the index.
    later = surface["newest_indexed"] + 24 * 3600
    os.utime(path, (later, later))

    (surface,) = coverage.collect(lag_hours=6)["surfaces"]
    assert surface["lag_hours"] == pytest.approx(24, abs=0.1)
    assert "lags" in surface["warning"]
    # Within a wider threshold the same gap is fine.
    (surface,) = coverage.collect(lag_hours=48)["surfaces"]
    assert surface["warning"] is None


@needs_vec
def test_coverage_never_writes_the_index(transcripts, fake_embed):
    transcripts("-work", SESSION, [msg("user", TEXT)])
    assert index.run() == 0
    db = config.db_path()
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    time.sleep(0.01)
    coverage.collect()
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before


def test_cli_json_and_exit_code(isolated_env, capsys):
    project = isolated_env / "projects" / "-home-me"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(b"")

    code = cli.main(["doctor", "--coverage", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 1, "an empty transcript needs attention"
    assert report["suspects"][0]["problem"] == "empty"
    assert {s["surface"] for s in report["unindexed_surfaces"]} >= {
        "claude.ai chats", "Claude Desktop Cowork"}


def test_cli_text_report_names_the_problem(isolated_env, capsys):
    project = isolated_env / "projects" / "-home-me"
    project.mkdir()
    (project / f"{SESSION}.jsonl").write_bytes(b"")

    cli.main(["doctor", "--coverage"])

    out = capsys.readouterr().out
    assert "EMPTY" in out and SESSION in out
    assert "not indexed by any adapter" in out
