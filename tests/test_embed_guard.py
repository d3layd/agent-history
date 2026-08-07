"""The off-box exfiltration guard.

Indexing POSTs the full text of every message to $OLLAMA_URL, so a non-loopback
endpoint has to be a deliberate choice rather than an accident.
"""
from __future__ import annotations

import pytest

from agent_history import embed


@pytest.fixture(autouse=True)
def reset_guard():
    embed._endpoint_checked = False
    yield
    embed._endpoint_checked = False


@pytest.mark.parametrize("url", [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://127.0.0.5:11434",
    "https://[::1]:11434",
])
def test_loopback_endpoints_are_allowed(monkeypatch, url):
    monkeypatch.setenv("OLLAMA_URL", url)
    embed.check_endpoint()  # must not raise


@pytest.mark.parametrize("url", [
    "http://192.168.1.50:11434",
    "http://10.0.0.1:11434",
    "https://ollama.example.com",
    "http://8.8.8.8:11434",
])
def test_remote_endpoints_are_refused(monkeypatch, url):
    monkeypatch.setenv("OLLAMA_URL", url)
    with pytest.raises(embed.EmbedError) as e:
        embed.check_endpoint()
    message = str(e.value)
    assert "AGENT_HISTORY_ALLOW_REMOTE" in message
    assert "full text" in message, "the error must say what is at stake"


def test_remote_is_allowed_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "https://ollama.example.com")
    monkeypatch.setenv("AGENT_HISTORY_ALLOW_REMOTE", "1")
    embed.check_endpoint()


def test_guard_runs_before_any_request(monkeypatch):
    """embed() must refuse without touching the network."""
    monkeypatch.setenv("OLLAMA_URL", "https://ollama.example.com")

    def explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("a request was made despite the guard")

    monkeypatch.setattr(embed.requests, "post", explode)
    with pytest.raises(embed.EmbedError):
        embed.embed("some text")
