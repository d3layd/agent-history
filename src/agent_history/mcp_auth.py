"""Bearer tokens for `agent-history mcp`: scopes, tenants, rate limits.

A token is a random string the MCP client (the router) sends as
``Authorization: Bearer <token>``. The hub stores only its SHA-256, in a JSON
file that must be ``0600`` and owned by the user running the server:

    {"tokens": [
      {"name": "router-read",  "sha256": "<hex>", "scope": ["read"],  "tenant": "dennis"},
      {"name": "router-write", "sha256": "<hex>", "scope": ["write"], "tenant": "dennis",
       "machine": "claude-ai"}
    ]}

- ``scope``: ``read`` (``history_recall``, ``history_show``) and/or ``write``
  (``history_record``). A write token cannot read and a read token cannot write.
- ``tenant``: the one tenant index this token opens (``null`` on a hub without
  tenants). Never taken from the request.
- ``machine``: the provenance a write token's records are filed under (the
  inbox directory), so the index says which client wrote them. Default
  ``mcp-<name>``.

Tokens can also come from the environment for a single-tenant setup
(``AGENT_HISTORY_MCP_READ_TOKEN`` / ``AGENT_HISTORY_MCP_WRITE_TOKEN``, tenant
``AGENT_HISTORY_MCP_TENANT``). Never from argv: a command line is visible to
every local user in ``ps``.

Nothing in this module prints or logs a token.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

SCOPES = ("read", "write")
TOKEN_PREFIX = "ahmcp_"
MIN_TOKEN_CHARS = 32
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Calls per minute per token. Cheap protection against a runaway agent loop,
# not a security boundary: the router in front has its own limits.
RATE_PER_MINUTE = {"read": 60, "write": 20}


class TokenConfigError(RuntimeError):
    """The token file is missing, unreadable, too permissive, or malformed."""


@dataclass(frozen=True)
class Identity:
    name: str
    scopes: frozenset[str]
    tenant: str | None
    machine: str

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def tokens_path() -> Path:
    raw = (config._env("AGENT_HISTORY_MCP_TOKENS", "") or "").strip()
    return Path(raw).expanduser() if raw else config.data_home() / "mcp-tokens.json"


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _check_file_mode(path: Path) -> None:
    """Refuse a token file anyone but its owner could read or replace."""
    if config.WINDOWS:
        return
    st = path.stat()
    if st.st_mode & 0o077:
        raise TokenConfigError(
            f"{path} must be mode 0600 (it is {oct(st.st_mode & 0o777)}); "
            f"run: chmod 600 {path}")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise TokenConfigError(f"{path} is not owned by the user running the server")


def _parse_entry(raw: object, where: str) -> tuple[bytes, Identity]:
    if not isinstance(raw, dict):
        raise TokenConfigError(f"{where}: each token must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise TokenConfigError(f"{where}: invalid name {name!r}")
    hexd = raw.get("sha256")
    if not isinstance(hexd, str) or not _HEX64.match(hexd):
        raise TokenConfigError(f"{where} ({name}): sha256 must be 64 lowercase hex chars")
    scope = raw.get("scope")
    scopes = [scope] if isinstance(scope, str) else scope
    if (not isinstance(scopes, list) or not scopes
            or any(s not in SCOPES for s in scopes)):
        raise TokenConfigError(f"{where} ({name}): scope must be read, write, or a list of them")
    tenant = raw.get("tenant")
    if tenant is not None and (not isinstance(tenant, str) or not config.valid_tenant(tenant)):
        raise TokenConfigError(f"{where} ({name}): invalid tenant {tenant!r}")
    machine = raw.get("machine") or f"mcp-{name}"
    if not isinstance(machine, str) or not _MACHINE.match(machine):
        raise TokenConfigError(f"{where} ({name}): invalid machine {machine!r}")
    return bytes.fromhex(hexd), Identity(name, frozenset(scopes), tenant, machine)


class TokenStore:
    """Token digests and the identity each maps to.

    The file is re-read when its mtime changes, so adding or revoking a token
    takes effect without a restart. A file that turns invalid keeps the last
    good set in force rather than locking everyone out, or letting everyone in.
    """

    def __init__(self, path: Path | None = None, *, use_env: bool = True) -> None:
        self.path = path or tokens_path()
        self.use_env = use_env
        self._entries: list[tuple[bytes, Identity]] = []
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self.reload(strict=True)

    # -- loading -------------------------------------------------------------

    def _env_entries(self) -> list[tuple[bytes, Identity]]:
        if not self.use_env:
            return []
        tenant = (config._env("AGENT_HISTORY_MCP_TENANT", "") or "").strip() or None
        if tenant is not None and not config.valid_tenant(tenant):
            raise TokenConfigError(f"AGENT_HISTORY_MCP_TENANT: invalid tenant {tenant!r}")
        machine = (config._env("AGENT_HISTORY_MCP_MACHINE", "") or "").strip() or "mcp-env"
        if not _MACHINE.match(machine):
            raise TokenConfigError(f"AGENT_HISTORY_MCP_MACHINE: invalid machine {machine!r}")
        out = []
        for scope in SCOPES:
            var = f"AGENT_HISTORY_MCP_{scope.upper()}_TOKEN"
            token = (os.environ.get(var) or "").strip()
            if not token:
                continue
            if len(token) < MIN_TOKEN_CHARS:
                raise TokenConfigError(f"{var} is shorter than {MIN_TOKEN_CHARS} characters")
            out.append((digest(token),
                        Identity(f"env-{scope}", frozenset([scope]), tenant, machine)))
        return out

    def _file_entries(self) -> list[tuple[bytes, Identity]]:
        if not self.path.exists():
            return []
        _check_file_mode(self.path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TokenConfigError(f"cannot read {self.path}: {exc}") from None
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, list):
            raise TokenConfigError(f'{self.path}: expected {{"tokens": [...]}}')
        entries = [_parse_entry(t, str(self.path)) for t in tokens]
        names = [i.name for _, i in entries]
        if len(set(names)) != len(names):
            raise TokenConfigError(f"{self.path}: duplicate token name")
        return entries

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload(self, strict: bool = False) -> None:
        stamp = self._file_stamp()
        try:
            entries = self._file_entries() + self._env_entries()
        except TokenConfigError:
            if strict:
                raise
            return  # keep the last good set
        with self._lock:
            self._entries = entries
            self._stamp = stamp

    def __len__(self) -> int:
        return len(self._entries)

    # -- checking ------------------------------------------------------------

    def authenticate(self, token: str | None) -> Identity | None:
        """The identity for ``token``, or None. Every stored digest is
        compared, in constant time, whatever matches first."""
        if self._file_stamp() != self._stamp:
            self.reload()
        if not token:
            return None
        candidate = digest(token)
        found: Identity | None = None
        for stored, identity in self._entries:
            if hmac.compare_digest(stored, candidate) and found is None:
                found = identity
        return found


def bearer(value: str | None) -> str | None:
    """The token from an ``Authorization`` header value."""
    if not value:
        return None
    match = re.match(r"^\s*Bearer\s+(\S+)\s*$", value, re.IGNORECASE)
    return match.group(1) if match else None


class RateLimiter:
    """Sliding one-minute window per (token, scope)."""

    def __init__(self, per_minute: dict[str, int] | None = None,
                 clock=time.monotonic) -> None:
        self.per_minute = dict(per_minute or RATE_PER_MINUTE)
        self.clock = clock
        self._hits: dict[tuple[str, str], collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, who: str, scope: str) -> bool:
        limit = self.per_minute.get(scope, 0)
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault((who, scope), collections.deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


# -------------------------------------------------------------- file editing

def _write_file(path: Path, tokens: list[dict]) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        if not config.WINDOWS:
            path.parent.chmod(0o700)
    tmp =path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(tmp, flags, 0o600), "w", encoding="utf-8") as fh:
        json.dump({"tokens": tokens}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    config.secure_file(path)


def _read_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    _check_file_mode(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("tokens", []))


def add_token(name: str, scopes: list[str], tenant: str | None,
              machine: str | None = None, path: Path | None = None) -> str:
    """Create a token, store its hash, and return the token (shown once)."""
    path = path or tokens_path()
    tokens = _read_raw(path)
    if any(t.get("name") == name for t in tokens):
        raise TokenConfigError(f"a token named {name!r} already exists; revoke it first")
    token = new_token()
    entry: dict = {"name": name, "sha256": digest(token).hex(),
                   "scope": sorted(set(scopes)), "tenant": tenant}
    if machine:
        entry["machine"] = machine
    _parse_entry(entry, "new token")  # same validation as on load
    _write_file(path, [*tokens, entry])
    return token


def revoke_token(name: str, path: Path | None = None) -> bool:
    path = path or tokens_path()
    tokens = _read_raw(path)
    kept = [t for t in tokens if t.get("name") != name]
    if len(kept) == len(tokens):
        return False
    _write_file(path, kept)
    return True


def list_tokens(path: Path | None = None) -> list[dict]:
    """Names, scopes, tenants and machines; never the hashes."""
    return [{k: t.get(k) for k in ("name", "scope", "tenant", "machine")}
            for t in _read_raw(path or tokens_path())]
