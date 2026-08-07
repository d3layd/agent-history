# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions before 0.4.1 were released during initial development and are recorded
here for context; the repository history was squashed before publication, so
they have no corresponding tags.

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
