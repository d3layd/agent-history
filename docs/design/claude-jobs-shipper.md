# Design note: `claude -p` job transcripts on the hub (piece A)

Status: draft, for review with the PR that implements it.

## Goal

Every `claude -p` job that Clamps starts as the `claude-jobs` user ships its
full transcript to the agent-history hub, so any agent up the chain can read
the job back without the job summarising itself:

- `history_recall(query="<job id>", mode="text")` finds the job;
- `history_show(session="<job id>")` reads it, oldest first.

Both are the existing Router tools (`history__history_recall`,
`history__history_show`), with their current argument shapes. Reading a job
back by job id needs no hub change: the shipper produces ordinary v1 inbox
records. Looking a job up by its **Claude session uuid** with `history_show`
needs one small hub change (a new alias kind, below), so it works after the
hub is upgraded.

Two gates, both from Dennis (Nov 1):

1. **Arrival** (`ship-final`, run by the runner as claude-jobs): exit 0 once
   every batch of the job has reached the hub.
2. **Indexing** (`confirm-indexed`, run on Clamps' side, never as
   claude-jobs): exit 0 once the hub has indexed the job, i.e. the header and
   every session are findable by job id.

## Shape: the Clamps pattern, plus a job wrapper

Clamps ships with `agent-history ship` from its own user, through an
upload-only key (`command="rrsync -wo /home/ahub/inbox/dennis/clamps"`). The
hub indexes whatever lands in `inbox/dennis/<machine>/`. This piece keeps all
of that and adds one command, `agent-history ship-jobs`, which knows the job
layout:

```
/var/lib/claude-jobs/jobs/<job_id>/job.json          job_id, ask, duet_channel_id, studio_task_key, repo, branch, status, timestamps
/var/lib/claude-jobs/jobs/<job_id>/transcript.jsonl  stream-json output of claude -p
/var/lib/claude-jobs/jobs/<job_id>/vms/<vm_id>/projects/**.jsonl   Boat VM transcripts (piece B)
~claude-jobs/.claude/projects/<slug>/<session>.jsonl                the Claude Code session file(s)
~claude-jobs/.claude/projects/<slug>/<session>/subagents/*.jsonl    subagent side chains
```

`claude-jobs` runs `ship-jobs`, not plain `ship`. Plain `ship` would send the
same session files a second time under their raw Claude session id.

## Data flow

```
claude -p ──writes──> transcript.jsonl + ~/.claude/projects/...
                │
   every 2 min  │  agent-history ship-jobs            (systemd user timer, claude-jobs)
   job end      │  /usr/local/lib/claude-jobs/ship-final <job_id>   (the runner, before "done")
                ▼
   redact ──> outbox/<job_id>-<stamp>-<seq>.jsonl ──rsync, upload-only key──> ahub:inbox/dennis/claude-jobs/
                                                                                 │
                                         agent-history-hub-index@dennis (.path + 1-min timer)
                                                                                 ▼
                                                   tenants/dennis/index.db (FTS + vectors, embedded by Ollama)
                                                                                 ▼
                                               agent-history mcp ─> Router ─> history_recall / history_show
```

Which Claude sessions belong to a job: every `session_id` that appears in the
job's `transcript.jsonl` (stream-json puts it on every line), plus any listed
in `job.json` as `session_id` or `session_ids`. Their files are found under
the projects directory by name. Session files that belong to no job are not
shipped.

## Path and header layout

**Path.** The upload key pins the hub directory `inbox/dennis/claude-jobs/`.
Each job is flushed into its own batch files, named
`<job_id>-<UTCstamp>-<pid>-<rand>-<seq>.jsonl`. The hub keeps that name when
it retires the batch to `.processed/claude-jobs/`, so the job id is in the
path at every stage. Record `origin` is the real source path, which carries
the job id for `transcript.jsonl` and VM files.

**Per-record header.** Every record is a normal v1 inbox line plus one extra
object, which current hubs ignore:

