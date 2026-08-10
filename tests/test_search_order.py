"""Result ordering — newest sort and the near-tie recency preference."""
from __future__ import annotations

from agent_history.search import _order


def hit(text, ts, score=None, distance=None):
    return {"text": text, "timestamp": ts, "score": score, "distance": distance}


def texts(results):
    return [r["text"] for r in results]


def test_newest_sorts_by_timestamp_descending():
    got = _order([
        hit("middle", "2026-06-01T00:00", score=0.03),
        hit("newest", "2026-08-01T00:00", score=0.01),
        hit("oldest", "2026-01-01T00:00", score=0.02),
    ], "newest")
    assert texts(got) == ["newest", "middle", "oldest"]


def test_relevance_keeps_clear_winners_regardless_of_age():
    got = _order([
        hit("old but clearly better", "2026-01-01T00:00", score=0.033),
        hit("new but weaker", "2026-08-01T00:00", score=0.016),
    ], "relevance")
    assert texts(got)[0] == "old but clearly better"


def test_near_tied_scores_prefer_the_newer_chunk():
    got = _order([
        hit("stale near-tie", "2026-01-01T00:00", score=0.016730),
        hit("fresh near-tie", "2026-08-01T00:00", score=0.016690),
    ], "relevance")
    assert texts(got)[0] == "fresh near-tie", \
        "same rounded bucket must go to the newer chunk"


def test_vector_distances_get_the_same_tiebreak():
    got = _order([
        hit("stale", "2026-01-01T00:00", distance=14.12),
        hit("fresh", "2026-08-01T00:00", distance=14.08),
        hit("clearly worse", "2026-08-09T00:00", distance=19.9),
    ], "relevance")
    assert texts(got) == ["fresh", "stale", "clearly worse"]


def test_pure_text_mode_preserves_bm25_order():
    rows = [
        hit("bm25 first", "2026-01-01T00:00"),
        hit("bm25 second", "2026-08-01T00:00"),
    ]
    assert texts(_order(rows, "relevance")) == ["bm25 first", "bm25 second"]
    # ...but the explicit newest sort still applies to text results.
    assert texts(_order(rows, "newest")) == ["bm25 second", "bm25 first"]


def test_missing_timestamps_sort_last_in_newest():
    got = _order([
        hit("undated", "", score=0.03),
        hit("dated", "2026-08-01T00:00", score=0.01),
    ], "newest")
    assert texts(got) == ["dated", "undated"]
