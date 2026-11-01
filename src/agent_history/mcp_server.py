"""`agent-history mcp` — the hub's history as MCP tools.

Three tools, for MCP clients such as claude.ai (through an MCP router):

- ``history_recall`` (read): ranked search, the same ``search.search_rows`` the
  CLI uses, with bounded output.
- ``history_show`` (read): the conversation around a hit, the same
  ``show.rows_for`` the CLI uses.
- ``history_record`` (write, append-only): a structured summary of a chat
  (title, decisions, findings, links). It is redacted with ``redact.redact``
  and written into the hub inbox as a normal shipped batch, so it is indexed
  and embedded exactly like every other Record. There is no update or delete
  tool, and the write path only ever creates new files.

Every call is authorised by a bearer token (``mcp_auth``): a token carries
``read`` and/or ``write`` scope and names exactly one tenant index. The tenant
comes from the token, never from the request.

The SDK (``pip install 'agent-history[mcp]'``) is imported only by
``build_server`` and ``serve``; the tool logic below does not need it.
"""
from __future__ import annotations

import contextlib
import datetime
import ipaddress
import json
import os
import re
import sys
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from . import config, embed, redact, search, show, store
from . import notes as notes_store
from .mcp_auth import Identity, RateLimiter, TokenStore, bearer

SURFACE = "claude-ai-chat"
RECORD_ROLE = "summary"
INBOX_FORMAT = 1

DEFAULT_K = 8
MAX_K = 20
MAX_QUERY_CHARS = 1000
EXCERPT_CHARS = 700
DEFAULT_SHOW_LIMIT = 20
MAX_SHOW_LIMIT = 50
SHOW_TEXT_CHARS = 1500

MAX_TITLE = 200
MAX_SUMMARY = 8000
MAX_ITEMS = 30
MAX_ITEM_CHARS = 1000
MAX_URL = 500
MAX_PROJECT = 200

TOOL_SCOPE = {"history_recall": "read", "history_show": "read",
              "history_record": "write", "history_note": "write"}
NOTES_OPTIONS = ("exclude", "include", "only")

_CHAT_URL = re.compile(
    r"^https://claude\.ai/(?:chat|project/[0-9a-f-]+/chat)/([0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#].*)?$")
_INBOX_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# config.tenant_home() points the process environment at one tenant's index,
# and tools run in worker threads, so index access is serialised. One person's
# chat traffic does not need more.
_INDEX_LOCK = threading.Lock()


class ToolFailure(Exception):
    """A refusal or bad input, reported to the caller as a tool error."""


# ------------------------------------------------------------------ helpers

@contextlib.contextmanager
def _tenant_index(tenant: str | None):
    """Hold the index lock with the data home pointed at ``tenant``. stdout
    is diverted so nothing a library prints can corrupt a stdio session."""
    with _INDEX_LOCK, contextlib.redirect_stdout(sys.stderr):
        if tenant is None:
            yield
        else:
            with config.tenant_home(tenant):
                yield


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _session_of(row: dict) -> str:
    """The id to hand back for ``history_show``: the immutable session uid
    where the index has one, else the agent's session id."""
    return row.get("session_uid") or row.get("session_id") or ""


def _resolve(session: str) -> str | None:
    """Map any session id or alias to the immutable uid, when the store
    supports it (``store.resolve_session``); None otherwise."""
    resolver = getattr(store, "resolve_session", None)
    if resolver is None:
        return None
    db = store.open_for_read(check_model=False)
    try:
        return resolver(db, session)
    finally:
        db.close()


def _require(identity: Identity | None, scope: str) -> Identity:
    if identity is None:
        raise ToolFailure("unauthorized")
    if not identity.can(scope):
        raise ToolFailure(f"this token does not have {scope} scope")
    return identity


# --------------------------------------------------------------------- read

