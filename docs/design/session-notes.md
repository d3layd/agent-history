# Design note: session notes (piece C)

Status: draft, for review with the PR that implements it.

## Goal

Next to the raw transcripts, keep a short, searchable **note** per stretch of
conversation that says what was talked about and points back to the exact
source, so an agent can find the right conversation fast and then read the
full transcript. Dennis, Nov 1: "a side system... that allows you to log
session notes as you go, either when sessions come to an end or after a few
minutes of inactivity, just kind of summarizing what was talked about in that
conversation and providing references for you to get back to it."

A summary is not a transcription, so notes live in their own store. They
never enter the transcript index, and nothing in the transcript index is
changed by a note.

## Rules (from the runbook's planned-feature table)

| rule | how |
|---|---|
| Stored apart from transcripts, own type; searches can include or exclude them | `notes.db` next to `index.db`, its own FTS and vectors. `history_recall(notes="exclude" \| "include" \| "only")`, default `exclude`, so today's callers see no change. `agent-history notes search` searches notes alone. |
| Written at session end and after a few minutes of inactivity (default 5) | `agent-history notes sweep`: a 1-minute user timer with `--idle-minutes 5`, plus an opt-in Claude Code `SessionEnd` hook (`agent-history notes hook`). Agents can also write one directly (`history_note`, `notes add`). |
| A resumed session gets a new note covering only what is new | Per session the sweep keeps the last timestamp a note covered; the next note's span starts after it. |
| Each note links to its source; a note without a resolvable source is rejected | Exactly one of: claude.ai conversation URL; Duet channel id + message id; Claude Code session id + machine (+ job id for `claude -p`). Anything else is refused. |
| `history_recall` can return notes first; each link opens the source span with `history_show` | `notes="include"` lists matching notes before transcript hits. Each note carries `show: {session, around}` for `history_show`. |
| Append-only: never changed or deleted; a correction is a new note | No update or delete path; SQLite triggers abort `UPDATE`/`DELETE` on the table; files are written `O_EXCL`; `corrects: <note id>` links a correction. One admin-only exception, `notes scrub` (below), for a leaked secret. |

## Data flow

```
writers                                   transport                         hub (tenant)
-------                                   ---------                         ------------
Claude Code / claude -p machine:
  notes sweep (timer, idle >= 5 min)  ─┐
  notes hook  (SessionEnd)            ─┼─> notes-outbox/*.jsonl ──rsync, same upload-only key──>
  notes add   (an agent, by hand)     ─┘          (redacted)        inbox/<tenant>/<machine>/notes/
                                                                              │
claude.ai / Duet / any MCP client:                                            │
  history_note (Router, write scope) ──────────── writes directly ──> inbox/<tenant>/<machine>/notes/
                                                                              ▼
                                       agent-history-hub-index@<tenant> (existing unit; trigger now
                                       also runs notes indexing) ──> tenants/<tenant>/notes.db
                                                                              ▼
                                       history_recall(notes=...) / agent-history notes search
```

A machine with no hub configured writes straight into its own `notes.db`.

## File, path and wire layout

- Hub inbox: `inbox/<tenant>/<machine>/notes/<stamp>-<note id>.jsonl`. The
  existing transcript reader globs only `<machine>/*.jsonl`, one level, so an
  older hub never reads a note as a transcript. A note line also has no
  `text` field, which the transcript parser requires. Two independent guards.
- Retired to `inbox/.processed/<machine>/notes/` after indexing.
- Shipper outbox: `<data home>/notes-outbox/`, uploaded to `<ingest>notes/`.
- Sweep state: `<data home>/notes-state.db` (session key → last covered
  timestamp, last note id).

One note per line:

```json
{"v": 1, "kind": "note", "note_id": "note-3f9c…", "created_at": "2026-11-01T23:10:00Z",
 "author": "sweep:last-message",
 "source": {"kind": "claude-code", "machine": "claude-jobs",
            "session_id": "5f2b8c1e-…", "job_id": "cj_20261101T120000Z_a1b2c3"},
 "span": {"start": "2026-11-01T12:00:01Z", "end": "2026-11-01T12:41:09Z"},
 "title": "Login redirect loop fixed on staging",
 "summary": "…", "decisions": ["…"], "findings": ["…"], "links": ["https://github.com/…/pull/12"],
 "corrects": null}
```

Source kinds and their `history_show` link:

| kind | required | `show.session` |
|---|---|---|
| `claude-ai` | `url` = `https://claude.ai/chat/<uuid>` (or `/project/<id>/chat/<uuid>`) | the conversation uuid (resolves for imported claude.ai chats) |
| `duet` | `channel_id`, `message_id` (the message that starts the span) | none yet: Duet transcripts are not on the hub; the note still records where to look |
| `claude-code` | `session_id`, `machine`; `job_id` (`cj_…`) for `claude -p` | the job id when there is one (piece A files a job under its id), else the session id; `around` = span start |

On the hub, the machine a note is filed under is the inbox directory it
arrived in, as for transcripts. The machine named inside a Claude Code
source is kept as the source's claim.

## Redaction

