"""Hosted sessions: agents running in a provider's cloud, with no machine name.

The ``hosted-session`` source kind points a note at one of these sessions by
provider, session id and link. No transcript of them is indexed, so the link is
how a reader gets back to the conversation.

``PROVIDERS`` is the whole list of providers a note may name. A note with any
other provider is refused, so an agent can't grow the list by typing a new name.
Adding one is a reviewed change to this file. Run
``agent-history notes providers --check NAME --host HOST`` first: it normalizes
the name to a slug and checks for duplicates, and it never writes anything.
Only register a host and URL shape you have seen in real sessions.

A link is accepted only if it equals the one URL built from the registered host,
path prefix and session id. So there is no substring or suffix match to spoof,
no userinfo, port, query or fragment, and the id always matches the link.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass

from . import redact

KIND = "hosted-session"
MAX_URL = 500
MAX_SESSION_ID = 128
MAX_SLUG = 40

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")


class HostedError(ValueError):
    """A hosted-session source, or a registry addition, that can't be accepted.
    Messages never repeat the refused value: it may hold a secret."""


@dataclass(frozen=True)
class Provider:
    slug: str
    host: str
    path_prefix: str
    id_pattern: re.Pattern[str]

    def url_for(self, session_id: str) -> str:
        return f"https://{self.host}{self.path_prefix}{session_id}"

    @property
    def example(self) -> str:
        return self.url_for("<session id>")


# claude-ai: the shape is from this repo's own Claude-Session commit trailers
# (https://claude.ai/code/session_ + 24 letters and digits). The bound is wider
# than that sample so a longer id still works; other characters are refused.
PROVIDERS: dict[str, Provider] = {p.slug: p for p in (
    Provider(slug="claude-ai", host="claude.ai", path_prefix="/code/",
             id_pattern=re.compile(r"session_[A-Za-z0-9]{1,120}")),
)}


def slugs() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def describe() -> str:
    """The provider options, for tool descriptions and refusals."""
    return "; ".join(f"{p.slug} ({p.example})" for p in PROVIDERS.values())


def provider_for_id(session_id: str) -> str | None:
    """The provider whose session ids look like this one, if any."""
    for provider in PROVIDERS.values():
        if provider.id_pattern.fullmatch(session_id):
            return provider.slug
    return None


def _check_url(provider: Provider, session_id: str, url: object) -> None:
    expected = provider.url_for(session_id)
    if not isinstance(url, str) or not url:
        raise HostedError("a hosted session needs its session_url")
    if url == expected:
        return
    # It's refused either way. The rest only finds the most useful reason.
    if len(url) > MAX_URL:
        raise HostedError(f"session_url: at most {MAX_URL} characters")
    want = f"session_url must be exactly {provider.example}"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise HostedError(f"session_url can't be parsed; {want}") from None
    if parts.scheme != "https" or not url.startswith("https://"):
        raise HostedError(f"session_url must use https; {want}")
    if "@" in parts.netloc:
        raise HostedError(f"session_url must not hold a user or password; {want}")
    if parts.netloc != provider.host:
        raise HostedError(f"session_url host must be exactly {provider.host} "
                          f"for provider {provider.slug}; {want}")
    if "?" in url or "#" in url:
        raise HostedError(f"session_url must not have a query or fragment; {want}")
    raise HostedError(f"session_url must be this session_id's own link; {want}")


def check(provider: object, session_id: object, session_url: object) -> tuple[str, str, str]:
    """(slug, session id, url) for a valid source. No normalizing: the values
    must already be exact, and anything else raises ``HostedError``."""
    entry = PROVIDERS.get(provider) if isinstance(provider, str) else None
    if entry is None:
        raise HostedError(f"provider must be one of: {', '.join(slugs())}. An agent "
                          "can't add one: a maintainer adds it to the registry")
    if not isinstance(session_id, str) or not session_id:
        raise HostedError("a hosted session needs its session_id")
    if len(session_id) > MAX_SESSION_ID or not _SESSION_ID.fullmatch(session_id):
        raise HostedError(f"session_id: at most {MAX_SESSION_ID} characters, letters, "
                          "digits, _ and - only")
    if not entry.id_pattern.fullmatch(session_id):
        raise HostedError(f"session_id doesn't look like a {entry.slug} session id; "
                          f"its link has the form {entry.example}")
    _check_url(entry, session_id, session_url)
    # Redacting an id would break its link, so one that looks like a secret is
    # refused. This is what keeps ids and links whole in redact_note.
    if redact.spans(session_id):
        raise HostedError("session_id looks like a secret")
    return entry.slug, session_id, session_url


# ------------------------------------------------------------ adding a provider

def normalize_slug(name: object) -> str:
    """A provider name as a lowercase slug: ``Codex Cloud`` -> ``codex-cloud``."""
    if not isinstance(name, str):
        raise HostedError("a provider name must be text")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    if not slug:
        raise HostedError("a provider name needs letters or digits")
    if len(slug) > MAX_SLUG:
        raise HostedError(f"a provider slug is at most {MAX_SLUG} characters")
    return slug


def valid_host(host: str) -> bool:
    return bool(_HOST.fullmatch(host or ""))


def plan_addition(name: object, host: str) -> dict:
    """The checks for a new registry entry. Changes nothing. Two slugs that
    differ only by hyphens (``claude-ai``, ``claudeai``) count as duplicates."""
    slug = normalize_slug(name)
    compact = slug.replace("-", "")
    for existing in PROVIDERS:
        if existing.replace("-", "") == compact:
            raise HostedError(f"{slug} is already registered as {existing}")
    if not valid_host(host):
        raise HostedError("the host must be a bare lowercase hostname, like example.com")
    return {"slug": slug, "host": host}
