#!/usr/bin/env bash
# Thin wrapper around `agent-history trigger`, kept for schedulers that want a
# script path rather than a command.
#
#   trigger-index.sh <label> [--if-changed] [--foreground]
#
# All the real behaviour — detaching, locking, the stamp check, logging — lives
# in the Python CLI so it is identical on Linux, macOS and Windows. This file
# only ensures the CLI is findable when cron runs with a minimal PATH.
set -u

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if ! command -v agent-history >/dev/null 2>&1; then
  echo "trigger-index: agent-history is not on PATH" >&2
  exit 127
fi

exec agent-history trigger "$@"