Every text field (title, summary, items, links, source fields) goes through
`redact.redact()` before the note is written anywhere: in `notes add`, the
sweep, and `history_note`. The hub redacts again when it indexes a note, so
a note from an older or buggy writer is still clean in `notes.db`. The sweep
redacts the transcript text it hands to the summarizer, so a summary cannot
repeat a secret it never saw.

## Who writes the note (the runbook's open question)

Both, through one validated path:

- **The agent**, at the end of its work: `history_note` over the Router
  (claude.ai, Duet) or `agent-history notes add` (Claude Code, `claude -p`).
  This writes the best note, because the agent knows what mattered.
- **The sweep**, as the safety net, for sessions that ended or went idle
  without one.

The hub does not summarize: it has only an embedding model.

## The sweep's summarizer: a model summary (Dennis, Nov 1)

Sessions wander, so the first prompt and the last message don't represent
them. The default summarizer is a model:

- `claude -p --model haiku --output-format json --max-turns 1
  --no-session-persistence --tools ""` with a fixed instruction, the span as
  JSON on stdin. It uses the machine's existing Claude login: on claude-jobs
  that is the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`). The child
  process never sees `ANTHROPIC_API_KEY`: it is removed from the environment
  before the call, so the production key can't be billed by accident.
  `AGENT_HISTORY_NOTES_MODEL` and `AGENT_HISTORY_CLAUDE_BIN` override the
  model and binary.
- The transcript is redacted before it goes to the model, and the model's
  output is redacted again when the note is built.
- The summarizer's own call is not persisted (`--no-session-persistence`),
  and its instruction starts with a marker the sweep skips, so a summary is
  never itself summarized.
- **Fallback:** if the model call fails (no `claude`, signed out, timeout,
  bad JSON), the note is built from the first prompt and the last assistant
  message and marked `fallback: true` (author `sweep:fallback`), with a line
  in its text saying so. The state still advances: a fallback note is a
  note, and the next span starts after it.
- `AGENT_HISTORY_NOTES_SUMMARIZER` can name another command with the same
  contract (JSON span on stdin, `{title, summary, decisions?, findings?,
  links?}` on stdout); its failures fall back the same way.

## Secrets in notes

**Redaction gap closed.** `redact` now catches bare 1Password service-account
tokens (`ops_` followed by 40+ token characters) and Scribe agent keys
(`scrb_agt_` followed by 16+), with no `NAME=` in front. Short names such as
the Linux user `ops_a0dc6f0c` are left alone. This applies to transcripts
too, everywhere `redact` runs.

**`notes scrub`: the one admin exception to append-only.**

```
agent-history notes scrub <note_id> --pattern <text | -> --reason "<why>"
```

Run on the hub, as the owner of the tenant's files (`ahub`); it is not an
MCP tool. It replaces every occurrence of the leaked text in that note (its
title, searchable text and stored JSON) with `[REDACTED:scrubbed]`, rebuilds
the note's full-text entry and its embedding, rewrites the note's raw copy
under `inbox/.processed/<machine>/notes/`, and appends an audit record: who
(`$SUDO_USER` or the user), when, which note, the reason, how many
replacements, which fields. **Never the secret**: not the pattern, not a
hash of it (a short secret's hash can be brute-forced). `--pattern -` reads
the text from stdin, so it stays out of `ps` and shell history.

How the trigger still blocks everything else: the `UPDATE` trigger aborts
unless the note's id is in a one-row `scrub_gate` table, which only `scrub`
fills and empties inside its own transaction, and it aborts any change to a
note's id, time, machine or source even then. `DELETE` is always refused.
The audit table is append-only the same way. (Anyone with write access to
the database file can drop triggers; they guard against code paths, not
against the file's owner.)

## Failure modes

| what fails | effect |
|---|---|
| source missing or malformed | the note is refused (CLI exit 2, MCP tool error, sweep skips and logs) |
| summarizer fails, times out or prints bad JSON | a fallback note (first prompt + last message), marked `fallback: true` |
| hub unreachable | notes stay in `notes-outbox/`, sent on the next sweep or `notes ship` |
| Ollama down on the hub | the notes batch stays in the inbox, indexed on a later run |
| the same note delivered twice | stored once (content hash is unique) |
| an older hub | notes sit unread in `notes/` until the hub is upgraded; nothing is mis-indexed |

## How a parent agent reads notes

```
history_recall(query="login redirect", notes="only")         → notes alone
history_recall(query="login redirect", notes="include")      → notes first, then transcript hits
  each note: {type: "note", title, excerpt, source, span, show: {session, around}}
history_show(session=note.show.session, around=note.show.around)   → the transcript span
agent-history notes search "login redirect"                  → the same, from a shell
```

## Not in this piece

- Duet transcripts on the hub, so a Duet note's link can open through
  `history_show`.
- Notes from Codex, OpenCode, Hermes and OpenClaw sessions: the sweep reads
  Claude Code transcripts (including `claude -p` jobs) only.
- Notes over `serve-ssh` (shell search against a remote hub): notes are
  searched through MCP on the hub or locally.
- `history_record`: it still writes chat summaries into the transcript index
  as `role: "summary"`. That is the mixing this piece avoids. Moving
  `history_record` onto notes is Dennis's call (Duet relies on it), so it is
  left untouched here.
