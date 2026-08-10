"""Opt-in context injection — `agent-history inject`.

A UserPromptSubmit hook retrieves past-session chunks relevant to the prompt
the user just typed and hands them to the agent as additional context. This
is the "stop re-explaining the project" feature, done without any LLM: the
prompt itself is the retrieval query, Ollama embeds it locally, and the
chunks are injected verbatim.

Unsolicited context has to hold a much higher bar than search results the
user asked for, so everything here leans toward precision:

- Off by default. `agent-history inject on` opts in.
- Vector search only, gated by an absolute distance cutoff. Hybrid RRF
  scores are relative to the query and cannot be thresholded; L2 distance
  can. Measured on a mature nomic-embed-text index, on-topic top hits sat
  at 14-16 and nonsense prompts at ~20, so the default cutoff of 17 admits
  the former and rejects the latter.
- When embedding is unavailable, nothing is injected. BM25 has no absolute
  score either, and injecting keyword noise would be worse than silence.
- Hard caps on chunk count and total size, and the current session's own
  chunks are excluded — the agent already has that context.

The hook must never break the prompt it rides on: every failure path exits 0
with no output.
"""
from __future__ import annotations

import json
import sys

from . import config, embed, store

MIN_PROMPT_CHARS = 12
FETCH = 8
MAX_CHUNKS = 3
CHUNK_CHARS = 700
MAX_TOTAL_CHARS = 2200
EMBED_TIMEOUT = 3

# Prompts that are conversation steering, not questions worth retrieving for.
TRIVIAL = frozenset({
    "y", "yes", "n", "no", "ok", "okay", "k", "sure", "go", "go ahead",
    "continue", "proceed", "stop", "wait", "thanks", "thank you", "nice",
    "good", "great", "do it", "yes please",
})

HEADER = (
    "Possibly relevant excerpts from past agent sessions, retrieved "
    "semantically by agent-history (local, opt-in). They may be stale or "
    "off-target — ignore freely. To read a full conversation: "
    "`agent-history show <session_id>`.\n"
)


def run_hook(stream=None) -> int:
    """Entry point for `agent-history inject run` (reads hook JSON on stdin)."""
    try:
        return _run(stream if stream is not None else sys.stdin)
    except Exception:  # noqa: BLE001 - a hook must never break the prompt
        return 0


def _run(stream) -> int:
    if not config.inject_enabled():
        return 0
    payload = json.load(stream)
    if not isinstance(payload, dict):
        return 0
    prompt = (payload.get("prompt") or "").strip()
    if not _worth_retrieving(prompt):
        return 0

    db = store.open_for_read()
    try:
        vector = embed.pack(embed.embed(prompt, timeout=EMBED_TIMEOUT))
    except embed.EmbedError:
        return 0
    cutoff = config.inject_max_distance()
    session = payload.get("session_id") or ""

    rows = [
        r for r in store.query(db, vector, FETCH)
        if r[7] <= cutoff and r[2] != session
    ]
    picked = _dedupe(rows)[:MAX_CHUNKS]
    if not picked:
        return 0

    # systemMessage is shown to the user (not the model) — a passive notice
    # that recall fired, so ambient injection isn't invisible.
    noun = "memory" if len(picked) == 1 else "memories"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _render(picked),
        },
        "systemMessage": (f"\U0001f4da agent-history: {len(picked)} {noun} "
                          "recalled  ·  /history:inject to toggle"),
    }))
    return 0


def _worth_retrieving(prompt: str) -> bool:
    if len(prompt) < MIN_PROMPT_CHARS:
        return False
    if prompt.startswith(("/", "!")):
        return False  # slash commands and shell escapes are not questions
    return prompt.lower().rstrip(".!?") not in TRIVIAL


def _dedupe(rows: list[tuple]) -> list[tuple]:
    """Adjacent sessions often contain near-identical text; keep one."""
    seen: set[str] = set()
    out = []
    for row in rows:
        key = row[6][:120]
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _render(rows: list[tuple]) -> str:
    parts = [HEADER]
    total = len(HEADER)
    for agent, _origin, session_id, cwd, timestamp, role, text, _dist in rows:
        excerpt = text[:CHUNK_CHARS]
        block = (f"\n--- {agent}/{role}  {(timestamp or '')[:16]}  "
                 f"{cwd or '?'}  session={session_id}\n{excerpt}\n")
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


# ------------------------------------------------------------------ toggling

def set_enabled(on: bool) -> int:
    off_marker = config.inject_off_path()
    if on:
        off_marker.unlink(missing_ok=True)
        print("inject: on")
        print(f"  cutoff:  distance <= {config.inject_max_distance()} "
              "($AGENT_HISTORY_INJECT_MAX_DIST)")
        print(f"  caps:    {MAX_CHUNKS} chunks, {MAX_TOTAL_CHARS} chars")
        print("  scope:   fires on UserPromptSubmit in sessions with the "
              "history plugin installed")
    else:
        config.ensure_data_home()
        off_marker.touch()
        config.secure_file(off_marker)
        print("inject: off")
    return 0


def toggle() -> int:
    """Flip the current state — the action behind the /history:inject command."""
    return set_enabled(not config.inject_enabled())


def status() -> int:
    on = config.inject_enabled()
    print(f"inject: {'on' if on else 'off'}  (on by default)")
    if on:
        print(f"  cutoff: distance <= {config.inject_max_distance()}")
    print(f"  toggle: agent-history inject {'off' if on else 'on'}"
          "   (or /history:inject in Claude Code)")
    print("  env:    AGENT_HISTORY_INJECT=1|0 overrides the off-marker")
    return 0
