# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions before 0.4.1 were released during initial development and are recorded
here for context; the repository history was squashed before publication, so
they have no corresponding tags.

## [Unreleased]

### Added
- **Session notes**, kept apart from transcripts in `notes.db`: `agent-history
  notes add | search | sweep | ship | index | hook`, the MCP tool
  `history_note`, and `history_recall(notes="exclude" | "include" | "only")`
  (default `exclude`, so existing callers see no change). Every note links to
  one source (claude.ai URL, Duet channel + message id, or Claude Code session
  + machine + job id) and is refused without it; notes are append-only
  (triggers refuse UPDATE/DELETE) and redacted at write and again on the hub.
  `notes sweep` writes a note for sessions idle 5 minutes (or at session end),
  covering only what is new since the last note. The hub's index unit indexes
  `inbox/<machine>/notes/` into the notes store.

## [0.17.0] — 2026-10-15

### Added
- **Hub: one central index that every machine feeds and every session can
  search** (#25). See the README's "Hub (central history)" section and the
  examples in `deploy/`.
- **`agent-history ship`** sends redacted Records to a hub
  (`AGENT_HISTORY_HUB_INGEST`, optional `AGENT_HISTORY_HUB_KEY`). Every record
  is redacted unconditionally, ignoring the local `AGENT_HISTORY_REDACT`.
  New records go into immutable JSONL batches in an outbox and are uploaded
  with `rsync --remove-source-files`; a record is marked shipped only after
  its batch has been delivered, so a failed upload loses and duplicates
  nothing. `--dry-run` reports counts only; `--backfill-from-index` also
  exports chunks already in the local index (watermarked). Overlapping runs
  no-op under a lock, and the SessionEnd/PreCompact `trigger` ships after
  indexing when the destination is set.
- **Machine provenance**: a `machine` column on chunks (added in place, no
  re-embed; old rows count as this machine), `config.machine_id()`
  (`AGENT_HISTORY_MACHINE` or the short hostname), `Record.machine`, a
  `search --machine` filter and `--exclude-machine`, and `@machine` on results
  and in `show` when it is not the local one. `doctor` lists per-machine counts.
- **Hub mode**: `AGENT_HISTORY_INBOX` enables an inbox adapter reading
  `<inbox>/<machine>/*.jsonl` (the machine is the directory name, never a
  field in the file); consumed batches move to `.processed/` only after the
  index run commits. `AGENT_HISTORY_HUB=1` indexes only the inbox and forces
  redaction on.
- **Remote search**: `AGENT_HISTORY_REMOTE=ssh://user@host[:port]` (and
  `AGENT_HISTORY_REMOTE_KEY`). `search` merges local and hub results by
  reciprocal-rank fusion (hub only when there is no local index) and fails
  open with a one-line warning if the hub is unreachable; `show` falls back to
  the hub. `--local` / `--hub` force one side.
- **`agent-history serve-ssh`** for `command="...",restrict` in the hub's
  authorized_keys: only `search` and `show`, validated by the normal parser,
  run in-process, never through a shell.
- **Tenancy**: one index file per tenant (`inbox/<tenant>/<machine>/`,
  `tenants/<tenant>/`), `serve-ssh --tenants a,b` fixes which tenants a read
  key can open (from the forced command, never from the client), `--tenant`
  narrows within that list, multi-tenant searches merge and label results.
- **Session identity** (#28): every session gets a tool-owned, immutable
  `session_uid`, a UUIDv5 over agent, machine and source id (plus the origin
  path for memory/doc pseudo-sessions, so `memory:MEMORY` no longer collides
  across projects). New `sessions` and `session_aliases` tables hold the
  source ids as metadata and aliases. Claude Code remote-control sessions are
  linked to their `cse_…` / `session_…` ids from the transcript's
  `bridge-session` line or `~/.claude/sessions/*.json`; when two sessions
  prove to be one, the earliest stays canonical. `show`, `purge --session`
  and `store.resolve_session()` accept a uid, a raw id or any alias, and
  `search --json` / `show --json` include `session_uid`. Shipped records
  carry `session_uid` and `aliases` (optional fields, wire format still
  `v: 1`). The hub always derives the uid from the sending machine's inbox
  directory and keeps a shipped uid only when it matches, so the inbox
  directory must be named after the machine's `AGENT_HISTORY_MACHINE`.
  Forks are recorded in `sessions.meta` (`fork_of` / `forks`), not as
  aliases. `purge --session` lists every linked uid, raw id and alias it
  reaches before deleting anything.
- **Chunk provenance**: new chunks record `record_id`, `chunk_ordinal`,
  `char_start` and `char_end` (offsets into the source record), groundwork
  for scrubbing secrets that cross a chunk boundary. Existing chunks keep
  NULLs.
- **`agent-history mcp`: the hub over MCP** (#28), for claude.ai through an
  MCP router. Tools: `history_recall` (search, `k` capped at 20, 700-char
  excerpts, no origin paths), `history_show` (a session, at most 50 messages
  of 1500 chars) and `history_record` (append-only chat records: title,
  summary, decisions, findings, links, chat URL, project). Records are
  redacted and written as immutable batches into the tenant's inbox under
  the token's machine name, agent `claude-ai-chat`, so the indexer handles
  them like shipped Records. Streamable HTTP (stateless, JSON responses) on
  127.0.0.1:8765, non-loopback refused without `--allow-non-loopback`;
  `--stdio` for local use. Bearer tokens with `read` or `write` scope and
  exactly one tenant, stored as SHA-256 hashes in a `0600` file
  (`agent-history mcp token add|list|revoke`, reloaded on change) or given
  in the environment; constant-time comparison, 401 before MCP, tool list
  filtered by scope, per-token rate limits. Session ids use `session_uid`
  and `store.resolve_session` when the index has them. Optional dependency:
  `agent-history[mcp]`. Example unit: `deploy/hub/agent-history-mcp.service`;
  draft claude.ai profile instruction: `docs/claude-ai-profile-instruction.md`.

- **`agent-history import claude-ai <export-dir>`** backfills claude.ai chats
  from the official data export (#28). Chat text, conversation titles and
  summaries, extracted attachment text (capped at 100k characters each),
  project docs and instructions, memories and Claude Design chats are indexed
  under agent `claude-ai`, with the conversation uuid as session id and
  `claude.ai` / `claude.ai/project/<name>` as the location. Re-importing a
  newer export adds only new text via the existing content-hash dedup.
  `--dry-run` parses and counts without the index or Ollama. The
  conversations file is streamed, so a 180 MB export parses in ~5 s at
  ~100 MB peak memory.

### Changed
- `reindex` now also refuses (without `--force`) to wipe imported claude.ai
  chunks: no adapter re-reads an export, so the index is their only copy.
- Store rows carry the machine as their eighth column and the canonical
  session uid as their ninth, so the distance now follows at index 9
  (internal; the JSON output gains `machine` and `session_uid`).
- An existing index is upgraded in place on first open: columns and tables
  are added and uids backfilled in one pass (about 3.6 s for a synthetic
  94k-chunk index), with no re-embedding and no deletes.

### Fixed
- **Hybrid search's full-text half works on natural-language queries.** Terms
  were joined with FTS5's implicit AND, so a prose query matched only chunks
  containing every word — usually none — and hybrid search quietly fell back
  to vector-only. Full-text search now takes chunks with every term first,
  then fills up from chunks with any term (BM25-ranked, common stopwords
  skipped, no duplicates). Punctuation-only tokens such as "—" are dropped.
  Exact-identifier queries ("os error 11001") still put exact matches first.
- **Hybrid search fuses a deeper candidate pool.** Each half now contributes
  `max(k × 10, 50)` candidates instead of `k × 2`, so a chunk that is only
  middling in each list can still outrank one that tops a single list.

## [0.16.0] — 2026-10-11

### Added
- **Secret redaction at ingest, opt-in locally** (#23). With
  `AGENT_HISTORY_REDACT=on`, or under `strict` mode, recognised secrets are
  replaced with typed markers like `[REDACTED:aws_access_key]` before anything
  is chunked, hashed or embedded: private-key blocks, Anthropic, OpenAI,
  GitHub, AWS, Slack, Stripe and Google keys, JWTs, `Bearer` tokens, URL
  passwords, and `password=`/`token:`-style assignments. Matches are found on
  the whole record but applied inside the original chunk boundaries, so
  chunks without a secret keep their exact text and hash. Off by default
  because a local index is often the only place a credential can still be
  recalled; anything that leaves the machine is meant to be redacted
  regardless. `doctor` reports the setting and the last index run's
  per-class counts.
- **`agent-history scrub [--yes]`** redacts secrets already in an index:
  preview by default, then rewrites affected chunks in place (text, content
  hash, full-text row, re-embedded vector), including orphaned chunks, and
  deletes any that become duplicates. Idempotent; refuses while a `reembed`
  is staged.

## [0.15.0] — 2026-08-10

### Changed
- **Injection prefers the current directory, cheaply.** Retrieval now uses a
  two-tier distance gate keyed on the prompt's working directory: chunks from
  the current project (the directory or a sub/parent of it) pass the normal
  cutoff, while chunks from *other* projects must clear a tighter
  "near-exact" cutoff (`AGENT_HISTORY_INJECT_CROSS_DIST`, default 14.0). So
  obviously-unrelated other-project matches are dropped up front by simple
  inference — no model call — while a genuinely strong cross-project match
  still comes through. `strict` mode continues to hard-scope to the current
  directory; deliberate `search` is unaffected and stays fully cross-project.

## [0.14.0] — 2026-08-10

### Changed
- **Injected context is now project-aware.** Retrieval is similarity-only and
  spans every project on the machine, so an excerpt from an unrelated project
  can rank high on a given prompt. The injected header now names the current
  project (from the hook's cwd) and tells the agent to check each excerpt
  against the current task and disregard ones from a different project —
  making the strong model the segmentation layer the embedder can't be. A
  stopgap until real per-project scoping lands (#16 / roadmap); deliberate
  `search` stays fully cross-project.

## [0.13.0] — 2026-08-10

### Changed
- **The injection notice now lists which memories were pulled**, not just a
  count. Each recalled chunk gets a one-line entry — date, agent/role,
  project, and a short snippet — so you can see what rode into a prompt at a
  glance, passively, without the agent stopping to narrate it. Still a
  user-only `systemMessage` (never added to the model's context), still ends
  with the `/history:inject` toggle.

### Fixed
- A trigger test that compared a new file's mtime against the stamp could
  flake on coarse-granularity filesystems; it now forces the mtime
  deterministically.

## [0.12.0] — 2026-08-10

### Changed
- **Context injection is now ON by default.** Retrieval-only recall is
  local, free, and precision-gated, so it earns its place without an opt-in
  step. Disable per-machine with `agent-history inject off` (writes an
  off-marker) or `AGENT_HISTORY_INJECT=0`; `=1` forces it on. Existing
  `inject on` installs are unaffected.

### Added
- **Visible recall count.** The injection hook now emits a `systemMessage`
  — `📚 agent-history: N memories recalled · /history:inject to toggle` — so
  ambient injection is no longer invisible. It is shown to the user only,
  never added to the model's context.
- **`/history:inject` command** and `agent-history inject toggle` — flip
  injection on/off without hand-editing anything. `inject status` shows the
  current state and how to change it.

- **`agent-history purge`** — permanent, surgical deletion, the counterpart
  `reindex --force` never was. Target with `--cwd`, `--session`, `--agent`
  (AND-combined) or `--all` for total destruction; prints the count and
  requires `--yes` to act (previews otherwise). Removes chunks from the
  vector and full-text indexes too. Fills the media-sanitization gap for
  contract-end data destruction. (Dedup caveat: identical text is stored
  once under its first source, so surgical purges match stored attribution;
  `--all` is the only guaranteed-complete wipe.)
- **`agent-history strict` — a compliance profile** for regulated (e.g. CUI)
  work. In strict mode injection defaults **off**, and when explicitly
  enabled retrieves only from the current project (same cwd) — closing the
  cross-project injection path where a chunk from one client could ride into
  a prompt for another. Set via `strict on` or `AGENT_HISTORY_STRICT=1`.
  (Strict mode narrows behaviour; the index remains sensitive-at-rest and
  its storage controls still apply.)

### Fixed
- **Injection can no longer feed itself.** The indexer refuses to index any
  record that begins with this tool's own recall markers (the local hook's
  header or the fleet injectors' `AUTO-RECALLED CONTEXT`), so a persisted
  injection block can never be re-indexed and re-injected into a
  memories-about-memories loop. One choke point in the indexer covers every
  adapter.

## [0.11.0] — 2026-08-10

### Added
- **Recency-aware results.** Relevance ranking now breaks near-ties toward
  the newer chunk (scores compared in rounded buckets — deliberately mild,
  no decay curves to miscalibrate), so a superseded answer only outranks
  its own correction when relevance genuinely separates them. And both the
  CLI (`--sort newest`) and the viewer (a sort dropdown) can order results
  chronologically outright — for "what's the latest on X" rather than
  "what's the best match for X". Pure text-mode results keep exact BM25
  order under relevance sort.

## [0.10.0] — 2026-08-10

### Added
- **`agent-history reembed`** — switch embedding models **without losing
  anything**. The chunk text lives in the index, so a model change never
  needed the source transcripts: every stored chunk is re-embedded with the
  configured model, staged alongside the old vectors (searches keep working
  throughout), and swapped in atomically at the end. Interrupted by a crash
  or an Ollama outage, it resumes where it stopped; a staging run for a
  different model is discarded and restarted. It holds the indexing lock for
  the duration, so a hook firing mid-run cannot insert vectors the swap
  would discard.
- **`reindex` now refuses to destroy orphaned history.** When chunks exist
  whose source files are gone — for a mature index, most of it — a wipe
  discards the only surviving copy. `reindex` counts them, refuses, and
  points at `reembed`; `--force` overrides. It also fails closed when the
  index exists but cannot be opened: an index this Python cannot rebuild is
  one it must not delete.

### Changed
- README rewritten as one coherent document: supported sources up front,
  quickstart, full command reference, and the `index`/`reembed`/`reindex`
  triad replacing the old "reindex is destructive" guidance.

## [0.9.0] — 2026-08-10

### Added
- **Refresh-on-change for docs** — the store's first and only delete path.
  When a re-indexed document no longer contains previously indexed text,
  those chunks are removed from the chunk store, the vector index, and the
  full-text index. Deliberately narrow: only mutable sources (the docs
  adapter marks itself `mutable`) are eligible, only origins actually
  re-read this run are considered, any hash produced by *any* source this
  run survives, and a document deleted from disk keeps its chunks.
  Transcript data is structurally untouchable. Runs even when a pure
  deletion produces no new chunks.
- `agent-history docs add|remove|list` — persist documentation roots in
  the data directory instead of relying on an environment variable, which
  hook processes don't reliably inherit (Windows especially).
  `$AGENT_HISTORY_DOCS_DIRS` still works and is merged in.

## [0.8.0] — 2026-08-10

### Added
- **Docs adapter** — index arbitrary documentation trees (`*.md`,
  `*.markdown`, `*.txt`) alongside transcripts, so one search spans "what
  was said" and "what was written down": runbooks, ops notes, session
  summaries. Entirely opt-in via `AGENT_HISTORY_DOCS_DIRS`
  (os.pathsep-separated roots) — nothing is auto-detected, because guessing
  at documentation locations would mean reading directories the user never
  offered. Trees are walked with vendored/hidden directories pruned
  (`node_modules`, `.git`, dot-directories, build output), files over 2MB
  are skipped as logs rather than documents, and records carry `role="doc"`
  with the file's path as a stable identifier. This closes the gap with
  hand-rolled doc indexers (e.g. a semsearch-style ops corpus) so a single
  agent-history install can replace them.

## [0.7.0] — 2026-08-09

### Added
- **Opt-in context injection** — `agent-history inject on`. A
  UserPromptSubmit hook embeds each prompt locally, retrieves past-session
  chunks about it, and hands them to the agent as additional context — the
  "stop re-explaining the project" feature, with no LLM involved anywhere:
  the prompt is the query and the chunks are injected verbatim.
  Precision-first by design: off by default, an absolute distance cutoff
  (`AGENT_HISTORY_INJECT_MAX_DIST`, default 17.0, calibrated so on-topic
  hits at 14–16 pass and nonsense at ~20 fails), hard caps (3 chunks /
  2,200 chars), the current session's own chunks excluded, trivial and
  slash-command prompts skipped, and nothing injected when Ollama is
  unreachable — a fast silent no-op is always preferred over noisy context.
  Every failure path exits 0 so the hook can never break a prompt.
- **OpenClaw adapter** — JSONL sessions under `~/.openclaw/agents/*/sessions/`
  (format verified against a live 2026.4.26 install), skipping the
  `*.trajectory.jsonl` tool-traffic duplicates and heartbeat noise, plus
  workspace markdown (`SOUL.md`, `MEMORY.md`, the `memory/` tree). Override
  the root with `AGENT_HISTORY_OPENCLAW_DIRS`. This completes the roadmap's
  adapter list — and its immediate job: indexing an OpenClaw install's full
  history *before* a migration to another agent discards it.

## [0.6.0] — 2026-08-09

### Added
- **Three new agents.** Adapters for OpenAI Codex CLI (JSONL rollouts under
  `~/.codex/sessions/`, including the WSL Windows side), OpenCode (SQLite at
  `~/.local/share/opencode/opencode.db`), and Hermes (SQLite at
  `~/.hermes*/state.db`, one per profile). The SQLite adapters open their
  sources read-only and select named columns only — the same databases hold
  OAuth tokens and system prompts, which must never reach the index. Override
  locations with `AGENT_HISTORY_CODEX`, `AGENT_HISTORY_OPENCODE_DB`, and
  `AGENT_HISTORY_HERMES_DIRS`.
- **Hybrid search.** An FTS5 index now sits alongside the vectors, and
  `search` fuses BM25 and vector rankings (reciprocal rank fusion) by
  default. Exact identifiers — error codes, function names — that embed
  poorly now rank properly. `--mode vector` restores the old behaviour;
  `--mode text` needs no Ollama at all, and hybrid degrades to it with a
  note when the embedding endpoint is down. An existing index gains the
  FTS table automatically on its next write.
- `agent-history show <session-or-origin>` — prints the conversation a
  search hit belongs to, read from the index rather than the source
  transcript (which frequently no longer exists). `--around <timestamp>`
  centres the window on the hit.
- `agent-history serve` — a local web viewer over the same search, standard
  library only, bound strictly to 127.0.0.1 with no flag to change it.

### Fixed
- `trigger --if-changed` only stat-ed `*.jsonl` and `*.md` files, so it
  could never notice a change in a database-backed source. File roots (and
  their `-wal` sidecars) are now checked directly.

## [0.5.0] — 2026-08-07

### Added
- **Windows support.** CI now covers Python 3.10–3.13 on Windows alongside
  Linux and macOS, plus PowerShell parsing and PSScriptAnalyzer for the new
  `install.ps1` and `sync-local.ps1`.
- `agent-history trigger` — automatic indexing moved out of bash and into the
  CLI, so detaching, locking, change detection and logging behave identically
  on every platform. The plugin hooks now call it directly, which removes the
  `${CLAUDE_PLUGIN_ROOT}` script path entirely.

### Fixed
- `AGENT_HISTORY_EXTRA_DIRS` split on `:`, which tore `C:\Users\...` in half.
  It now uses the platform separator.
- `reindex` failed on Windows with `WinError 32` because the index file was
  still open. Connections are collected first, and the tables are cleared in
  place if the file is still held.
- Windows now uses `%LOCALAPPDATA%` rather than `~/.local/share`.
- A trigger that finds the lock held now gives up after 5 seconds instead of
  waiting 15 minutes. Nothing is lost — the holder stamps at the start, so the
  next `--if-changed` run picks up anything written since.

### Changed
- The index cannot be made owner-only on Windows; POSIX mode bits do not exist
  there. Documented, with an `icacls` recipe.

## [0.4.4] — 2026-08-06

### Added
- `main` is protected: changes require a pull request with green CI. A
  `ci-success` job aggregates the matrix so protection can require one stable
  check rather than job names that shift with the matrix.
- Merging a version bump to `main` now tags it and publishes a GitHub release
  automatically, using that version's CHANGELOG entry as the notes.

## [0.4.3] — 2026-08-06

### Documentation
- Identified the mechanism behind deleted transcripts: Claude Code's
  `cleanupPeriodDays` setting defaults to 30 days and sweeps session files at
  startup. Verified against real data — not one of 1,616 deleted transcripts had
  a last message younger than 30 days, while surviving files had a median age of
  7 days. The README now cites the setting rather than describing the loss as
  incidental.

### Fixed
- A Python whose `sqlite3` lacks extension support now produces a clear,
  actionable error instead of `AttributeError`, and `doctor` reports it.
  CI found this is real on macOS with uv-managed Python 3.11–3.13 (3.10 works);
  the vector tests skip on such platforms so the rest of the suite still runs.

### Added
- Test suite (67 tests) covering chunking, dedup, transcript parsing, schema and
  model identity, the loopback guard, and WSL profile disambiguation. Runs
  without Ollama — the embedder is stubbed.
- CI across Python 3.10–3.13 on Ubuntu and macOS, plus shell-script syntax
  checks, a guard against bash 4+ builtins, manifest validation, cross-manifest
  version agreement, and a check that no index or log is ever tracked.
- `scripts/sync-local.sh` — reconciles the checkout, the installed CLI, and the
  plugin, which drift independently. `--check` reports without changing
  anything and exits non-zero on drift.
- `agent-history --version`.

## [0.3.0] — 2026-08-06

### Changed
- **Breaking.** The plugin is now named `history` rather than `agent-history`,
  so its command is `/history:search` instead of
  `/agent-history:search-history`. Claude Code namespaces plugin components as
  `<plugin>:<component>` with no opt-out, so the shorter name is the only way to
  a shorter command. Reinstall with `/plugin install history@agent-history`.
- The repository, Python package, and CLI keep the `agent-history` name.

## [0.2.2] — 2026-08-06

### Added
- Architecture documentation: module map, storage schema, and the division of
  labour between Ollama (text → vectors) and sqlite-vec (the actual search).

## [0.2.1] — 2026-08-06

### Fixed
- `trigger-index.sh` used `mapfile`, a bash 4 builtin, so `--if-changed` failed
  on the bash 3.2 that ships with macOS.

### Added
- Documented the real dependency surface, including the `sqlite3`
  extension-loading requirement that some distribution and Conda builds compile
  out, and that the Ollama CLI is not needed — only the HTTP endpoint.

## [0.2.0] — 2026-08-06

### Security
- The index is created `0600` inside a `0700` directory. It previously inherited
  the default umask and was written `0644` in `0755`, readable by every local
  account — while the transcripts it copies are `0600`.
- A non-loopback `OLLAMA_URL` is now refused unless `AGENT_HISTORY_ALLOW_REMOTE=1`
  is set. Indexing sends the full text of every message to that endpoint, so the
  previous behaviour was an unguarded bulk exfiltration channel.
- WSL detection selected every profile under `/mnt/c/Users`, which on a shared
  machine pulled other people's conversations into a private index. A profile is
  now chosen only when exactly one has transcripts.

### Changed
- Documented that the index routinely contains real credentials. An audit of a
  12,819-chunk index built from ordinary use found live database URIs with
  passwords, API keys, and 32 `password=` assignments.

## [0.1.3] — 2026-08-06

### Changed
- `reindex` is documented as destructive. Agents delete old transcripts and keep
  no archive, so the index becomes the only surviving copy — 67% of a mature
  index measured here came from files no longer on disk, and `reindex` discards
  all of it.

## [0.1.2] — 2026-08-06

### Fixed
- `install.sh` printed bash redirect errors when no controlling terminal was
  present (CI, `curl | bash`). It now probes `/dev/tty` by opening it.

## [0.1.1] — 2026-08-06

### Fixed
- Line-buffer stdout. Hooks and cron redirect to a log file, which made Python
  block-buffer; a long index wrote nothing for minutes and read as a hang.

## [0.1.0] — 2026-08-06

### Added
- Initial release: local semantic search over Claude Code transcripts using
  Ollama embeddings and sqlite-vec, as a CLI and a Claude Code plugin.
- Adapter interface so further agents are one module each.
- Embedding dimension probed from the model rather than assumed, with a
  mismatch guard.
- `doctor`, `sources`, and `datadir` subcommands.
