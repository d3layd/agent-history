#!/usr/bin/env bash
# Runs the incremental indexer. Invoked by Claude Code hooks (SessionEnd,
# PreCompact) and optionally by a cron safety-net.
#
# Usage:
#   trigger-index.sh <label> [--if-changed] [--foreground]
#
#   <label>        describes the trigger, and is written to the log so runs can
#                  be told apart
#   --if-changed   exit before doing anything (no indexer, no Ollama wake) unless
#                  a transcript changed since the last run. For cron, so idle
#                  hours cost nothing.
#   --foreground   don't detach; wait for indexing to finish
#
# The script detaches itself by default so a hook never delays session exit.
# A flock guard serialises overlapping runs, since the indexer writes a single
# SQLite file. A stamp file marks the start of the most recent run.
set -u

# The log records only counts and rates, never transcript text — but it sits
# beside the index, so keep everything this script creates owner-only.
umask 077

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

LABEL="${1:-manual}"
shift || true

IF_CHANGED=0
FOREGROUND=0
for arg in "$@"; do
  case "$arg" in
    --if-changed) IF_CHANGED=1 ;;
    --foreground) FOREGROUND=1 ;;
  esac
done

if ! command -v agent-history >/dev/null 2>&1; then
  echo "trigger-index: agent-history is not on PATH" >&2
  exit 127
fi

# Detach unless we already are the detached copy, so hooks return immediately.
if [ "$FOREGROUND" -eq 0 ] && [ "${AGENT_HISTORY_DETACHED:-0}" != "1" ]; then
  export AGENT_HISTORY_DETACHED=1
  if command -v setsid >/dev/null 2>&1; then
    setsid "$0" "$LABEL" "$@" </dev/null >/dev/null 2>&1 &
  else
    # macOS has no setsid; nohup is sufficient there.
    nohup "$0" "$LABEL" "$@" </dev/null >/dev/null 2>&1 &
  fi
  exit 0
fi

DATA_DIR="$(agent-history datadir)"
mkdir -p "$DATA_DIR"
LOG="$DATA_DIR/index.log"
LOCK="$DATA_DIR/.index.lock"
STAMP="$DATA_DIR/.last-index"

# Cheap pre-check: skip entirely when no source file is newer than the stamp.
# `agent-history sources` is the single source of truth for what gets scanned,
# so this list can never drift from what the indexer actually reads.
if [ "$IF_CHANGED" -eq 1 ] && [ -e "$STAMP" ]; then
  # Read with a loop rather than `mapfile`, which is bash 4+ and so absent from
  # the bash 3.2 that ships with macOS.
  SOURCES=()
  while IFS= read -r src; do
    [ -n "$src" ] && SOURCES+=("$src")
  done < <(agent-history sources)
  [ "${#SOURCES[@]}" -eq 0 ] && exit 0
  changed=$(find "${SOURCES[@]}" \( -name '*.jsonl' -o -name '*.md' \) \
    -newer "$STAMP" -print -quit 2>/dev/null)
  [ -z "$changed" ] && exit 0
fi

exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1; then
  if ! flock -w 900 9; then
    echo "[$(date -Iseconds)] $LABEL: lock wait timed out, skipping" >>"$LOG" 2>&1
    exit 0
  fi
fi

# Stamp the start, not the end, so anything written during the run is picked up
# by the next pass rather than being missed.
touch "$STAMP"

{
  echo ""
  echo "[$(date -Iseconds)] $LABEL trigger (pid=$$)"
  agent-history index || echo "[$(date -Iseconds)] index failed with exit $?"
} >>"$LOG" 2>&1
