"""Search over the index.

Three modes:

- hybrid (default): vector similarity and BM25 full-text, merged with
  reciprocal rank fusion. Embeddings find "what did we decide about auth";
  full-text finds exact identifiers ("os error 11001", a function name) that
  embed poorly. Fusion means each covers the other's blind spot.
- vector: embeddings only — the pre-0.6 behaviour.
- text: full-text only. Needs no Ollama, so it also serves as the fallback
  when the embedding endpoint is down.
"""
from __future__ import annotations

import json
import sys

from . import embed, store

PREVIEW_CHARS = 600
JSON_PREVIEW_CHARS = 500

MODES = ("hybrid", "vector", "text")
SORTS = ("relevance", "newest")

# Scores within a rounding bucket are treated as tied, and ties go to the
# newer chunk — so superseded information only outranks its correction when
# relevance genuinely separates them. Deliberately mild: no decay curves.
SCORE_TIE_DECIMALS = 4      # RRF scores (~0.016-0.033)
DISTANCE_TIE_DECIMALS = 1   # L2 distances (~14-20)


def search_rows(
    query: str,
    *,
    k: int = 8,
    cwd: str | None = None,
    agent: str | None = None,
    mode: str = "hybrid",
    sort: str = "relevance",
) -> list[dict]:
    """Run one search and return result dicts — shared by the CLI and viewer."""
    db = store.open_for_read(check_model=(mode != "text"))
    narrowing = {"cwd": cwd, "agent": agent}

    if mode == "text":
        rows = [(*r, None, None) for r in store.text_query(db, query, k, **narrowing)]
    elif mode == "vector":
        vector = embed.pack(embed.embed(query, timeout=60))
        rows = [(*r[:7], r[7], None) for r in store.query(db, vector, k, **narrowing)]
    else:
        if not store.fts_enabled(db):
            # An index last written by an older version has no FTS table yet;
            # it appears on the next `agent-history index`.
            vector = embed.pack(embed.embed(query, timeout=60))
            rows = [(*r[:7], r[7], None)
                    for r in store.query(db, vector, k, **narrowing)]
        else:
            try:
                vector = embed.pack(embed.embed(query, timeout=60))
            except embed.EmbedError as exc:
                print(f"note: embedding unavailable ({exc}); "
                      "full-text results only", file=sys.stderr)
                rows = [(*r, None, None)
                        for r in store.text_query(db, query, k, **narrowing)]
            else:
                rows = store.hybrid_query(db, vector, query, k, **narrowing)

    return _order([_as_dict(row) for row in rows], sort)


def _order(results: list[dict], sort: str) -> list[dict]:
    """Order results: newest-first, or relevance with a recency tiebreak.

    ISO 8601 timestamps compare lexicographically, so string comparison is
    chronological; empty timestamps sort last either way.
    """
    def when(r: dict) -> str:
        return r.get("timestamp") or ""

    if sort == "newest":
        return sorted(results, key=when, reverse=True)

    if not any(r.get("score") is not None or r.get("distance") is not None
               for r in results):
        return results  # pure text mode: BM25 order is the relevance order

    def rank(r: dict):
        if r.get("score") is not None:
            return -round(r["score"], SCORE_TIE_DECIMALS)
        return round(r["distance"], DISTANCE_TIE_DECIMALS)

    # Two-pass stable sort: newest-first first, then by bucketed relevance —
    # equal buckets keep the newer row in front.
    newest_first = sorted(results, key=when, reverse=True)
    return sorted(newest_first, key=rank)


def run(
    query: str,
    *,
    k: int = 8,
    cwd: str | None = None,
    agent: str | None = None,
    mode: str = "hybrid",
    sort: str = "relevance",
    full: bool = False,
    as_json: bool = False,
) -> int:
    results = search_rows(query, k=k, cwd=cwd, agent=agent, mode=mode, sort=sort)

    if as_json:
        print(json.dumps([_trim(dict(r), full) for r in results], indent=2))
        return 0

    if not results:
        print("(no matches)")
        return 0

    for position, result in enumerate(results, 1):
        _print_row(position, result, full)
    return 0


def _as_dict(row: tuple) -> dict:
    agent, origin, session_id, cwd, timestamp, role, text, distance, score = row
    return {
        "agent": agent,
        "file": origin,
        "session_id": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
        "role": role,
        "text": text,
        "distance": distance,
        "score": score,
    }


def _trim(result: dict, full: bool) -> dict:
    if not full:
        result["text"] = result["text"][:JSON_PREVIEW_CHARS]
    return result


def _print_row(position: int, result: dict, full: bool) -> None:
    if result["score"] is not None:
        ranking = f"score={result['score']:.4f}"
    elif result["distance"] is not None:
        ranking = f"dist={result['distance']:.2f}"
    else:
        ranking = "text-match"
    timestamp = (result["timestamp"] or "")[:16]
    print(
        f"#{position}  {ranking}  {timestamp}  "
        f"{result['agent']}/{result['role']}  {result['cwd'] or '?'}"
    )
    print(f"    file: {result['file']}")
    text = result["text"]
    if full:
        print("    ---")
        for line in text.splitlines():
            print(f"    {line}")
    else:
        print(f"    {text[:PREVIEW_CHARS].replace(chr(10), ' ')}")
    print()
