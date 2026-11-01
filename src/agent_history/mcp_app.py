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
                         _dump, note, recall, record, show_session)

INSTRUCTIONS = (
    "Search and add to the user's history of AI conversations (Claude Code, "
    "claude.ai, other agents, every machine). Use history_recall when past "
    "context would help, history_show to read around a hit, and history_record "
    "to save decisions, findings and links at milestones and when a chat wraps "
    "up. history_note adds a session note (a summary with a link to its "
    "source), kept apart from transcripts. Records and notes are append-only "
    "and secrets are redacted before storage."
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
            "sort: relevance (default) or newest; notes: exclude (default, "
            "transcripts only), include (matching session notes first, then "
            "transcripts) or only (session notes alone). A note's show field "
            "is the history_show call that opens its source span."),
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
        notes: Literal["exclude", "include", "only"] = "exclude",
    ) -> str:
        return run("history_recall", ctx, recall, query, k=k, agent=agent, surface=surface,
                   cwd=cwd, machine=machine, mode=mode, sort=sort, notes=notes)

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

    @server.tool(
        name="history_note",
        description=(
            "Save a session note: a short summary of what a stretch of "
            "conversation covered, with a link back to its source. Notes are "
            "kept apart from transcripts and found with history_recall "
            "notes=include or notes=only. Call it when a session or a "
            "stretch of work ends. Append-only: to correct one, add a new note "
            "with corrects=<note id>. Secrets are redacted. title: one line. "
            "summary: a few sentences. decisions / findings / links: short "
            "items. Source, exactly one of: chat_url (https://claude.ai/chat/"
            "...); duet_channel_id + duet_message_id (the message that starts "
            "the span); session_id + session_machine (+ job_id cj_... for a "
            "claude -p job). span_start / span_end: ISO timestamps of the span. "
            "A note without a valid source is refused."),
        annotations=ToolAnnotations(title="Add a session note", read_only_hint=False,
                                    destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=False),
        structured_output=False,
    )
    def history_note(
        title: str,
        summary: str,
        ctx: Context,
        decisions: list[str] | None = None,
        findings: list[str] | None = None,
        links: list[str] | None = None,
        chat_url: str | None = None,
        duet_channel_id: str | None = None,
        duet_message_id: str | None = None,
        session_id: str | None = None,
        session_machine: str | None = None,
        job_id: str | None = None,
        span_start: str | None = None,
        span_end: str | None = None,
        corrects: str | None = None,
    ) -> str:
        return run("history_note", ctx, note, title=title, summary=summary,
                   decisions=decisions, findings=findings, links=links,
                   chat_url=chat_url, duet_channel_id=duet_channel_id,
                   duet_message_id=duet_message_id, session_id=session_id,
                   session_machine=session_machine, job_id=job_id,
                   span_start=span_start, span_end=span_end, corrects=corrects)

    return server