```json
{"v": 1, "machine": "claude-jobs", "agent": "claude-code",
 "origin": "/var/lib/claude-jobs/jobs/cj_20261101T120000Z_a1b2c3/transcript.jsonl",
 "session_id": "cj_20261101T120000Z_a1b2c3",
 "cwd": "/var/lib/claude-jobs/worktrees/cj_20261101T120000Z_a1b2c3",
 "timestamp": "...", "role": "assistant", "text": "...", "session_uid": "...",
 "job": {"job_id": "cj_20261101T120000Z_a1b2c3", "duet_channel_id": "...",
         "studio_task_key": "ATR-31", "kind": "job"}}
```

Three choices make read-back work on the hub that is live today:

1. **`session_id` is the job id**, used only as the hub's unique key for the
   job. The hub registers it as the session's `source` alias, so
   `history_show(session="<job id>")` resolves directly (no search step). One
   job is one conversation in the index, side chains included. A Boat VM's
   transcripts form their own conversation, `<job id>/vm/<vm id>` (piece B).
   **The real Claude Code session uuid travels with every record** (below).
2. **`cwd` contains the job id.** The runbook's parent filters recall with
   `cwd=<job id>`. When the session's real cwd (the job's worktree) already
   contains the job id it is kept; otherwise the job directory
   `/var/lib/claude-jobs/jobs/<job id>` is used. The real cwd is in the
   header text either way.
3. **A header record per job, in the text.** Full-text search reads `text`
   only, so each job gets one `role: "job"` record whose text starts
   `[claude-jobs job] <job id>` and lists the owning Duet channel, the Studio
   task key, repo, branch, status, the Claude session ids, any VM ids and the
   quoted ask. A status change produces a new header record (append-only).
   The final ship adds a `[claude-jobs final]` seal record (below).

