"""Session identity: one immutable uid per conversation, source ids as aliases.

A source's own session id is not an identity. One conversation can carry
several (a local Claude Code uuid, a remote-control ``session_…`` / ``cse_…``
pair, a teleported copy, an export uuid), while synthetic ids collide
(every project's ``memory:MEMORY``, and the same id from two machines on a
hub). So the tool owns the identity:

- **uid**: a UUIDv5 under a fixed namespace over
  ``agent \\x1f machine \\x1f source key``. Pure function of the record, so a
  rebuild, another machine and the hub all derive the same value, and it is
  never rewritten once a chunk carries it. For pseudo-sessions (memory and
  doc files, or a source with no id at all) the source key includes the
  origin path, which is what keeps ``memory:MEMORY`` apart across projects.
- **aliases**: ``(kind, value) -> uid``. ``source`` is the raw id; the other
  kinds are links found on hard evidence. When evidence shows two uids are
  one conversation, the earliest stays canonical and the other becomes a
  ``uid`` alias of it. Chunks keep the uid they were written with;
  canonicalisation is an overlay resolved at read time.

``resolve_session()`` accepts any of these, so a raw id pasted from
``claude --resume`` keeps working everywhere a user types a session.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import uuid

from . import config

# Fixed forever: changing it would change every uid ever derived.
# (uuid5(NAMESPACE_URL, "https://github.com/d3layd/agent-history#session"))
NAMESPACE = uuid.UUID("e4c875af-6959-5662-87d4-c11bd9a41c4f")

# Ids that name a file rather than a conversation; their key needs the origin.
PSEUDO_PREFIXES = ("memory:", "doc:")

# `source` is the raw id; `uid` points a merged-away uid at its canonical one.
# The rest are other names of the same conversation. Only `bridge` and `cse`
# are produced today (Claude Code remote control); the others are reserved
# for the export, Cowork and teleport adapters. A fork is a different
# conversation, so it is not an alias: see link_fork().
ALIAS_KINDS = ("source", "uid", "bridge", "cse", "export", "cowork",
               "teleport", "claude")
# Kinds that are evidence of *sameness*. A raw `source` id can collide (the
# same id on two machines is not proof), so it never merges two uids.
# `claude` is a pointer from a real Claude Code session uuid to a session the
# hub files under another key (a claude -p job, keyed by its job id): a
# lookup aid, first come first served, never a reason to merge.
IDENTITY_KINDS = frozenset(ALIAS_KINDS) - {"source", "claude"}
BRIDGE_PREFIXES = ("cse_", "session_")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    uid               TEXT PRIMARY KEY,
    agent             TEXT,
    machine           TEXT,
    source_session_id TEXT,
    origin            TEXT,
    first_seen        TEXT,
    last_seen         TEXT,
    meta              TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS session_aliases (
    kind        TEXT NOT NULL,
    value       TEXT NOT NULL,
    session_uid TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE INDEX IF NOT EXISTS idx_aliases_value ON session_aliases(value);
CREATE INDEX IF NOT EXISTS idx_aliases_uid   ON session_aliases(session_uid);
"""


# ------------------------------------------------------------------ derivation

def is_pseudo(session_id: str | None) -> bool:
    """Whether ``session_id`` names a file rather than a conversation."""
    return not session_id or session_id.startswith(PSEUDO_PREFIXES)


def source_key(session_id: str | None, origin: str | None) -> str:
    """The part of the uid input that identifies the session at its source."""
    if is_pseudo(session_id):
        return f"{session_id or ''}@{origin or ''}"
    return session_id or ""


def derive_uid(agent: str, machine: str | None, session_id: str | None,
               origin: str | None = None) -> str:
    """The deterministic uid of a session. ``machine`` None means this one."""
    name = "\x1f".join((agent or "", machine or config.machine_id(),
                        source_key(session_id, origin)))
    return str(uuid.uuid5(NAMESPACE, name))


