"""Ollama embedding client."""
from __future__ import annotations

import ipaddress
import struct
from urllib.parse import urlparse

import requests

from . import config


class EmbedError(RuntimeError):
    pass


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


_endpoint_checked = False


def check_endpoint() -> None:
    """Refuse to send transcripts off-box unless explicitly permitted.

    Embedding sends the full text of every indexed message to $OLLAMA_URL. If
    that points somewhere other than this machine, the tool becomes a bulk
    exfiltration channel for the user's entire conversation history — so a
    non-loopback endpoint has to be opted into deliberately.
    """
    global _endpoint_checked
    if _endpoint_checked:
        return
    _endpoint_checked = True

    url = config.ollama_url()
    host = urlparse(url).hostname or ""
    if _is_loopback(host) or config.allow_remote():
        return
    raise EmbedError(
        f"OLLAMA_URL points at a remote host ({url}).\n"
        "Indexing sends the full text of your conversations there. If that is "
        "genuinely intended, set AGENT_HISTORY_ALLOW_REMOTE=1 to confirm."
    )


def embed(text: str, timeout: int = 120) -> list[float]:
    """Embed one string. Raises EmbedError with an actionable message."""
    check_endpoint()
    url = f"{config.ollama_url()}/api/embeddings"
    try:
        response = requests.post(
            url,
            json={"model": config.model(), "prompt": text},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise EmbedError(
            f"cannot reach Ollama at {config.ollama_url()} ({exc}). "
            "Is `ollama serve` running?"
        ) from exc

    if response.status_code == 404:
        raise EmbedError(
            f"model '{config.model()}' is not available. "
            f"Run: ollama pull {config.model()}"
        )
    try:
        response.raise_for_status()
        return response.json()["embedding"]
    except (requests.HTTPError, KeyError, ValueError) as exc:
        raise EmbedError(f"unexpected response from Ollama: {exc}") from exc


def probe_dimension() -> int:
    """Ask the configured model how many dimensions it produces."""
    return len(embed("dimension probe", timeout=60))


def pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def reachable() -> tuple[bool, str]:
    """Health check used by `doctor`."""
    try:
        response = requests.get(f"{config.ollama_url()}/api/tags", timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        return False, str(exc)
    names = {m.get("name", "") for m in response.json().get("models", [])}
    wanted = config.model()
    # Ollama reports "name:tag"; a bare model name matches its :latest tag.
    if wanted in names or f"{wanted}:latest" in names:
        return True, "ok"
    return False, f"model '{wanted}' not pulled (have: {', '.join(sorted(names)) or 'none'})"
