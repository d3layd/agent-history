"""Secret redaction, applied before anything reaches the index or the embedder.

The index outlives its sources and is searched by agents, so a credential
pasted into a session once becomes recallable forever. Redaction replaces the
secret part of a match with a typed marker such as ``[REDACTED:github_token]``
so a hit still shows *that* something was there, and what kind.

Matching works on spans of the full record text, and the indexer applies them
per chunk (see ``textproc.chunk_spans``). Chunk boundaries therefore stay
exactly where they were before redaction existed, which keeps content hashes
stable: re-indexing a transcript after ``scrub`` has rewritten its old chunks
converges instead of duplicating them.

Patterns are deliberately conservative. A missed secret is the cost of keeping
git SHAs, UUIDs, hashes and ordinary prose about passwords intact.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

MARKER = "[REDACTED:{}]"

# (class, pattern, group holding the secret). Order is priority: when spans
# overlap, the earlier class wins.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
        r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("openai_key", re.compile(
        r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"), 0),
    ("github_token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 0),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), 0),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("stripe_key", re.compile(
        r"\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{20,})"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), 0),
    # 1Password service-account tokens: `ops_` + a long base64url body. The
    # length floor keeps names like the Linux user `ops_a0dc6f0c` intact.
    ("onepassword_token", re.compile(r"\bops_[A-Za-z0-9+/=_-]{40,}"), 0),
    # Scribe agent keys.
    ("scribe_key", re.compile(r"\bscrb_agt_[A-Za-z0-9_-]{16,}"), 0),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    ("bearer", re.compile(
        r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})"), 1),
    # scheme://user:password@host — database URIs, git remotes with tokens.
    # The password runs to the last @ before the host, so one containing @
    # is redacted whole; it never crosses into the path, query or fragment.
    ("url_password", re.compile(
        r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@'\"<>]+:([^\s'\"<>/?#]+)@"), 1),
    # name = value, name: value, "name": "value", --name=value
    ("assignment", re.compile(
        r"(?i)\b[A-Za-z0-9_.-]*"
        r"(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key"
        r"|private[_-]?key|client[_-]?secret|auth[_-]?key|credentials?)"
        r"[A-Za-z0-9_.-]*[\"']?[ \t]*[:=][ \t]*[\"']?([^\s\"'`,;]{8,})"), 1),
]

# Values an `assignment` match must not redact: references, placeholders and
# code, which carry no secret and are worth keeping searchable.
_DOTTED_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
_PLACEHOLDER = re.compile(r"^(?:x+|\*+|\.+|-+|<.*>|\{\{.*\}\}|\$.*|%.*%)$", re.I)
_NOT_SECRET_WORDS = {
    "required", "optional", "undefined", "provided", "redacted", "changeme",
    "password", "string", "boolean", "default", "example", "placeholder",
    "token", "secret", "pass", "passwd", "pwd",
}


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.match(value)) or value.lower() in _NOT_SECRET_WORDS


def _plausible_assignment_value(value: str) -> bool:
    if value.startswith(("[REDACTED", "?")):  # ${VAR:?message} is not a value
        return False
    if _is_placeholder(value) or _DOTTED_IDENT.match(value) or _ENV_NAME.match(value):
        return False
    if "(" in value or ")" in value or "process.env" in value or "environ" in value:
        return False
    # Real secrets mix character classes; a single-class value of this length
    # is far more often a word, a count or an identifier.
    classes = sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    ))
    return classes >= 2 or len(value) >= 24


Span = tuple[int, int, str]


def spans(text: str) -> list[Span]:
    """Return non-overlapping (start, end, class) secret spans, sorted."""
    found: list[Span] = []
    for cls, pattern, group in _PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(group)
            if start == end or text.startswith("[REDACTED:", start):
                continue  # already redacted: keeps redaction idempotent
            if cls == "assignment" and not _plausible_assignment_value(match.group(group)):
                continue
            if cls == "url_password" and _is_placeholder(match.group(group)):
                continue
            found.append((start, end, cls))
    # Keep the highest-priority span wherever two overlap. Patterns were
    # appended in priority order, so a stable sort by start keeps that order
    # among spans starting at the same offset.
    priority = {cls: i for i, (cls, _, _) in enumerate(_PATTERNS)}
    found.sort(key=lambda s: (s[0], priority[s[2]]))
    out: list[Span] = []
    for span in found:
        if out and span[0] < out[-1][1]:
            prev = out[-1]
            if priority[span[2]] < priority[prev[2]]:
                out[-1] = (min(prev[0], span[0]), max(prev[1], span[1]), span[2])
            else:
                out[-1] = (prev[0], max(prev[1], span[1]), prev[2])
            continue
        out.append(span)
    return out


def apply(text: str, found: Iterable[Span], offset: int = 0) -> str:
    """Replace spans (in full-text coordinates) inside ``text``, which starts
    at ``offset`` of the full text. Spans cut by the window are still replaced,
    so a secret straddling a chunk boundary is never half-kept."""
    pieces: list[str] = []
    cursor = 0
    end_of_window = offset + len(text)
    for start, end, cls in found:
        if end <= offset or start >= end_of_window:
            continue
        local_start = max(start - offset, 0)
        local_end = min(end - offset, len(text))
        pieces.append(text[cursor:local_start])
        pieces.append(MARKER.format(cls))
        cursor = local_end
    pieces.append(text[cursor:])
    return "".join(pieces)


_MARKER_RE = re.compile(r"\[REDACTED:([a-z_]+)\]")


def markers(text: str) -> list[str]:
    """Classes of the redaction markers already present in ``text``."""
    return _MARKER_RE.findall(text)


def redact(text: str) -> tuple[str, Counter[str]]:
    """Redact a whole string; returns the new text and per-class counts."""
    found = spans(text)
    return apply(text, found), Counter(cls for _, _, cls in found)