def valid_uid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def uid_for(db: sqlite3.Connection, agent: str, machine: str | None,
            session_id: str | None, origin: str | None,
            claimed: str | None = None) -> str:
    """The uid a chunk of this session is written with.

    Always the derivation from the machine the record came from — on a hub,
    the inbox directory its authenticated key writes into. ``claimed`` (a
    uid a shipper sent along) is accepted only when it equals that
    derivation; anything else is ignored, so no shipper can file history
    under a conversation it does not own, even one not indexed yet. ``db`` is
    unused today and kept so the policy can consult the index.
    """
    return derive_uid(agent, machine, session_id, origin)


# -------------------------------------------------------------------- registry

def register_session(db: sqlite3.Connection, uid: str, *, agent: str,
                     machine: str | None, session_id: str | None,
                     origin: str | None, timestamp: str | None = None,
                     cwd: str | None = None) -> None:
    """Create or touch the session row and its raw-id alias.

    ``first_seen``/``last_seen`` are message times, not wall-clock times of
    indexing, so every index holding the session agrees on them. The raw id
    is recorded as a ``source`` alias first-come-first-served: a pseudo id
    is never one (it names a file, and collides across projects).
    """
    stamp = timestamp or None
    meta = json.dumps({"cwd": cwd}) if cwd else "{}"
    db.execute(
        "INSERT INTO sessions (uid, agent, machine, source_session_id, origin, "
        "first_seen, last_seen, meta) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET "
        "  origin = coalesce(excluded.origin, origin), "
        "  first_seen = CASE WHEN excluded.first_seen IS NULL THEN first_seen "
        "    WHEN first_seen IS NULL OR excluded.first_seen < first_seen "
        "    THEN excluded.first_seen ELSE first_seen END, "
        "  last_seen = CASE WHEN excluded.last_seen IS NULL THEN last_seen "
        "    WHEN last_seen IS NULL OR excluded.last_seen > last_seen "
        "    THEN excluded.last_seen ELSE last_seen END",
        (uid, agent, machine, session_id or "", origin or None,
         stamp, stamp, meta),
    )
    if not is_pseudo(session_id):
        db.execute(
            "INSERT OR IGNORE INTO session_aliases (kind, value, session_uid) "
            "VALUES ('source', ?, ?)", (session_id, uid))


