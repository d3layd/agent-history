# Design note: history_record writes notes, not transcript chunks (piece D)

Status: draft, for review with the PR that implements it. Stacked on piece C
(session notes); it needs C's notes store.

## Goal

`history_record` saves a chat summary today by writing it into the hub's
**transcript** index as `agent: claude-ai-chat`, `role: summary`. A summary
is not a transcription, so it should live in the notes store (piece C). This
must not break Duet, which relies on these records.

## How Duet uses history_record today (what I could establish)

Duet has no code repository: it is Anthropic's hosted agent, driven by its
instructions and the MCP Router connectors. I found no Duet code under
`~/repos`; the only callers of `history_record` in code are this repo's MCP
server and tests. The evidence:

- **Connectors.** Duet's briefing (Oct 29, Claude Code session `37b522cb`)
  lists `history` and `history-rec` among its Router connectors. Duet has no
  Router client of its own; it rides the `claude-ai` operator token (memory
  note `reference_claude_duet`). So every `history_record` from Duet lands in
  `inbox/dennis/claude-ai/` and is indistinguishable from an ordinary
  claude.ai chat's record.
- **Writes.** Duet asks Claude Code to "record finished work with
  `history-rec__history_record` AND in the Studio task/note, so Duet doesn't
  report it as open" (same briefing). The claude.ai profile instruction
  (`docs/claude-ai-profile-instruction.md`) tells chats to call it at
  milestones and wrap-up with `title`, `summary`, `decisions`, `findings`,
  `links`, `chat_url`, `project`.
- **Reads.** Duet "reads Claude Code history via the history connector before
  reporting status", and its nightly synthesis "reads every channel, mail,
  the apps and Claude Code history" to report what moved and what has no
  record. That is `history_recall` (the profile instruction suggests
  `surface=claude-ai-chat` to narrow to records) and `history_show` on a
  hit's session (`claude-ai-chat-<chat uuid or record id>`).
- **What is on the hub** (read-only search from MSI, Nov 1): 3 record
  sessions, all from machine `claude-ai`, dated Oct 29–30, none with a
  `chat_url` (session ids are `claude-ai-chat-rec-…`). So callers often omit
  the link, and a record without one must keep working.
