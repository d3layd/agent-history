"""Text normalisation shared by adapters and the indexer."""
from __future__ import annotations

import re

MIN_CHARS = 20
MAX_CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200

# Text this tool itself injects into a prompt (local inject hook, fleet
# recall injectors). If an agent persists it into a transcript, re-indexing
# it would create a feedback loop — recalled memories becoming new memories
# that get recalled again. The indexer refuses any record starting with one
# of these markers, at a single choke point covering every adapter.
INJECTION_MARKERS = (
    "Possibly relevant excerpts from past agent sessions",  # local inject.py
    "AUTO-RECALLED CONTEXT",                                # fleet injectors
)


def is_injection(text: str) -> bool:
    return text.lstrip().startswith(INJECTION_MARKERS)

# Long base64-ish runs are embedded payloads (images, archives, keys). Embedding
# them wastes time and produces vectors that match nothing meaningful.
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/=_-]{300,}")


def scrub(text: str) -> str:
    """Replace encoded blobs with a placeholder."""
    return _BASE64_RUN.sub("[blob]", text)


def chunk_text(text: str) -> list[str]:
    """Split into overlapping chunks small enough to embed."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + MAX_CHUNK_CHARS, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks
