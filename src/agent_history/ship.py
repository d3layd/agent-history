"""`agent-history ship` — send this machine's history to a hub, redacted.

What leaves the machine is a normalised **Record** (agent, origin, session,
cwd, timestamp, role, text) with secrets already replaced by typed markers —
not the raw transcript, not embeddings. Redaction here ignores
`redact_enabled()`: the local index may keep credentials on purpose, but
nothing that leaves the machine ever does.

The flow is built so that a dropped connection loses nothing and sends
nothing twice:

1. New records are written as immutable JSONL batches into the outbox
   (<= BATCH_LINES lines each). Their hashes are recorded as *queued*.
2. The batches are uploaded with rsync, which deletes each source file only
   once it has been transferred.
3. A hash becomes *shipped* only when its batch file is gone from the outbox
   — i.e. after the upload succeeded. Leftover batches simply go again on
   the next run, and the queue stops them being re-collected meanwhile.

The hub dedups by content, so even a crash between the steps costs only a
repeated upload, never a lost or doubled record.

Each record also carries its session's uid and any link aliases
(``session_uid``, ``aliases``), so the hub files it under the same
conversation this machine does instead of re-deriving. Both fields are
optional on the wire: a hub accepts records from older shippers without them,
and an older hub ignores them.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import urllib.parse
import uuid
from collections import Counter
from pathlib import Path

from . import adapters, config, redact, sessions, store, textproc

BATCH_LINES = 2000
FORMAT = 1
UPLOAD_GROUP = 200      # batch files per rsync call (keeps argv short)
BACKFILL_PAGE = 500


def record_hash(agent: str, origin: str, session_id: str, timestamp: str,
                role: str, text: str) -> str:
    """Identity of a shipped record. JSON-encoded so field boundaries cannot
    be forged by text that happens to contain a separator."""
    payload = json.dumps([agent, origin, session_id, timestamp, role, text],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- state

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shipped (hash TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS queued  (hash TEXT PRIMARY KEY, batch TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_queued_batch ON queued(batch);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class State:
    """What has been sent, in a small SQLite file in the data home (0600)."""

    def __init__(self, db: sqlite3.Connection, writable: bool) -> None:
        self.db = db
        self.writable = writable

    @classmethod
    def open(cls, writable: bool) -> "State":
        path = config.ship_state_path()
        if writable:
            config.ensure_data_home()
            db = sqlite3.connect(path)
            config.secure_file(path)
            db.executescript(_SCHEMA)
            return cls(db, True)
        # A dry run must not create anything: read what exists, else start empty.
        if path.exists():
            db = sqlite3.connect(
                f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
        else:
            db = sqlite3.connect(":memory:")
            db.executescript(_SCHEMA)
        return cls(db, False)

    def known(self, digest: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM shipped WHERE hash = ? UNION ALL "
            "SELECT 1 FROM queued WHERE hash = ?", (digest, digest)
        ).fetchone() is not None

    def queue(self, digests: list[str], batch: str) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO queued(hash, batch) VALUES (?, ?)",
            [(d, batch) for d in digests])
        self.db.commit()

    def settle(self, outbox: Path) -> int:
        """Promote queued hashes whose batch has left the outbox to shipped.

        A batch file disappears only when rsync (or a test transport) has
        delivered it, so absence is the proof of upload. Returns the number of
        batches settled.
        """
        batches = [row[0] for row in
                   self.db.execute("SELECT DISTINCT batch FROM queued")]
        settled = 0
        for batch in batches:
            if (outbox / batch).exists():
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO shipped(hash) "
                "SELECT hash FROM queued WHERE batch = ?", (batch,))
            self.db.execute("DELETE FROM queued WHERE batch = ?", (batch,))
            settled += 1
        self.db.commit()
        return settled

    def get(self, key: str, default: str = "0") -> str:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# --------------------------------------------------------------------- batches

class Batches:
    """Collects redacted records and writes them out as immutable batch files."""

    def __init__(self, state: State, outbox: Path, dry_run: bool) -> None:
        self.state = state
        self.outbox = outbox
        self.dry_run = dry_run
        self.machine = config.machine_id()
        self.pending: list[tuple[str, str]] = []   # (hash, json line)
        self.seen: set[str] = set()
        self.new = 0
        self.files = 0
        self.redacted: Counter[str] = Counter()
        self._seq = 0
        # Names never repeat: a clock tick and a pid are not enough, since
        # two runs in one second with a recycled pid would overwrite a batch
        # the hub has not read yet.
        self._stamp = "%s-%d-%s" % (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            os.getpid(), uuid.uuid4().hex[:6])

    def add(self, *, agent: str, origin: str, session_id: str, cwd: str,
            timestamp: str, role: str, text: str,
            session_uid: str | None = None,
            aliases: tuple | list = ()) -> None:
        # Unconditional: this is the one place text leaves the machine.
        text, found = redact.redact(text)
        digest = record_hash(agent, origin, session_id, timestamp, role, text)
        if digest in self.seen or self.state.known(digest):
            return
        self.seen.add(digest)
        self.new += 1
        self.redacted.update(found)
        if self.dry_run:
            return
        line = {
            "v": FORMAT, "machine": self.machine, "agent": agent,
            "origin": origin, "session_id": session_id, "cwd": cwd,
            "timestamp": timestamp, "role": role, "text": text,
            "session_uid": session_uid or sessions.derive_uid(
                agent, self.machine, session_id, origin),
        }
        if aliases:
            line["aliases"] = [list(pair) for pair in aliases]
        self.pending.append((digest, json.dumps(line, ensure_ascii=False)))
        if len(self.pending) >= BATCH_LINES:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        self._seq += 1
        name = f"{self._stamp}-{self._seq:04d}.jsonl"
        final = self.outbox / name
        part = self.outbox / f".{name}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(part, flags, 0o600), "w", encoding="utf-8") as fh:
            fh.write("\n".join(line for _, line in self.pending) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, final)
        # Queue only after the file exists: a crash in between leaves a batch
        # with unqueued hashes, which costs a duplicate upload, never a loss.
        self.state.queue([d for d, _ in self.pending], name)
        self.files += 1
        self.pending = []


def _outbox() -> Path:
    path = config.outbox_dir()
    path.mkdir(parents=True, exist_ok=True)
    if not config.WINDOWS:
        path.chmod(0o700)
    return path


# ------------------------------------------------------------------- transport

def upload(files: list[Path], dest: str, key: str | None) -> bool:
    """Send batch files with rsync; each source is removed once delivered.

    The destination is an rsync target such as ``ahub@host:``. The hub pins
    the directory with rrsync, so the remote path is relative and usually
    empty. This is the only function that touches the network; tests swap it.
    """
    if dest.startswith("-"):
        print("ship: refusing a destination that looks like an option",
              file=sys.stderr)
        return False
    ssh = "ssh -o BatchMode=yes -o ConnectTimeout=20"
    if key:
        ssh += f" -i {shlex.quote(key)} -o IdentitiesOnly=yes"
    for start in range(0, len(files), UPLOAD_GROUP):
        group = [str(f) for f in files[start:start + UPLOAD_GROUP]]
        argv = ["rsync", "-e", ssh, "--remove-source-files", "--timeout=60",
                "--", *group, dest]
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True)
        except OSError as exc:
            print(f"ship: cannot run rsync: {exc}", file=sys.stderr)
            return False
        if done.returncode != 0:
            lines = (done.stderr or "").strip().splitlines()
            print(f"ship: rsync failed ({done.returncode}): "
                  f"{lines[-1] if lines else 'no output'}", file=sys.stderr)
            return False
    return True


# ------------------------------------------------------------------------- run

def run(dry_run: bool = False, backfill: bool = False) -> int:
    from . import trigger

    dest = config.hub_ingest()
    if not dest and not dry_run:
        print("ship: AGENT_HISTORY_HUB_INGEST is not set (an rsync "
              "destination such as ahub@host:)", file=sys.stderr)
        return 1
    if dry_run:
        return _run_locked(dest, True, backfill)
    config.ensure_data_home()
    with trigger._exclusive(config.ship_lock_path()) as acquired:
        if not acquired:
            print("ship: another run is in progress; skipping")
            return 0
        config.secure_file(config.ship_lock_path())
        return _run_locked(dest, False, backfill)


def _run_locked(dest: str | None, dry_run: bool, backfill: bool) -> int:
    state = State.open(writable=not dry_run)
    try:
        outbox = config.outbox_dir()
        if not dry_run:
            outbox = _outbox()
            for stale in outbox.glob(".*.part"):  # an interrupted write
                stale.unlink(missing_ok=True)
            # A previous run may have delivered files and died before it
            # could record that.
            state.settle(outbox)

        batches = Batches(state, outbox, dry_run)
        _collect(batches)
        watermark = None
        if backfill:
            watermark = _collect_index(batches, state)
        batches.flush()
        if watermark is not None and not dry_run:
            state.set("backfill_id", str(watermark))

        leftovers = (sorted(outbox.glob("*.jsonl")) if outbox.is_dir() else [])
        verb = "would ship" if dry_run else "queued"
        print(f"ship: {batches.new} new record(s) {verb}"
              + ("" if dry_run else f" in {batches.files} batch(es)"))
        if batches.redacted:
            print("  redacted: " + ", ".join(
                f"{n} {cls}" for cls, n in batches.redacted.most_common()))
        if dry_run:
            if leftovers:
                print(f"  {len(leftovers)} batch(es) already waiting in the outbox")
            return 0
        if not leftovers:
            return 0

        try:
            ok = upload(leftovers, dest, config.hub_key())
        except Exception as exc:  # noqa: BLE001 - a transport must not lose state
            print(f"ship: upload failed: {exc}", file=sys.stderr)
            ok = False
        if ok:
            for batch in leftovers:  # delivered, whether or not the transport removed them
                batch.unlink(missing_ok=True)
        done = state.settle(outbox)
        remaining = len(list(outbox.glob("*.jsonl")))
        print(f"ship: uploaded {done} batch(es)"
              + (f"; {remaining} left in the outbox for the next run"
                 if remaining else ""))
        return 0 if ok and not remaining else 1
    finally:
        state.close()


def _collect(batches: Batches) -> None:
    """Records from every local adapter. The inbox is excluded: a hub's own
    queue is other machines' history, not this one's to re-ship."""
    for adapter in adapters.available_adapters():
        if adapter.name == "inbox":
            continue
        for record in adapter.records():
            if textproc.is_injection(record.text):
                continue
            batches.add(agent=record.agent, origin=record.origin,
                        session_id=record.session_id, cwd=record.cwd,
                        timestamp=record.timestamp, role=record.role,
                        text=record.text, aliases=record.aliases)


def _collect_index(batches: Batches, state: State) -> int | None:
    """Export chunks already in the local index, past the watermark.

    This covers history whose transcripts the agents have since deleted.
    Chunks are redacted individually here (a secret cut by a chunk boundary
    can leave a fragment — the same limit `scrub` has), and only this
    machine's own chunks are exported, never ones merged in from elsewhere.
    Returns the highest chunk id seen; the caller persists it once the
    batches are safely queued.
    """
    if not config.db_path().exists():
        print("ship: no local index to back-fill from")
        return None
    local = config.machine_id()
    last = int(state.get("backfill_id"))
    db = store.open_for_maintenance()
    links: dict[str, list] = {}
    try:
        while True:
            rows = db.execute(
                "SELECT id, agent, origin, session_id, cwd, timestamp, role, "
                "text, machine, session_uid FROM chunks WHERE id > ? "
                "ORDER BY id LIMIT ?",
                (last, BACKFILL_PAGE)).fetchall()
            if not rows:
                return last
            for (rowid, agent, origin, session_id, cwd, timestamp, role,
                 text, machine, uid) in rows:
                last = rowid
                if machine not in (None, local) or not text:
                    continue
                if uid and uid not in links:
                    links[uid] = sessions.links_of(db, uid)
                batches.add(agent=agent, origin=origin or "",
                            session_id=session_id or "", cwd=cwd or "",
                            timestamp=timestamp or "", role=role or "",
                            text=text, session_uid=uid,
                            aliases=links.get(uid, ()))
    finally:
        db.close()
