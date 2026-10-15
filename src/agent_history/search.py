"""Search over the index.

Three modes:

- hybrid (default): vector similarity and BM25 full-text, merged with
  reciprocal rank fusion. Embeddings find "what did we decide about auth";
  full-text finds exact identifiers ("os error 11001", a function name) that
  embed poorly. Fusion means each covers the other's blind spot.
- vector: embeddings only — the pre-0.6 behaviour.
- text: full-text only. Needs no Ollama, so it also serves as the fallback
  when the embedding endpoint is down.

With `AGENT_HISTORY_REMOTE` set, results can also come from a hub: merged with
the local ones when both exist, or hub-only when there is no local index.
"""
from __future__ import annotations

import datetime
import json
import sys

from . import config, embed, remote, store

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
    machine: str | None = None,
    exclude_machine: list[str] | None = None,
) -> list[dict]:
    """Run one search and return result dicts — shared by the CLI and viewer."""
    db = store.open_for_read(check_model=(mode != "text"))
    narrowing = {"cwd": cwd, "agent": agent, "machine": machine,
                 "exclude_machine": exclude_machine}

    if mode == "text":
        rows = [(*r, None, None) for r in store.text_query(db, query, k, **narrowing)]
    elif mode == "vector":
        vector = embed.pack(embed.embed(query, timeout=60))
        rows = [(*r, None) for r in store.query(db, vector, k, **narrowing)]
    else:
        if not store.fts_enabled(db):
            # An index last written by an older version has no FTS table yet;
            # it appears on the next `agent-history index`.
            vector = embed.pack(embed.embed(query, timeout=60))
            rows = [(*r, None) for r in store.query(db, vector, k, **narrowing)]
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

    Timestamps are normalised before comparison: adapters store a mix of
    UTC-suffixed and local-naive ISO strings, and comparing those
    lexicographically can misorder by the UTC offset. Naive values are
    taken as local time (that is how they were generated); unparseable or
    empty ones sort last.
    """
    def when(r: dict) -> float:
        return _instant(r.get("timestamp") or "")

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


def _instant(timestamp: str) -> float:
    """A comparable epoch for an ISO timestamp of any stored flavour."""
    if not timestamp:
        return float("-inf")
    try:
        parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive values were written in local time
    return parsed.timestamp()


def run(
    query: str,
    *,
    k: int = 8,
    cwd: str | None = None,
    agent: str | None = None,
    machine: str | None = None,
    exclude_machine: list[str] | None = None,
    mode: str = "hybrid",
    sort: str = "relevance",
    full: bool = False,
    as_json: bool = False,
    where: str | None = None,
    tenant: str | None = None,
) -> int:
    """``where`` is None (decide), "local", or "hub"."""
    side = _side(where, machine)
    if side is None:
        return 1
    if tenant and side == "local":
        print("error: --tenant selects an index on a hub; set "
              "AGENT_HISTORY_REMOTE", file=sys.stderr)
        return 1
    options = dict(k=k, cwd=cwd, agent=agent, machine=machine,
                   exclude_machine=exclude_machine, mode=mode, sort=sort)

    if side == "hub":
        return remote.passthrough(remote.search_args(
            query, full=full, as_json=as_json, tenant=tenant, **options))

    results = search_rows(query, **options)
    if side == "both":
        results = _merge_hub(query, results, full=full, tenant=tenant, **options)
    return emit(results, full=full, as_json=as_json)


def emit(results: list[dict], *, full: bool, as_json: bool) -> int:
    """Print results as JSON or the human format. A tenant is labelled only
    when the results span more than one — one tenant is just "the index"."""
    if as_json:
        print(json.dumps([_trim(dict(r), full) for r in results], indent=2))
        return 0

    if not results:
        print("(no matches)")
        return 0

    label = len({r["tenant"] for r in results if r.get("tenant")}) > 1
    for position, result in enumerate(results, 1):
        _print_row(position, result, full, label_tenant=label)
    return 0


def _side(where: str | None, machine: str | None) -> str | None:
    """Which indexes to ask: "local", "hub", or "both". None means an error
    was already printed."""
    configured = config.remote_url()
    if where == "hub" and not configured:
        print("error: --hub needs AGENT_HISTORY_REMOTE=ssh://user@host",
              file=sys.stderr)
        return None
    if where == "local" or not configured:
        return "local"
    if where == "hub" or not config.db_path().exists():
        return "hub"
    # The hub also holds this machine's own history; asking it for that would
    # only return the local results a second time.
    return "local" if machine == config.machine_id() else "both"


def _merge_hub(query: str, local: list[dict], *, full: bool, k: int,
               exclude_machine: list[str] | None, sort: str,
               tenant: str | None = None, **options) -> list[dict]:
    """Interleave hub results into ``local`` by reciprocal-rank fusion.

    The hub is asked to leave out this machine's own chunks, so the two lists
    are disjoint and fusion reduces to interleaving by rank, with the usual
    recency tiebreak between equal ranks. If the hub cannot be reached the
    local results stand, with one line on stderr: search must keep working
    on a plane.
    """
    skip = [*(exclude_machine or ()), config.machine_id()]
    try:
        hub = remote.query_json(remote.search_args(
            query, k=k, exclude_machine=skip, sort=sort, full=full,
            as_json=True, tenant=tenant, **options))
    except remote.RemoteError as exc:
        print(f"warning: hub unavailable ({exc}); showing local results only",
              file=sys.stderr)
        return local

    return fuse([local, [{**_HUB_DEFAULTS, **r} for r in hub]], sort=sort, k=k)


def fuse(lists: list[list[dict]], *, sort: str, k: int) -> list[dict]:
    """Interleave independently ranked result lists by reciprocal-rank
    fusion: each hit scores 1/(60+rank) in its own list, equal ranks fall to
    the newer chunk, and repeats are dropped."""
    seen: set[tuple] = set()
    fused: list[dict] = []
    for results in lists:
        for rank, result in enumerate(results):
            result = {**result, "score": 1.0 / (60 + rank)}
            key = (result["file"], result["session_id"], result["timestamp"],
                   result["text"][:200], result.get("tenant"))
            if key not in seen:
                seen.add(key)
                fused.append(result)
    return _order(fused, sort)[:k]


_HUB_DEFAULTS = {"agent": "?", "file": "", "session_id": "",
                 "session_uid": None, "cwd": "",
                 "timestamp": "", "role": "?", "machine": "hub",
                 "distance": None}


def _as_dict(row: tuple) -> dict:
    (agent, origin, session_id, cwd, timestamp, role, text, machine,
     session_uid, distance, score) = row
    return {
        "agent": agent,
        "file": origin,
        "session_id": session_id,
        "session_uid": session_uid,
        "cwd": cwd,
        "timestamp": timestamp,
        "role": role,
        "text": text,
        "machine": machine or config.machine_id(),
        "distance": distance,
        "score": score,
    }


def _trim(result: dict, full: bool) -> dict:
    if not full:
        result["text"] = result["text"][:JSON_PREVIEW_CHARS]
    return result


def _print_row(position: int, result: dict, full: bool,
               label_tenant: bool = False) -> None:
    if result["score"] is not None:
        ranking = f"score={result['score']:.4f}"
    elif result["distance"] is not None:
        ranking = f"dist={result['distance']:.2f}"
    else:
        ranking = "text-match"
    timestamp = (result["timestamp"] or "")[:16]
    where = ""
    if result.get("machine") and result["machine"] != config.machine_id():
        where = f"  @{result['machine']}"
    if label_tenant and result.get("tenant"):
        where += f"  [{result['tenant']}]"
    print(
        f"#{position}  {ranking}  {timestamp}  "
        f"{result['agent']}/{result['role']}  {result['cwd'] or '?'}{where}"
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
