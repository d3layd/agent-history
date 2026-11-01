# Design note: `claude -p` job transcripts on the hub (piece A)

Status: draft, for review with the PR that implements it.

## Goal

Every `claude -p` job that Clamps starts as the `claude-jobs` user ships its
full transcript to the agent-history hub, so any agent up the chain can read
the job back without the job summarising itself:

- `history_recall(query="<job id>", mode="text")` finds the job;
- `history_show(session="<job id>")` reads it, oldest first.

Both are the existing Router tools (`history__history_recall`,
`history__history_show`), with their current argument shapes. **The hub needs
no code change and no redeploy for this piece**: the shipper produces ordinary
v1 inbox records.

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

1. **`session_id` is the job id.** The hub registers it as the session's
   `source` alias, so `history_show(session="<job id>")` resolves directly
   (no search step). The Claude session id is not lost: it is in the header
   record's text, so `history_recall("<claude session id>", mode="text")`
   finds the job too. One job is one conversation in the index, side chains
   included. A Boat VM's transcripts form their own conversation,
   `<job id>/vm/<vm id>` (piece B).
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

This is a transport confirmation, not an index confirmation. The key is
upload-only, so the job user cannot read the hub back; indexing follows
within about a minute (the `.path` unit fires on arrival). The seal record
makes completeness checkable on the hub side: it states the record count and
a digest of the source files, and whether `transcript.jsonl` ended with a
`result` line.

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

The exact commands are in `deploy/claude-jobs/README.md`.
