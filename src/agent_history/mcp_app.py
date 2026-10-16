"""The MCP tool definitions. Imports the SDK (the ``mcp`` extra).

Kept apart from ``mcp_server`` because the SDK reads these signatures to build
the tool schemas, so the annotations must be real objects (no
``from __future__ import annotations`` here), and because nothing else in the
package should need the SDK installed.
"""
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .mcp_auth import Identity
from .mcp_server import (DEFAULT_K, DEFAULT_SHOW_LIMIT, Gate, ToolFailure,
                         _dump, recall, record, show_session)

INSTRUCTIONS = (
    "Search and add to the user's history of AI conversations (Claude Code, "
    "claude.ai, other agents, every machine). Use history_recall when past "
    "context would help, history_show to read around a hit, and history_record "
    "to save decisions, findings and links at milestones and when a chat wraps "
    "up. Records are append-only and secrets are redacted before storage."
)


def build_server(gate: Gate) -> MCPServer:
    server = MCPServer(name="agent-history", version=__version__,
                       instructions=INSTRUCTIONS, middleware=[gate.middleware],
                       # INFO logs a line per request (and tool errors with
                       # their text); the journal needs only problems.
                       log_level="WARNING")

    def run(tool: str, ctx: Context, fn, *args, **kwargs) -> str:
        try:
            identity: Identity = gate.authorize(gate.identity(ctx.headers), tool)
            return _dump(fn(identity, *args, **kwargs))
        except ToolFailure as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="history_recall",
        description=(
            "Search the user's past AI conversations and saved chat records "
            "(every agent and machine). Returns ranked hits with timestamp, "
            "agent, machine, project, session id, role, a text excerpt and a "
            "score. Pass a hit's session to history_show to read around it. "
            "Arguments: query (natural language or exact identifiers); k "
            "(results, default 8, max 20); agent or surface (e.g. claude-code, "
            "codex, claude-ai-chat); cwd (substring of the project directory); "
            "machine; mode: hybrid (default), text (exact words) or vector; "
            "sort: relevance (default) or newest."),
        annotations=ToolAnnotations(title="Recall from history", read_only_hint=True,
                                    open_world_hint=False),
        structured_output=False,
    )
    def history_recall(
        query: str,
        ctx: Context,
        k: int = DEFAULT_K,
        agent: str | None = None,
        surface: str | None = None,
        cwd: str | None = None,
        machine: str | None = None,
        mode: Literal["hybrid", "text", "vector"] = "hybrid",
        sort: Literal["relevance", "newest"] = "relevance",
    ) -> str:
        return run("history_recall", ctx, recall, query, k=k, agent=agent, surface=surface,
                   cwd=cwd, machine=machine, mode=mode, sort=sort)

    @server.tool(
        name="history_show",
        description=(
            "Read the conversation a history_recall hit belongs to, oldest "
            "first. session: the hit's session. around: optionally the hit's "
            "timestamp, to centre the window on it. limit: messages (default "
            "20, max 50)."),
        annotations=ToolAnnotations(title="Show a past conversation", read_only_hint=True,
                                    open_world_hint=False),
        structured_output=False,
    )
    def history_show(session: str, ctx: Context, around: str | None = None,
                     limit: int = DEFAULT_SHOW_LIMIT) -> str:
        return run("history_show", ctx, show_session, session, around=around, limit=limit)

    @server.tool(
        name="history_record",
        description=(
            "Save a record of this chat to the user's history: what was "
            "decided, what was found, and the useful links. Call it at "
            "milestones and when the chat wraps up. Append-only: a record "
            "cannot be changed or deleted, so a later call adds a new one. "
            "Secrets are redacted before storage. title: one line. summary: "
            "what happened, a few sentences. decisions / findings / links: "
            "short items. chat_url: this chat's https://claude.ai/chat/... URL "
            "(records of one chat are grouped under one session). project: the "
            "project or repo it concerns. Returns the record id."),
        annotations=ToolAnnotations(title="Record to history", read_only_hint=False,
                                    destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False),
        structured_output=False,
    )
    def history_record(
        title: str,
        summary: str,
        ctx: Context,
        decisions: list[str] | None = None,
        findings: list[str] | None = None,
        links: list[str] | None = None,
        chat_url: str | None = None,
        project: str | None = None,
    ) -> str:
        return run("history_record", ctx, record, title=title, summary=summary,
                   decisions=decisions, findings=findings, links=links,
                   chat_url=chat_url, project=project)

    return server
