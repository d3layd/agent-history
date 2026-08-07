# agent-history

[![CI](https://github.com/d3layd/agent-history/actions/workflows/ci.yml/badge.svg)](https://github.com/d3layd/agent-history/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Local semantic search over your AI coding agent session transcripts.

You had a long conversation three weeks ago where you worked out why the auth
flow had to be structured a particular way. You remember deciding it. You do not
remember which project, which day, or what the reasoning was. `grep` is no help,
because you don't remember the words you used.

`agent-history` embeds every message from your past sessions into a local vector
index, so you can ask for it by meaning instead of by keyword:

```console
$ agent-history search "why did we split the auth middleware"
#1  dist=12.70  2026-06-14T22:03  claude-code/assistant  ~/repos/example
    file: /home/you/.claude/projects/-home-you-repos-example/a1b2c3.jsonl
    The reason to split it is that the session refresh needs to run before...
```

Everything runs on your machine — embeddings come from your own Ollama instance.
No API keys, no per-query cost, and nothing is sent off-box (the tool refuses a
non-loopback endpoint unless you explicitly allow it).

## Requirements

- **Python 3.10+**, with a `sqlite3` module that permits extension loading (see
  below)
- **[Ollama](https://ollama.com) reachable over HTTP**, with an embedding model
  pulled
- **[`uv`](https://docs.astral.sh/uv/)** to install

```console
$ ollama pull nomic-embed-text
```

The Python side is deliberately small — two direct dependencies, six once
resolved:

| | |
|---|---|
| direct | `sqlite-vec`, `requests` |
| transitive | `certifi`, `charset-normalizer`, `idna`, `urllib3` |

No vector database, no ML framework, no ORM. The index is a single SQLite file.

**Two things that are easy to get wrong:**

*Extension loading.* Vectors live in the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension, so the `sqlite3`
module must allow loading it. Check with:

```console
$ python3 -c "import sqlite3; sqlite3.connect(':memory:').enable_load_extension(True)"
```

If that raises `AttributeError`, that Python was built without extension
support and the tool cannot work under it. `agent-history doctor` reports this
explicitly rather than failing obscurely.

**Known: this affects macOS.** CI shows uv-managed CPython on macOS lacks
extension support on **Python 3.11, 3.12 and 3.13**, while **3.10 works**. It is
a property of the Python build, not of macOS itself — Homebrew's `python3`
generally works. On macOS either install under 3.10:

```console
$ uv tool install --python 3.10 git+https://github.com/d3layd/agent-history
```

or use a Python where the check above succeeds. The same limitation appears in
some Conda and Linux distribution builds.

*The Ollama CLI is not required.* Only the HTTP endpoint is. Ollama can run on
another OS entirely — the setup this was built on has Ollama native on Windows
with the tool running in WSL, reaching it over forwarded localhost, and no
`ollama` binary present at all. `install.sh` will offer to `ollama pull` the
model when the CLI exists, and tell you to pull it yourself when it doesn't.

**Shell tooling** (only for the auto-indexing scripts, not the CLI): `bash`,
`find`, `date`, `touch`, `mkdir`. `flock` and `setsid` are used when present and
degraded around when absent, so macOS works. `curl` is needed by `install.sh`,
and `pgrep` by the optional cron line. The scripts avoid bash 4 builtins, so the
bash 3.2 that ships with macOS is fine.

## Install

```console
$ uv tool install git+https://github.com/d3layd/agent-history
$ agent-history index
```

The first index of a large history takes a while — it embeds every message once.
Subsequent runs only handle what's new, and take seconds.

Or clone and run the installer, which also checks Ollama, pulls the model if it
is missing, and offers to wire up automatic indexing:

```console
$ git clone https://github.com/d3layd/agent-history
$ cd agent-history && ./scripts/install.sh
```

### As a Claude Code plugin

```
/plugin marketplace add d3layd/agent-history
/plugin install history@agent-history
```

This adds a **`/history:search`** command that runs the search and *synthesizes
an answer* from the results, rather than making you read raw JSONL:

```
/history:search why did we split the auth middleware
```

It also installs SessionEnd and PreCompact hooks that keep the index current
automatically.

The plugin is named `history` rather than `agent-history` purely for ergonomics:
Claude Code namespaces every plugin component as `<plugin>:<component>`, with no
way to opt out, so a long plugin name makes for a long command. The repository
and the CLI keep the full `agent-history` name.

## Usage

```console
$ agent-history search "query"              # top 8 matches
$ agent-history search "query" -k 20        # more results
$ agent-history search "query" --full       # full chunk text, not a preview
$ agent-history search "query" --cwd myapp  # only sessions from a directory
$ agent-history search "query" --json       # machine-readable
$ agent-history "query"                     # `search` is the default

$ agent-history index      # incremental update
$ agent-history reindex    # wipe and rebuild
$ agent-history sources    # show which directories are scanned
$ agent-history doctor     # diagnose the installation
```

Start with `doctor` if anything misbehaves — it reports whether Ollama is
reachable, whether the model is pulled, what was detected, and how much is
indexed.

## `reindex` is destructive

**Claude Code deletes your old transcripts by default.** The
[`cleanupPeriodDays`](https://code.claude.com/docs/en/settings) setting defaults
to **30 days**, and the sweep runs at startup:

> "Claude Code deletes session files and other application data older than this
> period at startup."

There is no archive directory. Once a transcript is swept, **your index is the
only surviving copy of that conversation**.

This is why the tool is worth more than it first appears: the index outlives the
transcripts and keeps answering questions about work whose source files are
gone. On the machine this was built for, **67% of a mature index — 25,727 chunks
from 1,616 files — came from transcripts that no longer existed.** The single
best answer to "why does SessionEnd not always fire" lived in a session whose
file had been swept months earlier.

The mechanism is measurable. Comparing every indexed transcript against what is
still on disk:

| | files | median age of last message |
|---|---|---|
| still on disk | 250 | 7 days |
| **deleted** | **1,616** | **51 days** |

**Not one deleted transcript was younger than 30 days.** That hard floor is the
retention policy, not chance. Unless you have raised `cleanupPeriodDays`, the
same is happening to you right now.

`reindex` wipes the index and rebuilds it from what is on disk *today*, so it
permanently discards all of that. Use `index` (incremental, additive, the
default) for normal operation. Reach for `reindex` only when you are changing
embedding model, and only if you accept losing history whose transcripts are
gone.

## How it works

```
transcripts ──▶ adapter ──▶ filter ──▶ chunk ──▶ embed ──▶ sqlite-vec
 (JSONL/SQLite)   normalise   drop noise  ~1500ch   Ollama    + SHA-256 dedup
```

**1. Adapters normalise.** Each agent stores transcripts differently — some as
append-only JSONL, some in SQLite. An adapter turns whichever it is into a
common `Record` (agent, origin, session, cwd, timestamp, role, text). Nothing
downstream knows which agent it came from.

**2. Filtering does most of the work.** The overwhelming majority of a
transcript is tool traffic — file reads, diffs, command output — which is bulky
and semantically useless to search. Measured across a real 293 MB corpus:

| stage | count |
|---|---|
| raw JSONL lines | 87,627 |
| user/assistant messages | 63,341 |
| dropped: tool calls and results only | 54,506 |
| dropped: harness noise (`<system-reminder>`, `<local-command>`, …) | 321 |
| dropped: under 20 characters | 109 |
| **kept as records** | **9,195** |

That is **11.0 MB of actual conversation out of 293 MB on disk — 3.8%**. The
index is small because 96% of what agents write down is not conversation.

**3. Chunking.** Messages longer than 1500 characters are split with 200
characters of overlap, so a passage spanning a boundary is still findable. Runs
of 300+ base64-ish characters are replaced with `[blob]` first — embedding an
encoded payload costs time and matches nothing. 9,195 records became 12,819
chunks.

**4. Embedding and storage.** Each chunk goes to Ollama and lands in SQLite with
the [`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension: a `chunks`
table for text and metadata, a `vec_chunks` virtual table for the vectors, and a
`meta` table recording which model built the index.

**5. Dedup makes re-runs cheap.** Every chunk is keyed by the SHA-256 of its
text. Re-indexing skips anything already embedded, so the expensive step only
ever runs on genuinely new content. This works identically for file-backed and
database-backed adapters — no per-source watermark bookkeeping.

Claude Code project memory files (`~/.claude/projects/*/memory/*.md`) are indexed
alongside transcripts. They are 3% of chunks but hand-written and durable, and
are often the single most useful thing a search surfaces.

### What ends up in the index

| | |
|---|---|
| assistant messages | 9,596 (75%) |
| user messages | 2,873 (22%) |
| memory files | 350 (3%) |

## What it costs

Measured on the corpus above — 276 transcript files, 293 MB, 12,819 chunks:

| | |
|---|---|
| First full index | **13 minutes** (~16 chunks/s, CPU-bound on Ollama) |
| Incremental run, nothing new | **0.4 s** — no Ollama call at all |
| Incremental run, one new session | **~27 s** for 399 chunks |
| Search, warm | **~0.2 s** |
| Search, first after boot | ~2.9 s (Ollama loading the model) |
| Index on disk | **60 MB** — 39 MB vectors, 9 MB text, 4.6 KB/chunk |
| Hook overhead at session end | **5 ms** (it detaches immediately) |
| Plugin context cost | ~37 tokens always-on |

The vectors dominate the footprint: 768 float32 values is 3 KB per chunk no
matter how short the text.

## Architecture

The whole tool is about 1,200 lines. The dependency graph is acyclic and
shallow — `config` and `textproc` import nothing internal, everything else
depends downward, and `cli` is the only module aware of all the others.

```
        ┌─────────────────────────────────────────────────────────┐
 ENTRY  │  cli.py (140)   index │ reindex │ search │ sources      │
        │                 doctor │ datadir                        │
        └───────┬──────────────────────────────┬──────────────────┘
                │                              │
        ┌───────▼────────┐            ┌────────▼────────┐
 FLOWS  │  index.py (99) │            │ search.py (65)  │
        │  collect→dedup │            │ embed q→kNN     │
        │  →embed→store  │            │ →filter→print   │
        └───┬────────┬───┘            └────┬───────┬────┘
            │        │                     │       │
    ┌───────▼───┐    │                     │       │
 IN │ adapters/ │    │                     │       │
    │  (51+148) │    │                     │       │
    │ Record ×N │    │                     │       │
    └───────────┘    │                     │       │
                ┌────▼─────────────────────▼──┐ ┌──▼──────────────┐
 OUT            │      store.py (194)         │ │  embed.py (102) │
                │  schema, dedup, kNN, meta   │ │  Ollama client  │
                └──────────────┬──────────────┘ └──────┬──────────┘
                               │                       │
                        SQLite + sqlite-vec      HTTP :11434
                               │                       │
        ┌──────────────────────▼───────────────────────▼──────────┐
 BASE   │  config.py (166) — every path and env var, imports      │
        │  nothing.  textproc.py (32) — chunk + scrub, pure.      │
        └─────────────────────────────────────────────────────────┘
```

**Adapters** are the extensibility seam. One answers three questions —
`available()`, `roots()`, `records()` — and yields `Record(agent, origin,
session_id, cwd, timestamp, role, text)`. Nothing downstream knows which agent
produced a record, which is why `origin` is a string: it holds a file path
today and `db:<path>#<table>` for the SQLite-backed agents on the roadmap.

**`config.py`** is the single source of truth for location. No other module
contains a hardcoded path. It also owns the security posture — `0700` creation,
the loopback policy, WSL profile disambiguation.

**`embed.py`** is the only code that touches the network.

### Storage

```sql
chunks(id, agent, origin, session_id, cwd, timestamp, role, text,
       content_hash UNIQUE)
vec_chunks USING vec0(embedding float[768])   -- rowid ⟷ chunks.id
meta(key, value)                              -- model, dimension
```

Three decisions carry most of the weight:

- **`content_hash UNIQUE` is the entire incremental strategy.** No watermarks,
  no mtime tracking, no per-source bookkeeping — and it behaves identically for
  append-only JSONL and for SQLite sources.
- **`meta` makes the dimension dynamic** rather than a hardcoded 768, and
  doubles as the model-mismatch detector.
- **`vec_chunks` joins by rowid**, keeping the vector table a pure vector store.
  `sqlite-vec` expands it into four shadow tables underneath.

### Who does what

A common misreading is that Ollama performs the search. It does not — it never
sees the index. Ollama converts text to vectors, and that is all: once per chunk
at index time, once per query at search time. The nearest-neighbour search runs
inside SQLite, in-process, with no network involved.

| | |
|---|---|
| Ollama — text → 768 floats | 94 ms |
| sqlite-vec — kNN over 12,819 vectors | **27 ms** |

`sqlite-vec` is a real vector index, not a BLOB column scanned in Python:
`vec_chunks` is a virtual table with its own packed storage format, and
`MATCH ? AND k = 8` is a kNN operator implemented in C. The accurate description
is an embedded vector database living inside a SQLite file — the same way SQLite
is a real database that happens to be a file rather than a server.

**The limitation that follows from this:** `sqlite-vec` does *exhaustive* kNN.
There is no HNSW or IVF approximate index, so search cost is linear in corpus
size — the query plan says `SCAN`, not `SEARCH`. At 12,819 vectors that is 27 ms
and irrelevant. At a few hundred thousand it becomes noticeable; at millions you
would want a different store. For a personal transcript index the ceiling is
remote, and in exchange there is no daemon, no server, and no separate process
to keep alive.

## Automatic indexing

The plugin installs two hooks:

- **SessionEnd** — index when a session finishes
- **PreCompact** — index before context is compacted, so nothing is lost

One honest caveat: `SessionEnd` only fires on a *clean* exit. Kill the terminal
and that session is not indexed until something else triggers a run. Gaps are
expected, not a bug. For a safety net, add a cron entry that indexes only when a
session is active and something actually changed:

```cron
0 * * * * pgrep -x claude >/dev/null && ~/.../scripts/trigger-index.sh cron --if-changed
```

`--if-changed` compares your transcripts against a stamp file and exits before
waking Ollama when there is nothing to do, so idle hours cost nothing.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_HISTORY_HOME` | `$XDG_DATA_HOME/agent-history` | Where the index lives |
| `AGENT_HISTORY_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `AGENT_HISTORY_PROJECTS` | `~/.claude/projects` | Claude Code transcript root |
| `AGENT_HISTORY_EXTRA_DIRS` | – | Extra transcript roots, colon-separated |
| `AGENT_HISTORY_NO_WSL` | – | Set to `1` to skip Windows-side transcripts |
| `AGENT_HISTORY_ALLOW_REMOTE` | – | Set to `1` to permit a non-loopback `OLLAMA_URL` |

Switching `AGENT_HISTORY_MODEL` requires a `reindex` — vectors from different
models are not comparable. The tool detects the mismatch and tells you, rather
than silently returning nonsense.

### WSL

When running inside WSL, transcripts written by Claude Code on the Windows host
are picked up automatically, so one index covers both sides. WSL cannot tell
which Windows account is yours, so a profile is selected only when exactly one
under `/mnt/c/Users` has transcripts; if several do, none is chosen and you are
told to name one via `AGENT_HISTORY_EXTRA_DIRS`. Set `AGENT_HISTORY_NO_WSL=1` to
opt out entirely.

## Privacy and security

**The index is a credential store.** It holds the full text of every indexed
message, and conversations with a coding agent routinely contain secrets you
pasted or that a tool printed. Auditing a real 12,819-chunk index built from
ordinary daily use turned up live database URIs with passwords, API keys, and
32 distinct `password=` assignments. Assume yours is the same.

Consequences worth internalising:

- `index.db` deserves the same handling as `~/.ssh`. It is created `0600` inside
  a `0700` directory — deliberately, since the default umask would otherwise
  make it world-readable on a shared machine.
- Never commit it. `.gitignore` excludes `*.db` and `*.log`.
- Think twice before putting `AGENT_HISTORY_HOME` in a synced, backed-up, or
  cloud-mounted directory.
- There is **no encryption at rest**. Full-disk encryption is the answer.

**Nothing is sent off your machine by default.** Embeddings come from your own
Ollama instance. Because that endpoint is configurable, and pointing it
elsewhere would stream your entire history to a third party, a non-loopback
`OLLAMA_URL` is refused unless you set `AGENT_HISTORY_ALLOW_REMOTE=1`:

```console
$ OLLAMA_URL=http://someone-elses-box:11434 agent-history index
error: OLLAMA_URL points at a remote host (http://someone-elses-box:11434).
Indexing sends the full text of your conversations there. If that is genuinely
intended, set AGENT_HISTORY_ALLOW_REMOTE=1 to confirm.
```

**Shared machines.** On WSL, a Windows profile is auto-detected only when
exactly one has transcripts. If several do, none is indexed — pulling a
colleague's or family member's conversations into your index would be worse than
indexing nothing. Name the one you want with `AGENT_HISTORY_EXTRA_DIRS`.

## Roadmap

Support for more agents, on the existing adapter interface:

- [ ] OpenAI Codex CLI — JSONL rollouts under `~/.codex/sessions/`
- [ ] OpenCode — SQLite at `~/.local/share/opencode/opencode.db`
- [ ] Hermes — SQLite at `~/.hermes-*/state.db`
- [ ] OpenClaw

Adding one means writing a single module in `src/agent_history/adapters/` that
yields `Record`s. The indexer and search layer don't change.

## Development

```console
$ uv sync --group dev
$ uv run pytest                 # 67 tests, no Ollama required
```

The embedder is stubbed in tests, so the suite is fast and hermetic — it never
touches a real index and never needs a model. CI runs it on Python 3.10–3.13
across Ubuntu and macOS, which is what keeps the macOS claims honest: that
platform ships bash 3.2 and lacks `setsid`, so the scripts are checked against
both.

Keeping an installation current:

```console
$ ./scripts/sync-local.sh --check   # report drift, change nothing
$ ./scripts/sync-local.sh           # pull, reinstall what changed, run doctor
```

The checkout, the `uv tool` CLI, and the plugin drift independently and nothing
reconciles them on its own — an out-of-date CLI is easy to miss because it keeps
working.

## Known limitations

- Deleting a transcript leaves its chunks in the index — usually what you want
  (see [`reindex` is destructive](#reindex-is-destructive)), but it means `--cwd`
  filters can surface paths that no longer exist.
- The first index of a large corpus is slow — see [What it costs](#what-it-costs).
  It is a one-time cost; every run after it is incremental.
- Search quality is bounded by the embedding model. `nomic-embed-text` is a good
  default; `mxbai-embed-large` is slower and somewhat better.

## Contact

Issues and pull requests are welcome on GitHub. For anything else:
[github@dennislayden.com](mailto:github@dennislayden.com).

## License

MIT