**The Claude Code session uuid.** Every record carries the uuid of the
Claude session it came from as a structured field, `claude_session_id`, and
as a link alias `("claude", <uuid>)`. Header and seal records carry
`claude_session_ids` (all of the job's sessions) and one alias per uuid. The
uuids come from the `session_id` on each `transcript.jsonl` line, the session
file names, and `job.json`'s `session_id` / `claude_session_ids`.

- `history_recall("<uuid>", mode="text")` finds the job on any hub: the
  header text lists the uuids.
- `history_show(session="<uuid>")` resolves on a hub that knows the alias
  kind `claude` (this PR adds it to `sessions.ALIAS_KINDS`). An older hub
  drops the alias it doesn't know, so this one lookup needs the **hub
  upgrade** (Dennis's steps). `claude` is a pointer, not proof that two
  sessions are one: it never merges sessions, and the first session to claim
  a uuid keeps it.
- `history_show` and `history_recall` results include `claude_session_ids`
  for the session when the hub knows them.

`transcript.jsonl` lines carry no timestamps. Their records get a stable
synthetic one: the job's start time (from `job.json`, else the stamp inside
the job id) plus the line number in milliseconds. The `result` line gets the
job's finish time, or the last session-file timestamp. A stable timestamp
keeps the record hash stable, so nothing ships twice. Text that appears both
in the session file and in `transcript.jsonl` is stored once on the hub: the
index de-duplicates chunks by content hash, and session-file records are
queued first, so their real timestamps win.

## Redaction

The same as every shipper: `redact.redact()` runs on every record's text
unconditionally, whatever the local `AGENT_HISTORY_REDACT` says, before the
batch file is written. Nothing unredacted is written to the outbox or sent.
This covers the quoted ask and the header fields too, because they are record
text. The values in the `job` object are redacted the same way. The known gap
from the hub note still applies: bare `ops_…` and `scrb_agt_…` tokens
without a `NAME=value` context are not caught by `redact` today.

## The final ship: `ship-final <job_id>`

The runner calls `/usr/local/lib/claude-jobs/ship-final <job_id>` before it
marks a job done. That wrapper runs `agent-history ship-jobs --final
<job_id>`. Exit codes:

| exit | meaning | runner should |
|---|---|---|
| 0 | the hub confirmed it holds every record of the job, seal included | mark done |
| 1 | upload failed or a record is unconfirmed; batches stay queued | retry later, don't mark done |
| 2 | bad job id, missing job dir or `job.json`, or no hub configured | fix config |
| 3 | another ship held the lock past `--wait` (default 300 s) | retry |

What "the hub confirmed" means, exactly: the batch files go up with
`rsync --remove-source-files`. rsync deletes a source file only after the
receiving side (the hub, through `rrsync -wo`) has written it and its
whole-file checksum matched. So a batch that has left the outbox is a batch
the hub acknowledged byte for byte. `ship-final` then recomputes every record
hash of the job from the sources as they stand and requires all of them,
plus the seal's, to be in the shipped set. Exit 0 needs all three: no upload
error, nothing of this job left in the outbox, every hash shipped.

This is the arrival gate. The key is upload-only, so the job user cannot
read the hub back; indexing follows within about a minute (the `.path` unit
fires on arrival) and is confirmed separately by `confirm-indexed`. The seal
record makes completeness checkable on the hub side: it lists every
conversation of the job (`Sessions:`) and every Claude session uuid
(`Claude sessions:`), and states the record count, a digest of the source
files, and whether `transcript.jsonl` ended with a `result` line.

## The index gate: `confirm-indexed --job <job_id>`

```
agent-history confirm-indexed --job <job_id> [--json] [--receipt FILE]
  0  indexed: the header, the seal and every session are findable by job id
  1  not yet, or partial (the JSON and the last line say what is missing)
  2  config or credential error (no hub configured, key refused, tenant refused)
```

It runs where the runner's index confirmer runs: inside clamps-bridge, as
Clamps (`ops_a0dc6f0c`), once a minute for up to 15 minutes after
`ship-final`. **Never as claude-jobs**: jobs run arbitrary code in auto mode,
and a read credential there would expose all of Dennis's history to every
job. claude-jobs keeps only its upload-only key.

**Read path: Clamps' existing read-only ssh key.** Clamps already reads the
hub through `AGENT_HISTORY_REMOTE` and a key pinned in `authorized_keys` to
`command="… agent-history serve-ssh --tenants dennis",restrict`. `serve-ssh`
answers only `search` and `show`, runs them in-process (never a shell), and
the tenant list is fixed by the forced command, so the client cannot widen
it. Why this one:

- It exists and is in daily use (Clamps' `ah-search`), so there is no new
  secret, key, token or listener, and nothing new on claude-jobs.
- It runs the same functions the MCP tools run (`search.search_rows` for
  `history_recall`, `show.rows_for` for `history_show`), so "findable with
  history_recall / history_show" is tested literally, against the tenant's
  own index.
- The alternative, a bearer token for the hub's MCP server in Clamps' env,
  would be a second read credential with the same reach, and a new secret to
  rotate. A forced command that only answers index status would be narrower,
  but Clamps already holds full read access through this key, so a narrower
  key would protect nothing.

With no `AGENT_HISTORY_REMOTE`, `confirm-indexed` reads the local index
(used on the hub itself as `ahub`, and by the tests).

**What it checks**, all by job id:

| check | how |
|---|---|
| header | `search <job id> --mode text --cwd <job id>` returns `[claude-jobs job] <job id>` |
| seal | the same search returns `[claude-jobs final] <job id>` (the final batch is indexed) |
| job session | `show <job id>` returns rows filed under the job id |
| every VM session in the latest seal | `show <job id>/vm/<vm id>` returns rows, and the VM header is found |
| every Claude uuid in the seal | `search <uuid> --mode text` returns a hit in the job (reported, and gated) |
| uuid lookup by `show <uuid>` | reported as `claude_lookup`, not gated, because it needs the hub upgrade |

The latest seal (newest timestamp) defines "every session", so a VM exported
after the first final ship is included once its own seal is indexed.

`--json` prints `{"job_id", "state": "indexed" | "pending" | "partial" |
"error", "checked_at", "sessions", "claude_session_ids", "checks",
"missing", "claude_lookup"}`. `--receipt FILE` also writes that JSON
atomically, for piece B's deletion gate.

`ship-final` waits for the lock (a timer run may be mid-upload) instead of
skipping like the timer does, and retries an upload twice with a short
back-off, because sshd `MaxStartups` throttling has dropped uploads during
scanner floods before.

## Failure modes

| what fails | effect | recovery |
|---|---|---|
| hub unreachable, sshd throttled | batches stay in the outbox, hashes stay `queued` | next timer run resends; `ship-final` exits 1 |
| crash between writing a batch and queueing it | the batch is resent | hub de-duplicates by content hash |
| crash after upload, before settle | next run sees the batch gone and settles it | none needed |
| `transcript.jsonl` has no `result` line | shipped as is; seal says `result: missing` | runner decides the job status |
| session file not found (other `CLAUDE_CONFIG_DIR`) | `transcript.jsonl` still carries every message | set `AGENT_HISTORY_PROJECTS` |
| malformed `job.json` or job id that doesn't match its directory | that job is skipped with a warning; `--final` exits 2 | fix the runner |
| timer and `ship-final` overlap | `ship-final` waits for the lock | none needed |
| old job dirs pile up | each run re-reads them; already-shipped hashes are skipped | prune job dirs after the hub has them |
| arrived but not indexed (Ollama down, indexer stuck) | `confirm-indexed` exits 1 | the runner retries for 15 min, then alerts on Telegram |
| Clamps' read key refused or no hub configured | `confirm-indexed` exits 2 | the runner alerts at once |

## How a parent agent reads a job back

```
history_recall(query="cj_20261101T120000Z_a1b2c3", mode="text",
               cwd="cj_20261101T120000Z_a1b2c3", k=5)
  → hits with session_id = the job id, machine = claude-jobs
history_show(session="cj_20261101T120000Z_a1b2c3", limit=50)
  → the job's conversation, oldest first (the last 50 messages; pass
    around=<timestamp> to page)
history_show(session="cj_20261101T120000Z_a1b2c3/vm/bx_abc123")
  → one Boat VM's conversation (piece B)
```

```
history_recall(query="5f2b8c1e-…", mode="text")      → the job, by its Claude session uuid
history_show(session="5f2b8c1e-…")                   → the same job (after the hub upgrade)
```

`machine="claude-jobs"` narrows any recall to job transcripts. Don't use
`sort="newest"` to find the latest job: it only re-sorts the top relevance
hits.

## Deployment (Dennis's steps; nothing here touches the live hub)

1. Hub: `mkdir -m 700 /home/ahub/inbox/dennis/claude-jobs` (as ahub), add the
   upload-only key line to `~ahub/.ssh/authorized_keys`, and add
   `PathChanged=/home/ahub/inbox/dennis/claude-jobs` to the
   `agent-history-hub-index@dennis.path` drop-in.
2. claude-jobs: install agent-history (`uv tool install`) from this branch
   (once merged, from the release), create the ingest key, write
   `~/.config/agent-history/env`, install and enable the two user units in
   `deploy/claude-jobs/`.
3. Root: install `deploy/claude-jobs/ship-final` as
   `/usr/local/lib/claude-jobs/ship-final` (root-owned, 0755).
4. Clamps (`ops_a0dc6f0c`): upgrade its agent-history to a version with
   `confirm-indexed`; the hub settings it already uses for `ah-search`
   (`AGENT_HISTORY_REMOTE`, `AGENT_HISTORY_REMOTE_KEY`) are all it needs.
5. Hub (for uuid lookup with `history_show` only): upgrade agent-history so
   the indexer knows the `claude` alias kind.

The exact commands are in `deploy/claude-jobs/README.md`.
