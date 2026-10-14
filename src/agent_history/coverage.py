"""Coverage report — what is on disk versus what reached the index.

A search that silently misses is worse than one that fails loudly. Crashes
leave transcripts at 0 bytes or cut off mid-line, a Windows-side directory
stops being picked up, a hook stops firing — and nothing says so until the
one search that needed that conversation comes back empty. This report puts
the gaps in front of you before then.

It is strictly read-only: the index is opened with SQLite's `mode=ro`, no
vector extension is loaded, and transcript files are only ever read. Source
discovery goes through the adapters themselves, so the report always looks
exactly where indexing looks.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import urllib.parse
import uuid
from pathlib import Path

from . import adapters, config

# A surface whose newest source is this much newer than its newest indexed
# chunk is reported as lagging. Hooks index within minutes, so hours of lag
# means indexing is not reaching that surface at all.
LAG_WARN_HOURS = 6.0

# Transcripts under this size hold no real conversation — typically a title
# or bridge stub whose actual turns never reached disk.
SHORT_BYTES = 2048

# How far back to look for the final line of a transcript before giving up.
# Single lines can be large (tool output), but not this large.
TAIL_CAP = 16 * 1024 * 1024

# How many leading lines of a suspect transcript to scan for its
# remote-control bridge id.
BRIDGE_SCAN_LINES = 50

# Database-backed adapters: the table holding one row per session.
_SESSION_TABLES = {"opencode": "session", "hermes": "sessions"}

# Adapters whose sources are JSONL transcripts that can be torn by a crash.
_TRANSCRIPT_AGENTS = {"claude-code", "codex", "openclaw"}

# Places conversations happen that no adapter can read from this machine.
UNINDEXED_SURFACES = [
    ("claude.ai chats", "stored server-side; no local transcript"),
    ("claude.ai/code cloud sessions", "run remotely; nothing written here"),
    ("Claude Desktop Cowork", "no adapter yet"),
    ("fleet machines", "index on each host, or sync their transcripts into "
                       "AGENT_HISTORY_EXTRA_DIRS"),
]


# ----------------------------------------------------------------- the index

def _open_ro(path: Path) -> sqlite3.Connection:
    # Percent-encode: '#', '?' or '%' in a path would otherwise be parsed as
    # URI syntax and open the wrong file.
    return sqlite3.connect(
        f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)


def _indexed_origins(path: Path) -> dict[str, dict] | None:
    """Per-origin chunk count, newest timestamp, and session count.

    None when there is no index to read.
    """
    if not path.is_file():
        return None
    db = _open_ro(path)
    try:
        rows = db.execute(
            "SELECT origin, count(*), max(timestamp), count(DISTINCT session_id) "
            "FROM chunks GROUP BY origin"
        ).fetchall()
    finally:
        db.close()
    return {
        origin: {"chunks": chunks, "newest": _epoch(newest), "sessions": sessions}
        for origin, chunks, newest, sessions in rows
        if origin
    }


def _epoch(stamp: str | None) -> float | None:
    """Parse a stored ISO timestamp; naive ones are local time."""
    if not stamp:
        return None
    try:
        # fromisoformat() only accepts a trailing 'Z' from Python 3.11.
        parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()  # naive datetimes are treated as local


# ------------------------------------------------------------------ surfaces

def _source_files(adapter) -> list[Path] | None:
    """The files an adapter would read, or None if it is database-backed."""
    watch = getattr(adapter, "watch_files", None)
    if callable(watch):
        return list(watch())
    for attr in ("_transcript_files", "_rollout_files"):
        files = getattr(adapter, attr, None)
        if callable(files):
            return list(files())
    return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _under(path: str, root: Path) -> bool:
    prefix = str(root)
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


def _file_surfaces(adapter, files: list[Path], origins: dict) -> list[dict]:
    """One surface per root, each file assigned to the first root holding it."""
    roots = adapter.roots()
    grouped: dict[Path, list[Path]] = {root: [] for root in roots}
    for path in files:
        for root in roots:
            if _under(str(path), root):
                grouped[root].append(path)
                break

    surfaces = []
    for root, members in grouped.items():
        mtimes = [m for m in map(_mtime, members) if m is not None]
        indexed = [origins[str(p)] for p in members if str(p) in origins]
        newest_indexed = max(
            (o["newest"] for o in indexed if o["newest"] is not None),
            default=None)
        # Indexed sources whose file has since vanished: the index is now the
        # only copy, which reindex would destroy.
        gone = sum(
            1 for origin in origins
            if _under(origin, root) and not Path(origin).exists())
        surfaces.append({
            "agent": adapter.name,
            "root": str(root),
            "kind": "files",
            "on_disk": len(members),
            "indexed": len(indexed),
            "gone_from_disk": gone,
            "newest_source": max(mtimes, default=None),
            "newest_indexed": newest_indexed,
        })
    return surfaces


def _db_surfaces(adapter, origins: dict) -> list[dict]:
    """One surface per database file; sessions are counted inside it."""
    surfaces = []
    table = _SESSION_TABLES.get(adapter.name)
    for path in adapter.roots():
        on_disk = None
        if table:
            try:
                db = _open_ro(path)
                try:
                    on_disk = db.execute(
                        f"SELECT count(*) FROM {table}").fetchone()[0]
                finally:
                    db.close()
            except sqlite3.Error:
                on_disk = None
        mine = [o for origin, o in origins.items()
                if origin.startswith(f"db:{path}#")]
        # SQLite in WAL mode writes the -wal first; it is the fresher clock.
        mtimes = [m for m in (_mtime(path), _mtime(Path(f"{path}-wal")))
                  if m is not None]
        surfaces.append({
            "agent": adapter.name,
            "root": str(path),
            "kind": "sessions",
            "on_disk": on_disk,
            "indexed": sum(o["sessions"] for o in mine),
            "gone_from_disk": 0,
            "newest_source": max(mtimes, default=None),
            "newest_indexed": max(
                (o["newest"] for o in mine if o["newest"] is not None),
                default=None),
        })
    return surfaces


def _judge(surface: dict, lag_hours: float, have_index: bool) -> None:
    """Attach `lag_hours` and a `warning` (None when healthy)."""
    newest_source = surface["newest_source"]
    newest_indexed = surface["newest_indexed"]
    lag = None
    if newest_source is not None and newest_indexed is not None:
        lag = max(0.0, (newest_source - newest_indexed) / 3600)
    surface["lag_hours"] = None if lag is None else round(lag, 1)

    warning = None
    if not have_index:
        warning = "no index"
    elif surface["on_disk"] and not surface["indexed"]:
        warning = "nothing indexed"
    elif lag is not None and lag > lag_hours:
        warning = f"index lags source by {_duration(lag)}"
    surface["warning"] = warning


# ------------------------------------------------------------------ suspects

def _last_line(path: Path, size: int) -> bytes | None:
    """The final non-empty line, read backwards; None if it is too long."""
    with path.open("rb") as handle:
        buf = b""
        pos = size
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            handle.seek(pos)
            buf = handle.read(step) + buf
            body = buf.rstrip(b"\r\n")
            cut = body.rfind(b"\n")
            if cut != -1:
                return body[cut + 1:]
            if len(buf) > TAIL_CAP:
                return None
        return buf.rstrip(b"\r\n")


def _is_session_file(agent: str, path: Path) -> bool:
    """Whether a file is a whole session rather than a sidecar log.

    Claude Code keeps small non-transcript JSONL beside its sessions
    (workflow journals, plugin logs); only <session-uuid>.jsonl files are
    expected to hold a conversation, so only they can be "short".
    """
    if agent != "claude-code":
        return True
    try:
        uuid.UUID(path.stem)
    except ValueError:
        return False
    return True


def _inspect(path: Path, allow_short: bool = True) -> dict | None:
    """Classify one transcript: empty, truncated, short — or None if fine."""
    try:
        stat = path.stat()
    except OSError:
        return None
    problem = None
    if stat.st_size == 0:
        problem = "empty"
    else:
        try:
            line = _last_line(path, stat.st_size)
        except OSError:
            line = None
        if line:
            try:
                json.loads(line)
            except ValueError:
                problem = "truncated"
        if problem is None and allow_short and stat.st_size < SHORT_BYTES:
            problem = "short"
    if problem is None:
        return None
    return {"problem": problem, "path": str(path), "bytes": stat.st_size,
            "mtime": stat.st_mtime}


def _bridge_from_sessions(roots: list[Path]) -> dict[str, str]:
    """Map Claude Code session id -> remote-control bridge session id.

    Claude Code records per-process metadata in ~/.claude/sessions/<pid>.json,
    including `bridgeSessionId` for remote-control sessions — the handle a
    later backfill needs to re-fetch a transcript that never reached disk.
    """
    dirs = {config.claude_projects_dir().parent / "sessions"}
    dirs |= {root.parent / "sessions" for root in roots if root.name == "projects"}
    mapping: dict[str, str] = {}
    for directory in sorted(dirs):
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in files:
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            session, bridge = meta.get("sessionId"), meta.get("bridgeSessionId")
            if isinstance(session, str) and isinstance(bridge, str) and bridge:
                mapping[session] = bridge
    return mapping


def _bridge_from_transcript(path: Path) -> str | None:
    """A `bridge-session` entry near the top of the transcript, if any."""
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for number, line in enumerate(lines):
                if number >= BRIDGE_SCAN_LINES:
                    break
                if '"bridge-session"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("bridgeSessionId"):
                    return str(entry["bridgeSessionId"])
    except OSError:
        pass
    return None


def _suspects(per_agent_files: dict[str, list[Path]],
              claude_roots: list[Path]) -> list[dict]:
    bridges = _bridge_from_sessions(claude_roots)
    found = []
    for agent, files in per_agent_files.items():
        for path in files:
            suspect = _inspect(path, _is_session_file(agent, path))
            if suspect is None:
                continue
            suspect["agent"] = agent
            bridge = None
            if agent == "claude-code":
                if suspect["bytes"]:
                    bridge = _bridge_from_transcript(path)
                bridge = bridge or bridges.get(path.stem)
            suspect["bridge_session_id"] = bridge
            found.append(suspect)
    order = {"empty": 0, "truncated": 1, "short": 2}
    found.sort(key=lambda s: (order[s["problem"]], -s["mtime"]))
    return found


# -------------------------------------------------------------------- report

def collect(lag_hours: float = LAG_WARN_HOURS) -> dict:
    """Build the whole report as plain data (also the --json payload)."""
    index_path = config.db_path()
    origins = _indexed_origins(index_path)
    have_index = origins is not None
    origins = origins or {}

    surfaces: list[dict] = []
    not_detected: list[str] = []
    transcript_files: dict[str, list[Path]] = {}
    claude_roots: list[Path] = []
    for adapter in adapters.all_adapters():
        if not adapter.available():
            not_detected.append(adapter.name)
            continue
        files = _source_files(adapter)
        if files is None:
            surfaces += _db_surfaces(adapter, origins)
        else:
            surfaces += _file_surfaces(adapter, files, origins)
            if adapter.name in _TRANSCRIPT_AGENTS:
                transcript_files[adapter.name] = files
        if adapter.name == "claude-code":
            claude_roots = adapter.roots()

    for surface in surfaces:
        _judge(surface, lag_hours, have_index)

    stamp = config.stamp_path()
    return {
        "index": str(index_path),
        "index_present": have_index,
        "last_index": _mtime(stamp) if stamp.exists() else None,
        "lag_threshold_hours": lag_hours,
        "surfaces": surfaces,
        "not_detected": not_detected,
        "suspects": _suspects(transcript_files, claude_roots),
        "unindexed_surfaces": [
            {"surface": name, "why": why} for name, why in UNINDEXED_SURFACES],
    }


def _has_problems(report: dict) -> bool:
    return (any(s["warning"] for s in report["surfaces"])
            or any(s["problem"] != "short" for s in report["suspects"]))


def run(*, as_json: bool = False, lag_hours: float = LAG_WARN_HOURS) -> int:
    """Print the report. Exit 1 when anything needs attention."""
    try:
        report = collect(lag_hours)
    except sqlite3.Error as exc:
        print(f"error: cannot read the index read-only: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)
    return 1 if _has_problems(report) else 0


# ------------------------------------------------------------------- display

SHOW_SHORT = 10  # short stubs are common; list only the newest few


def _home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if _under(path, Path(home)) else path


def _when(epoch: float | None) -> str:
    if epoch is None:
        return "-"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _duration(hours: float) -> str:
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _size(count: int) -> str:
    return f"{count} B" if count < 1024 else f"{count / 1024:.1f} KiB"


def _print(report: dict) -> None:
    state = "" if report["index_present"] else "  (MISSING)"
    print(f"index:      {_home(report['index'])}{state}  (opened read-only)")
    print(f"last index: {_when(report['last_index'])}")
    print()

    for s in report["surfaces"]:
        unit = "sessions" if s["kind"] == "sessions" else "files"
        on_disk = "?" if s["on_disk"] is None else s["on_disk"]
        counts = f"{on_disk} {unit} on disk, {s['indexed']} indexed"
        if s["gone_from_disk"]:
            counts += f", {s['gone_from_disk']} gone from disk (index-only)"
        status = f"WARNING {s['warning']}" if s["warning"] else "ok"
        print(f"{s['agent']:<12}{_home(s['root'])}")
        print(f"    {counts}")
        print(f"    newest source {_when(s['newest_source'])}"
              f" · newest indexed {_when(s['newest_indexed'])} · {status}")
    if report["not_detected"]:
        print(f"not detected: {', '.join(report['not_detected'])}")
    print()

    suspects = report["suspects"]
    tally = {kind: sum(1 for s in suspects if s["problem"] == kind)
             for kind in ("empty", "truncated", "short")}
    print(f"suspect transcripts: {tally['empty']} empty, "
          f"{tally['truncated']} truncated (last line not JSON), "
          f"{tally['short']} short (< {_size(SHORT_BYTES)})")
    shown_short = 0
    for s in suspects:
        if s["problem"] == "short":
            shown_short += 1
            if shown_short > SHOW_SHORT:
                continue
        line = (f"  {s['problem'].upper():<10}{_home(s['path'])}  "
                f"{_size(s['bytes'])}  {_when(s['mtime'])}")
        if s["bridge_session_id"]:
            line += f"  bridge {s['bridge_session_id']}"
        print(line)
    if tally["short"] > SHOW_SHORT:
        print(f"  ... {tally['short'] - SHOW_SHORT} more short (--json lists all)")
    print()

    print("not indexed by any adapter:")
    for item in report["unindexed_surfaces"]:
        print(f"  {item['surface']} — {item['why']}")