- **Not established:** Duet's exact queries. The Router's audit log records
  client, server and tool name per call (not arguments), and Duet shares the
  `claude-ai` client, so even that log cannot separate Duet from claude.ai
  chats. Reading it is a fleet action (Dennis's step, below).

So the compatibility target is: **the same tool, the same arguments, and the
same reads still finding records**: `history_recall` with default arguments
or `surface`/`agent` = `claude-ai-chat`, and `history_show` on the record's
session.

## Design

```
history_record(title, summary, decisions, findings, links, chat_url, project
               [, duet_channel_id, duet_message_id, session_id, session_machine, job_id])
   └─> a note (origin "history_record") in inbox/<tenant>/<machine>/notes/   (no longer the transcript inbox)
          └─> hub index unit → notes.db
reads
   history_recall (default notes="exclude")  → transcript hits + record-notes, records in the old shape
   history_recall(surface/agent="claude-ai-chat") → record-notes only (the old filter keeps working)
   history_show(session="claude-ai-chat-…")   → the transcript index first, then the record-notes of that session
```

**Write.** `history_record` keeps its name, its arguments and its answer
(`record_id`, `session`, `timestamp`, `machine`, `status`, `redacted`). It
gains optional, additive arguments for a real source link:
`duet_channel_id` + `duet_message_id` (Duet: the message that starts the
span), or `session_id` + `session_machine` (+ `job_id`). It writes a note
whose source is, in order: the claude.ai URL, the Duet ids, the Claude Code
session, or, when the caller gave none (today's common case), a `record`
source: `{"kind": "record", "client": <token machine>, "session":
"claude-ai-chat-<…>"}`. A `record` source resolves through
`history_show(session=…)`, which this piece serves from the notes store, so
it still satisfies "a note without a resolvable source is rejected". It is
accepted only from `history_record` and the migration, never from
`history_note` or `notes add`. The note keeps the record's exact text
(`[claude.ai chat record] …`), its record id and its project.

**Read compatibility.**

- `history_recall` with `notes="exclude"` (the default) adds matching
  record-notes to the transcript hits, in the shape records had before
  (`agent: claude-ai-chat`, `role: summary`, `session` and `session_id` =
  `claude-ai-chat-…`, `machine`, `project`, `excerpt` = the same text). Hits
  are interleaved by rank. `agent`/`surface` = `claude-ai-chat` returns only
  records; another agent filter returns none; `cwd` matches the record's
  project; `machine` its machine. Session notes still need `notes="include"`
  or `"only"`, where records also show up as notes.
- `history_show(session="claude-ai-chat-…")` returns the record trail of
  that chat from the notes store when the transcript index has nothing for
  it (after the migration, always), as `role: summary` messages.

**Hub-side diversion.** The inbox reader no longer indexes legacy record
lines (`agent: claude-ai-chat`, `role: summary`, origin
`mcp:claude-ai-chat/rec-…`) as transcripts. It converts each into a note in
the same machine's `notes/` directory, which the notes indexer picks up in
the same run. An MCP server not yet upgraded, a re-delivered batch, or a
`reindex` from `.processed/` therefore never puts records back into the
transcript index. The note id is derived from the record id, so a record is
stored once however many times it arrives.

**Migration: `agent-history notes migrate-records`.** Run on the hub, per
tenant, as `ahub`. Dry run by default: it lists every record found in the
transcript index (chunks with `agent = claude-ai-chat`, `role = summary`,
origin `mcp:claude-ai-chat/…`), stitches each record's chunks back into its
text (chunks overlap by 200 characters), and says what it would move.
`--yes` then, per record: writes the note (idempotent), checks it can be read
back from the notes store, appends the record's chunks to a rollback file
(`<tenant>/migrate-records-<stamp>.jsonl`, already redacted, 0600), and only
then deletes those chunks from the transcript index (chunks, vectors, FTS).

## Redaction

As for every note: `redact` runs on every field before the note is written,
and again when the hub indexes it. Migrated records were redacted when they
were first written; they are redacted again on the way into notes.

## Failure modes

| what fails | effect |
|---|---|
| hub MCP server not upgraded yet | `history_record` writes the old line; the upgraded indexer diverts it to notes; before that upgrade, records keep landing in the transcript index as today |
| indexer upgraded, MCP server not | reads miss migrated records until the MCP server is upgraded too, so upgrade both before migrating |
| migration interrupted | a record's chunks are deleted only after its note is written and read back; rerunning finishes the rest and skips notes already there |
| Ollama down during migration | notes are written without vectors (text search works); chunks are still removed |
| a record whose chunks don't stitch cleanly | migrated with the chunks joined in order, flagged in the dry run |

## Dennis's steps

1. Upgrade the hub's agent-history (indexer and MCP server) to a version with
   C and D, restart `agent-history-mcp`, refresh the Router. Approval needed.
2. Dry run, then migrate, per tenant:
   `sudo -u ahub env AGENT_HISTORY_HOME=/home/ahub/tenants/dennis /home/ahub/.local/bin/agent-history notes migrate-records`
   then the same with `--yes`.
3. Optional: in the claude.ai profile instruction and Duet's roster, ask
   Duet to pass `duet_channel_id` and `duet_message_id` to `history_record`,
   so its records link back to the channel.
4. Optional evidence of real usage: count `history_*` calls by client in the
   Router audit log (`audit.target` in the Router config).

## No Duet PR

Duet has no repository to change: its behaviour comes from its instructions
and the Router. The optional argument change is an instruction for Dennis to
give Duet (step 3), and the updated profile instruction is in this PR.
