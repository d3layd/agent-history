"""Rebuild whole records from stored chunks, so redaction sees whole secrets.

A record longer than ``MAX_CHUNK_CHARS`` is stored as overlapping chunks
(``textproc.chunk_spans``: each chunk starts ``CHUNK_OVERLAP`` chars before the
previous one ended). Redacting chunk by chunk misses a secret longer than the
overlap that straddles a boundary: neither chunk holds all of it, yet the two
fragments together do. ``scrub`` and ``ship --backfill-from-index`` work on
stored chunks whose source transcripts may be gone, so they rebuild the
record first, redact it whole, and only then cut it back into chunk windows.

Two chunks join into one record only when every check passes:

* same agent, origin, session, timestamp, role and machine;
* provenance, when both chunks have it (``record_id``/``chunk_ordinal``/
  ``char_start``/``char_end``, NULL on rows indexed before session identity):
  same record id, consecutive ordinals, offsets that overlap or touch, and
  stored text whose length matches its offsets (a redacted or rewritten chunk
  no longer lines up with its offsets, so that pair is not joined this way);
* otherwise the heuristic: consecutive ids, and the next chunk's first
  ``CHUNK_OVERLAP`` chars exactly equal the previous chunk's last
  ``CHUNK_OVERLAP`` chars. An unredacted non-final chunk is always exactly
  ``MAX_CHUNK_CHARS`` long, and the final chunk of a split record is always
  longer than the overlap, so both must be.

Anything that fails stays a separate record and is redacted on its own.
``guard_edges`` is the safety net for those: it redacts a long base64-ish run
touching a window edge, which may be a secret fragment cut by that window.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Iterator, NamedTuple

from . import redact
from .textproc import CHUNK_OVERLAP, MAX_CHUNK_CHARS

COLUMNS = ("id, agent, origin, session_id, cwd, timestamp, role, text, machine, "
           "session_uid, record_id, chunk_ordinal, char_start, char_end")

FRAGMENT_MARKER = redact.MARKER.format("fragment")
_FRAGMENT_MIN = 40
_B64_CHAR = re.compile(r"[A-Za-z0-9+/_=-]")
_B64_RUN = re.compile(r"[A-Za-z0-9+/_=-]{%d,}" % _FRAGMENT_MIN)


class Chunk(NamedTuple):
    id: int
    agent: str | None
    origin: str | None
    session_id: str | None
    cwd: str | None
    timestamp: str | None
    role: str | None
    text: str | None
    machine: str | None
    session_uid: str | None
    record_id: str | None = None
    ordinal: int | None = None
    start: int | None = None
    end: int | None = None


@dataclass
class Stitched:
    """One record rebuilt from >= 1 chunks."""
    chunks: list[Chunk]
    text: str
    spans: list[tuple[int, int, int]]   # (chunk id, start, end) within ``text``
    head_anchored: bool = False         # text begins where the real record does
    tail_anchored: bool = False         # text ends where the real record does

    @property
    def first(self) -> Chunk:
        return self.chunks[0]

    @property
    def stitched(self) -> bool:
        return len(self.chunks) > 1


def _provenance(c: Chunk) -> bool:
    return None not in (c.record_id, c.ordinal, c.start, c.end)


def _aligned(c: Chunk) -> bool:
    """Stored text still has the length its offsets claim."""
    return _provenance(c) and c.text is not None and len(c.text) == c.end - c.start


def _same_meta(a: Chunk, b: Chunk) -> bool:
    return (a.agent, a.origin, a.session_id, a.timestamp, a.role, a.machine) == \
           (b.agent, b.origin, b.session_id, b.timestamp, b.role, b.machine)


def _heuristic_join(prev: Chunk, nxt: Chunk, tail: str) -> bool:
    """``tail`` is the text assembled so far (its end is ``prev``'s end)."""
    if nxt.id != prev.id + 1:
        return False
    if prev.record_id != nxt.record_id and (prev.record_id or nxt.record_id):
        return False
    n = CHUNK_OVERLAP
    if len(prev.text) <= n or len(nxt.text) <= n:
        return False
    # Unredacted non-final chunks are full length; redaction shortens windows.
    if len(prev.text) != MAX_CHUNK_CHARS and "[REDACTED:" not in prev.text:
        return False
    return nxt.text[:n] == tail[-n:]


def _provenance_join(prev: Chunk, nxt: Chunk) -> int | None:
    """Chars of ``nxt`` already present at the end of ``prev`` (provenance)."""
    if not (_aligned(prev) and _aligned(nxt)):
        return None
    if prev.record_id != nxt.record_id or nxt.ordinal != prev.ordinal + 1:
        return None
    overlap = prev.end - nxt.start
    if overlap < 0 or nxt.start <= prev.start or overlap > len(nxt.text):
        return None
    return overlap


class _Builder:
    def __init__(self, c: Chunk) -> None:
        self.chunks = [c]
        self.text = c.text
        self.spans = [(c.id, 0, len(c.text))]

    def try_add(self, nxt: Chunk) -> bool:
        prev = self.chunks[-1]
        if not _same_meta(prev, nxt) or nxt.text is None or prev.text is None:
            return False
        skip = _provenance_join(prev, nxt)
        if skip is None:
            if not _heuristic_join(prev, nxt, self.text):
                return False
            skip = CHUNK_OVERLAP
        begin = len(self.text) - skip
        self.text += nxt.text[skip:]
        self.chunks.append(nxt)
        self.spans.append((nxt.id, begin, len(self.text)))
        return True

    def done(self) -> Stitched:
        first, last = self.chunks[0], self.chunks[-1]
        return Stitched(
            self.chunks, self.text, self.spans,
            head_anchored=_aligned(first) and first.start == 0,
            # Only a record's final chunk is shorter than a full window, unless
            # redaction shortened it (then it shows a marker).
            tail_anchored=len(last.text) < MAX_CHUNK_CHARS
            and (_aligned(last) or "[REDACTED:" not in last.text))


def stitch(rows: Iterable[Chunk]) -> Iterator[Stitched]:
    """Group chunks, given in id order, into whole records.

    Chunks with no text are skipped (they break a chain, as an id gap does).
    """
    current: _Builder | None = None
    for c in rows:
        if c.text is None:
            if current:
                yield current.done()
                current = None
            continue
        if current and current.try_add(c):
            continue
        if current:
            yield current.done()
        current = _Builder(c)
    if current:
        yield current.done()


def guard_edges(text: str, head: bool = True, tail: bool = True) -> str:
    """Redact a long base64/base64url run touching the start or end of ``text``.

    Such a run may be the cut-off part of a secret whose rest sits in the
    neighbouring chunk. Conservative: 40+ chars of ``[A-Za-z0-9+/_=-]`` that mix
    at least two of lower case, upper case and digits, so prose, kebab-case
    slugs and rules like ``=====`` are left alone, and a run in the middle of
    a chunk is never touched.
    """
    def risky(run: str) -> bool:
        return sum((any(c.islower() for c in run), any(c.isupper() for c in run),
                    any(c.isdigit() for c in run))) >= 2

    out = text
    if tail:
        i = len(out)
        while i > 0 and _B64_CHAR.match(out[i - 1]):
            i -= 1
        if len(out) - i >= _FRAGMENT_MIN and risky(out[i:]):
            out = out[:i] + FRAGMENT_MARKER
    if head and out and not out.startswith(FRAGMENT_MARKER):
        m = _B64_RUN.match(out)
        if m and risky(m.group()):
            out = FRAGMENT_MARKER + out[m.end():]
    return out


def redact_record(rec: Stitched) -> list[tuple[int, str, Counter[str]]]:
    """Redact the whole record, then cut the result back into its chunk windows.

    Returns ``(chunk id, redacted window text, class counts)`` per member.
    Window edges that were not cut from a neighbour get ``guard_edges``; edges
    shared with a stitched neighbour were seen whole, so need no guard.
    """
    found = redact.spans(rec.text)
    out = []
    last = len(rec.spans) - 1
    for i, (cid, start, end) in enumerate(rec.spans):
        counts = Counter(cls for s, e, cls in found if s < end and e > start)
        piece = redact.apply(rec.text[start:end], found, start)
        guarded = guard_edges(piece, head=i == 0 and not rec.head_anchored,
                              tail=i == last and not rec.tail_anchored)
        if guarded != piece:
            counts["fragment"] += 1
        out.append((cid, guarded, counts))
    return out