def update_meta(db: sqlite3.Connection, uid: str, **fields) -> None:
    """Merge ``fields`` into a session's mutable metadata (title, links...)."""
    row = db.execute("SELECT meta FROM sessions WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        return
    try:
        meta = json.loads(row[0] or "{}")
    except ValueError:
        meta = {}
    meta.update({k: v for k, v in fields.items() if v is not None})
    db.execute("UPDATE sessions SET meta = ? WHERE uid = ?",
               (json.dumps(meta, ensure_ascii=False), uid))


def link_fork(db: sqlite3.Connection, child: str, parent: str) -> None:
    """Record that session ``child`` was forked from ``parent``.

    A fork is a relative, not the same conversation, so it lives in both
    sessions' metadata (``fork_of`` on the child, ``forks`` on the parent)
    rather than in the alias table. Hook for adapters that see forks; none
    does yet.
    """
    update_meta(db, child, fork_of=parent)
    row = db.execute("SELECT meta FROM sessions WHERE uid = ?",
                     (parent,)).fetchone()
    if row is None:
        return
    try:
        forks = json.loads(row[0] or "{}").get("forks") or []
    except ValueError:
        forks = []
    if child not in forks:
        update_meta(db, parent, forks=[*forks, child])


# --------------------------------------------------------------------- linking

def canonical(db: sqlite3.Connection, uid: str) -> str:
    """The uid ``uid`` was merged into, or itself."""
    seen = {uid}
    while True:
        row = db.execute(
            "SELECT session_uid FROM session_aliases "
            "WHERE kind = 'uid' AND value = ?", (uid,)).fetchone()
        if row is None or row[0] in seen:  # a cycle cannot happen; never loop
            return uid
        uid = row[0]
        seen.add(uid)


def link_alias(db: sqlite3.Connection, uid: str, kind: str, value: str) -> str:
    """Record that ``value`` (of ``kind``) names the session ``uid``.

    Call only on hard evidence. If the alias already belongs to another
    session and ``kind`` proves sameness, the two are merged: the earliest
    stays canonical, the other becomes an alias of it. Returns the canonical
    uid afterwards.
    """
    if kind not in ALIAS_KINDS:
        raise ValueError(f"unknown alias kind {kind!r}")
    if not value:
        return canonical(db, uid)
    target = canonical(db, uid)
    row = db.execute(
        "SELECT session_uid FROM session_aliases WHERE kind = ? AND value = ?",
        (kind, value)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO session_aliases (kind, value, session_uid) "
            "VALUES (?, ?, ?)", (kind, value, target))
        return target
    if kind in IDENTITY_KINDS:
        return merge(db, target, row[0])
    return target


def link_bridge(db: sqlite3.Connection, uid: str, bridge_id: str) -> str:
    """Link a Claude Code session to its remote-control pair.

    ``bridge_id`` is either half of the ``cse_<x>`` / ``session_<x>`` pair;
    both share the suffix, so both are recorded. A session indexed under
    either id as its raw source id (a cloud mirror of the same conversation)
    is merged in too. Returns the canonical uid.
    """
    suffix = bridge_suffix(bridge_id)
    if not suffix:
        return canonical(db, uid)
    target = link_alias(db, uid, "cse", f"cse_{suffix}")
    target = link_alias(db, target, "bridge", f"session_{suffix}")
    for (other,) in db.execute(
            "SELECT session_uid FROM session_aliases WHERE kind = 'source' "
            "AND value IN (?, ?)", (f"cse_{suffix}", f"session_{suffix}")
    ).fetchall():
        target = merge(db, target, other)
    return target


def bridge_suffix(bridge_id: str | None) -> str | None:
    for prefix in BRIDGE_PREFIXES:
        if bridge_id and bridge_id.startswith(prefix):
            suffix = bridge_id[len(prefix):]
            return suffix if suffix.isalnum() else None
    return None


def merge(db: sqlite3.Connection, a: str, b: str) -> str:
    """Make two sessions one; return the surviving (canonical) uid.

    No uid is rewritten — chunks and session rows keep theirs. The loser is
    recorded as a ``uid`` alias of the winner and its aliases are repointed,
    so every chain stays one hop long.
    """
    a, b = canonical(db, a), canonical(db, b)
    if a == b:
        return a
    winner, loser = sorted((a, b), key=lambda uid: _seniority(db, uid))
    db.execute("UPDATE session_aliases SET session_uid = ? WHERE session_uid = ?",
               (winner, loser))
    db.execute(
        "INSERT OR REPLACE INTO session_aliases (kind, value, session_uid) "
        "VALUES ('uid', ?, ?)", (loser, winner))
    return winner


def _seniority(db: sqlite3.Connection, uid: str) -> tuple:
    """Sort key for "earliest": first message time, then the uid itself so
    every index breaks a tie the same way."""
    row = db.execute("SELECT first_seen FROM sessions WHERE uid = ?",
                     (uid,)).fetchone()
    return (_instant(row[0] if row else None), uid)


def _instant(timestamp: str | None) -> float:
    """Epoch of an ISO timestamp; unknown sorts last. Naive values are read
    as UTC so that every machine orders them the same."""
    if not timestamp:
        return float("inf")
    try:
        parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


# ------------------------------------------------------------------- resolving

def resolve_session(db: sqlite3.Connection, any_id: str) -> str | None:
    """The canonical uid for a uid, a raw source id or any alias; else None.

    A pseudo id such as ``memory:MEMORY`` resolves only when exactly one
    session has it — with several it is ambiguous, and callers fall back to
    matching the raw id as before.
    """
    any_id = (any_id or "").strip()
    if not any_id:
        return None
    if db.execute("SELECT 1 FROM sessions WHERE uid = ?", (any_id,)).fetchone():
        return canonical(db, any_id)
    row = db.execute(
        "SELECT session_uid FROM session_aliases WHERE value = ? "
        "ORDER BY kind = 'source', rowid LIMIT 1", (any_id,)).fetchone()
    if row:
        return canonical(db, row[0])
    rows = db.execute(
        "SELECT DISTINCT uid FROM sessions WHERE source_session_id = ? LIMIT 2",
        (any_id,)).fetchall()
    if len(rows) == 1:
        return canonical(db, rows[0][0])
    # A uid that only exists on chunks (an index written by a peer whose
    # session rows never arrived) is still a uid.
    if valid_uid(any_id) and db.execute(
            "SELECT 1 FROM chunks WHERE session_uid = ? LIMIT 1",
            (any_id,)).fetchone():
        return canonical(db, any_id)
    return None


def members(db: sqlite3.Connection, uid: str) -> list[str]:
    """Every uid that resolves to canonical ``uid`` (itself included)."""
    return [uid] + [row[0] for row in db.execute(
        "SELECT value FROM session_aliases WHERE kind = 'uid' "
        "AND session_uid = ?", (uid,))]


def ids_of(db: sqlite3.Connection, uid: str) -> list[tuple[str, str]]:
    """Every (kind, id) the session ``uid`` belongs to is known by: its
    member uids, their raw source ids and all aliases. What a purge of the
    session reaches, listed before anything is deleted."""
    group = members(db, canonical(db, uid))
    marks = ",".join("?" * len(group))
    out = [("uid", member) for member in group]
    out += [("source", row[0]) for row in db.execute(
        f"SELECT DISTINCT source_session_id FROM sessions WHERE uid IN ({marks}) "
        "AND source_session_id != '' ORDER BY 1", group)]
    out += [tuple(row) for row in db.execute(
        "SELECT kind, value FROM session_aliases WHERE session_uid = ? "
        "AND kind NOT IN ('source', 'uid') ORDER BY kind, value",
        (canonical(db, uid),))]
    return out


def links_of(db: sqlite3.Connection, uid: str) -> list[tuple[str, str]]:
    """The evidence aliases (everything but ``source`` and ``uid``) of the
    session ``uid`` belongs to — what a shipper sends along."""
    return [tuple(row) for row in db.execute(
        "SELECT kind, value FROM session_aliases WHERE session_uid = ? "
        "AND kind NOT IN ('source', 'uid') ORDER BY kind, value",
        (canonical(db, uid),))]


# ------------------------------------------------------------------- migration

def backfill(db: sqlite3.Connection) -> int:
    """Give every chunk without a uid one, and every uid a session row.

    One UPDATE over the rows still NULL (so a crash or an older writer just
    leaves work for the next open), with derivation done by a Python SQL
    function. Returns the number of chunks filled.
    """
    if db.execute("SELECT 1 FROM chunks WHERE session_uid IS NULL "
                  "LIMIT 1").fetchone() is None:
        return 0
    local = config.machine_id()
    db.create_function(
        "ah_session_uid", 4,
        lambda agent, machine, sid, origin: derive_uid(agent, machine or local,
                                                       sid, origin),
        deterministic=True)
    filled = db.execute(
        "UPDATE chunks SET session_uid = "
        "ah_session_uid(agent, machine, session_id, origin) "
        "WHERE session_uid IS NULL").rowcount
    db.execute(
        "INSERT OR IGNORE INTO sessions (uid, agent, machine, "
        "source_session_id, origin, first_seen, last_seen) "
        "SELECT session_uid, agent, machine, coalesce(session_id, ''), "
        "       max(origin), min(nullif(timestamp, '')), "
        "       max(nullif(timestamp, '')) "
        "FROM chunks WHERE session_uid NOT IN (SELECT uid FROM sessions) "
        "GROUP BY session_uid")
    db.execute(
        "INSERT OR IGNORE INTO session_aliases (kind, value, session_uid) "
        "SELECT 'source', source_session_id, uid FROM sessions "
        "WHERE source_session_id != '' AND source_session_id NOT LIKE 'memory:%' "
        "AND source_session_id NOT LIKE 'doc:%' "
        "ORDER BY first_seen IS NULL, first_seen, uid")
    return filled