def recall(identity: Identity | None, query: str, *, k: int = DEFAULT_K,
           agent: str | None = None, surface: str | None = None,
           cwd: str | None = None, machine: str | None = None,
           mode: str = "hybrid", sort: str = "relevance",
           notes: str = "exclude") -> dict:
    identity = _require(identity, "read")
    if notes not in NOTES_OPTIONS:
        raise ToolFailure(f"notes must be one of {', '.join(NOTES_OPTIONS)}")
    query = (query or "").strip()
    if not query:
        raise ToolFailure("query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise ToolFailure(f"query is longer than {MAX_QUERY_CHARS} characters")
    if mode not in search.MODES:
        raise ToolFailure(f"mode must be one of {', '.join(search.MODES)}")
    if sort not in search.SORTS:
        raise ToolFailure(f"sort must be one of {', '.join(search.SORTS)}")
    if surface and agent and surface != agent:
        raise ToolFailure("agent and surface name the same field; pass one")
    k = max(1, min(int(k), MAX_K))

    note_hits: list[dict] = []
    with _tenant_index(identity.tenant):
        if notes != "exclude":
            try:
                note_hits = notes_store.search(query, k=k, mode=mode)
            except embed.EmbedError as exc:
                raise ToolFailure(f"embedding unavailable ({exc}); "
                                  "retry with mode='text'") from None
        if notes == "only" or not config.db_path().exists():
            rows: list[dict] = []
        else:
            try:
                rows = search.search_rows(
                    query, k=k, cwd=cwd or None, agent=(agent or surface) or None,
                    machine=machine or None, mode=mode, sort=sort)
            except store.StoreError as exc:
                raise ToolFailure(str(exc)) from None
            except embed.EmbedError as exc:
                raise ToolFailure(f"embedding unavailable ({exc}); "
                                  "retry with mode='text'") from None

    results = []
    for rank, row in enumerate(rows[:k], 1):
        results.append({
            "rank": rank,
            "timestamp": row.get("timestamp") or "",
            "agent": row.get("agent") or "",
            "machine": row.get("machine") or "",
            "project": row.get("cwd") or "",
            "session": _session_of(row),
            "session_id": row.get("session_id") or "",
            "role": row.get("role") or "",
            "score": row.get("score"),
            "distance": row.get("distance"),
            "excerpt": _clip(row.get("text") or "", EXCERPT_CHARS),
        })
    if notes == "exclude":
        return {"query": query, "mode": mode, "count": len(results),
                "results": results}
    # Notes first (a note is a pointer to read), then transcript hits.
    for hit in results:
        hit["type"] = "transcript"
    merged = []
    for rank, hit in enumerate(note_hits + results, 1):
        merged.append({**hit, "rank": rank})
    return {"query": query, "mode": mode, "notes": notes, "count": len(merged),
            "results": merged}


def show_session(identity: Identity | None, session: str, *,
                 around: str | None = None, limit: int = DEFAULT_SHOW_LIMIT) -> dict:
    identity = _require(identity, "read")
    session = (session or "").strip()
    if not session or len(session) > 500:
        raise ToolFailure("session must be a session id as returned by history_recall")
    limit = max(1, min(int(limit), MAX_SHOW_LIMIT))

    with _tenant_index(identity.tenant):
        rows: list[dict] = []
        if config.db_path().exists():
            try:
                rows = show.rows_for(session, around=around, limit=limit)
                if not rows:
                    uid = _resolve(session)
                    if uid and uid != session:
                        rows = show.rows_for(uid, around=around, limit=limit)
            except store.StoreError as exc:
                raise ToolFailure(str(exc)) from None
    if not rows:
        raise ToolFailure(f"nothing indexed for session {session!r}")

    first = rows[0]
    return {
        "session": _session_of(first) or session,
        "session_id": first.get("session_id") or "",
        "agent": first.get("agent") or "",
        "machine": first.get("machine") or "",
        "project": first.get("cwd") or "",
        "messages": [{"timestamp": r.get("timestamp") or "",
                      "role": r.get("role") or "",
                      "text": _clip(r.get("text") or "", SHOW_TEXT_CHARS)}
                     for r in rows],
    }


# -------------------------------------------------------------------- write

def _items(name: str, values: list[str] | None) -> list[str]:
    values = values or []
    if not isinstance(values, list):
        raise ToolFailure(f"{name} must be a list of strings")
    if len(values) > MAX_ITEMS:
        raise ToolFailure(f"{name}: at most {MAX_ITEMS} items")
    out = []
    for value in values:
        if not isinstance(value, str):
            raise ToolFailure(f"{name} must be a list of strings")
        value = value.strip()
        if len(value) > MAX_ITEM_CHARS:
            raise ToolFailure(f"{name}: each item at most {MAX_ITEM_CHARS} characters")
        if value:
            out.append(value)
    return out


def record_text(title: str, summary: str, decisions: list[str],
                findings: list[str], links: list[str], chat_url: str | None,
                project: str | None, record_id: str) -> str:
    """The searchable body of a chat record (before redaction)."""
    lines = [f"[claude.ai chat record] {title}"]
    if chat_url:
        lines.append(f"Chat: {chat_url}")
    if project:
        lines.append(f"Project: {project}")
    lines.append(f"Record: {record_id}")
    lines += ["", "Summary:", summary]
    for heading, items in (("Decisions", decisions), ("Findings", findings),
                           ("Links", links)):
        if items:
            lines += ["", f"{heading}:"] + [f"- {item}" for item in items]
    return "\n".join(lines)


def _inbox_dir(identity: Identity) -> Path:
    root = config.inbox_dir()
    if root is None:
        raise ToolFailure("this server has no inbox (AGENT_HISTORY_INBOX is not set)")
    if not _INBOX_MACHINE.match(identity.machine):
        raise ToolFailure("this token's machine name is not usable as an inbox directory")
    base = root
    if identity.tenant is not None:
        if not config.valid_tenant(identity.tenant):
            raise ToolFailure("invalid tenant")
        base = root / identity.tenant
    if not base.is_dir():
        # The hub's operator creates tenant inboxes; a missing one is a
        # configuration error, not something a client may create.
        raise ToolFailure("the inbox for this token's tenant does not exist on the hub")
    target = base / identity.machine
    if target.is_symlink():
        raise ToolFailure("refusing a symlinked inbox directory")
    target.mkdir(exist_ok=True)
    if not config.WINDOWS:
        target.chmod(0o700)
    return target


def _write_batch(directory: Path, line: str, record_id: str) -> Path:
    """Write one immutable batch file, the way ``ship`` does: a hidden
    ``.part`` file (the inbox reader skips dot-files) renamed into place.
    O_EXCL on a fresh name means an existing batch is never touched."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{record_id}.jsonl"
    final = directory / name
    part = directory / f".{name}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(part, flags, 0o600), "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if final.exists():  # a uuid collision; never overwrite
        part.unlink(missing_ok=True)
        raise ToolFailure("record id collision; retry")
    os.replace(part, final)
    return final


def record(identity: Identity | None, *, title: str, summary: str,
           decisions: list[str] | None = None, findings: list[str] | None = None,
           links: list[str] | None = None, chat_url: str | None = None,
           project: str | None = None) -> dict:
    identity = _require(identity, "write")
    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise ToolFailure(f"title is required, at most {MAX_TITLE} characters")
    if not summary or len(summary) > MAX_SUMMARY:
        raise ToolFailure(f"summary is required, at most {MAX_SUMMARY} characters")
    decisions = _items("decisions", decisions)
    findings = _items("findings", findings)
    links = _items("links", links)
    chat_url = (chat_url or "").strip() or None
    if chat_url and (len(chat_url) > MAX_URL or not chat_url.startswith("https://")):
        raise ToolFailure(f"chat_url must be an https URL of at most {MAX_URL} characters")
    project = (project or "").strip() or None
    if project and (len(project) > MAX_PROJECT or "\n" in project):
        raise ToolFailure(f"project must be one line of at most {MAX_PROJECT} characters")

    record_id = "rec-" + uuid.uuid4().hex[:20]
    match = _CHAT_URL.match(chat_url or "")
    # Every milestone of one chat shares a session, so history_show on it
    # returns the chat's whole record trail.
    session_id = f"{SURFACE}-{match.group(1) if match else record_id}"
    text, found = redact.redact(record_text(
        title, summary, decisions, findings, links, chat_url, project, record_id))
    cwd, found_cwd = redact.redact(project or "")
    found.update(found_cwd)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    line = json.dumps({
        "v": INBOX_FORMAT, "machine": identity.machine, "agent": SURFACE,
        "origin": f"mcp:{SURFACE}/{record_id}", "session_id": session_id,
        "cwd": cwd, "timestamp": timestamp, "role": RECORD_ROLE, "text": text,
    }, ensure_ascii=False)
    directory = _inbox_dir(identity)
    _write_batch(directory, line, record_id)
    return {
        "record_id": record_id,
        "session": session_id,
        "timestamp": timestamp,
        "machine": identity.machine,
        "status": "queued: indexed on the hub's next run (about a minute)",
        "redacted": dict(Counter(found)),
    }


def note(identity: Identity | None, *, title: str, summary: str,
         decisions: list[str] | None = None, findings: list[str] | None = None,
         links: list[str] | None = None, chat_url: str | None = None,
         duet_channel_id: str | None = None, duet_message_id: str | None = None,
         session_id: str | None = None, session_machine: str | None = None,
         job_id: str | None = None, span_start: str | None = None,
         span_end: str | None = None, corrects: str | None = None) -> dict:
    """Append a session note. It goes to the tenant's notes inbox (never the
    transcript inbox) and is indexed into notes.db, apart from transcripts."""
    identity = _require(identity, "write")
    counter: Counter = Counter()
    try:
        source = notes_store.make_source(
            chat_url=chat_url, duet_channel_id=duet_channel_id,
            duet_message_id=duet_message_id, session_id=session_id,
            machine=session_machine, job_id=job_id)
        written = notes_store.build(
            title=title, summary=summary, source=source, decisions=decisions,
            findings=findings, links=links, span_start=span_start,
            span_end=span_end, corrects=corrects, author=f"mcp:{identity.name}",
            counter=counter)
    except notes_store.NoteError as exc:
        raise ToolFailure(str(exc)) from None
    directory = _inbox_dir(identity) / notes_store.NOTES_DIR
    if directory.is_symlink():
        raise ToolFailure("refusing a symlinked notes directory")
    try:
        notes_store.write_file(directory, written)
    except notes_store.NoteError as exc:
        raise ToolFailure(str(exc)) from None
    return {
        "note_id": written["note_id"],
        "created_at": written["created_at"],
        "source": written["source"],
        "show": notes_store.show_target(written["source"], written["span"]),
        "status": "queued: indexed into the notes store on the hub's next run",
        "redacted": dict(counter),
    }


# ---------------------------------------------------------------- MCP layer

def _dump(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1)


class Gate:
    """Who is calling, and may they? Authentication, scope and rate limits in
    one place, shared by the HTTP front door, the tool listing and every
    tool call."""

    def __init__(self, tokens: TokenStore | None, fixed: Identity | None = None,
                 limiter: RateLimiter | None = None) -> None:
        self.tokens = tokens
        self.fixed = fixed  # stdio / in-process only: no headers to read
        self.limiter = limiter or RateLimiter()

    def from_headers(self, headers) -> Identity | None:
        if self.tokens is None or headers is None:
            return None
        value = headers.get("authorization") if hasattr(headers, "get") else None
        return self.tokens.authenticate(bearer(value))

    def identity(self, headers) -> Identity | None:
        """HTTP requests are judged by their header alone; the fixed identity
        applies only where the transport has no headers at all."""
        if headers is not None:
            return self.from_headers(headers)
        return self.fixed

    def authorize(self, identity: Identity | None, tool: str) -> Identity:
        """Raise ToolFailure unless ``identity`` may call ``tool`` now."""
        scope = TOOL_SCOPE.get(tool)
        if identity is None:
            raise ToolFailure("unauthorized")
        if scope is None or not identity.can(scope):
            raise ToolFailure(f"this token may not call {tool}")
        if not self.limiter.allow(identity.name, scope):
            raise ToolFailure("rate limit reached; try again in a minute")
        return identity

    async def middleware(self, ctx, call_next):
        """List only the tools this caller's token may use. Calls are checked
        again inside each tool, so knowing a hidden name gets nothing."""
        if getattr(ctx, "method", None) != "tools/list":
            return await call_next(ctx)
        identity = self.identity(_headers_of(ctx))
        result = await call_next(ctx)
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            allowed = identity.scopes if identity else frozenset()
            result = {**result, "tools": [
                t for t in result["tools"]
                if TOOL_SCOPE.get(t.get("name")) in allowed]}
        return result


def _headers_of(ctx) -> Any:
    request = getattr(ctx, "request", None)
    return getattr(request, "headers", None) if request is not None else None


# ------------------------------------------------------------------ serving

def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class BearerFrontDoor:
    """ASGI wrapper: no valid token, no MCP. ``/healthz`` answers without a
    token and says nothing but "ok"."""

    def __init__(self, app, gate: Gate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("path") == "/healthz":
                return await _plain(send, 200, b"ok\n")
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if self.gate.from_headers(headers) is None:
                return await _plain(send, 401, b'{"error":"unauthorized"}\n',
                                    content_type=b"application/json",
                                    extra=[(b"www-authenticate", b"Bearer")])
        await self.app(scope, receive, send)


async def _plain(send, status: int, body: bytes, *,
                 content_type: bytes = b"text/plain",
                 extra: list | None = None) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type),
                            (b"content-length", str(len(body)).encode()),
                            *(extra or [])]})
    await send({"type": "http.response.body", "body": body})


def http_app(gate: Gate, *, host: str = "127.0.0.1", path: str = "/mcp"):
    """The ASGI app: bearer front door around a stateless, JSON-response
    Streamable HTTP endpoint (one request, one answer — simple to proxy)."""
    from .mcp_app import build_server

    server = build_server(gate)
    inner = server.streamable_http_app(streamable_http_path=path,
                                       stateless_http=True, json_response=True,
                                       host=host)
    door = BearerFrontDoor(inner, gate)
    door.server = server  # tests drive its session manager directly
    return door


def _sdk_missing() -> int:
    print("error: the MCP server needs the optional dependency: "
          "uv tool install 'agent-history[mcp]' (or pip install "
          "'agent-history[mcp]')", file=sys.stderr)
    return 1


def serve(*, transport: str = "http", host: str = "127.0.0.1", port: int = 8765,
          allow_non_loopback: bool = False) -> int:
    from .mcp_auth import TokenConfigError

    try:
        import mcp  # noqa: F401
    except ImportError:
        return _sdk_missing()
    try:
        tokens = TokenStore()
    except TokenConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if transport == "stdio":
        # No headers on stdio: the caller proves itself once, with a token in
        # the environment (never argv), checked against the same store.
        token = os.environ.get("AGENT_HISTORY_MCP_TOKEN", "").strip()
        identity = tokens.authenticate(token)
        if identity is None:
            print("error: stdio needs AGENT_HISTORY_MCP_TOKEN set to a valid token",
                  file=sys.stderr)
            return 1
        from .mcp_app import build_server

        build_server(Gate(tokens, fixed=identity)).run("stdio")
        return 0

    if len(tokens) == 0:
        print(f"error: no tokens configured. Create one with "
              f"`agent-history mcp token add` (stored in {tokens.path})",
              file=sys.stderr)
        return 1
    if not _is_loopback(host) and not allow_non_loopback:
        print(f"error: refusing to bind {host}: this server is meant to sit on "
              "loopback behind the MCP router. Pass --allow-non-loopback only "
              "behind an authenticating, TLS-terminating proxy.", file=sys.stderr)
        return 1

    import uvicorn

    app = http_app(Gate(tokens), host=host)
    print(f"agent-history mcp: http://{host}:{port}/mcp "
          f"({len(tokens)} token(s) from {tokens.path})", file=sys.stderr)
    # No access log: request lines carry nothing secret, but there is no need
    # to keep a trail of every query on disk either.
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0
