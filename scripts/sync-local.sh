#!/usr/bin/env bash
# Bring this machine's installation in line with the repository.
#
#   ./scripts/sync-local.sh            pull, reinstall if changed, report
#   ./scripts/sync-local.sh --check    report drift only, change nothing
#   ./scripts/sync-local.sh --force    reinstall even when versions already match
#
# There are three things that drift independently — the checkout, the CLI
# installed by `uv tool`, and the Claude Code plugin — and nothing keeps them
# aligned on its own. A CLI left behind a bumped repo is easy to miss because
# everything keeps working.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-sync}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

repo_version() {
  sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_DIR/pyproject.toml" | head -1
}

cli_version() {
  command -v agent-history >/dev/null 2>&1 || { echo "not-installed"; return; }
  agent-history --version 2>/dev/null | tr -d 'v' | awk '{print $NF}' \
    || echo "unknown"
}

plugin_version() {
  command -v claude >/dev/null 2>&1 || { echo "no-claude"; return; }
  claude plugin details history 2>/dev/null | head -1 | awk '{print $NF}' \
    || echo "not-installed"
}

say "Repository"
if [ "$MODE" != "--check" ]; then
  git -C "$REPO_DIR" pull --ff-only --quiet 2>/dev/null \
    && ok "pulled $(git -C "$REPO_DIR" rev-parse --short HEAD)" \
    || warn "could not fast-forward (local commits or no remote?)"
else
  ok "at $(git -C "$REPO_DIR" rev-parse --short HEAD)"
fi

REPO_V="$(repo_version)"
CLI_V="$(cli_version)"
PLUGIN_V="$(plugin_version)"

say "Versions"
printf '  %-10s %s\n' "repo"   "$REPO_V"
printf '  %-10s %s\n' "cli"    "$CLI_V"
printf '  %-10s %s\n' "plugin" "$PLUGIN_V"

DRIFT=0
[ "$CLI_V"    != "$REPO_V" ] && DRIFT=1
[ "$PLUGIN_V" != "$REPO_V" ] && [ "$PLUGIN_V" != "no-claude" ] && DRIFT=1

if [ "$MODE" = "--check" ]; then
  if [ "$DRIFT" -eq 1 ]; then
    warn "drift detected — run ./scripts/sync-local.sh to fix"
    exit 1
  fi
  ok "everything in sync"
  exit 0
fi

if [ "$DRIFT" -eq 0 ] && [ "$MODE" != "--force" ]; then
  ok "already in sync (use --force to reinstall anyway)"
else
  say "Reinstalling"
  uv tool install --force "$REPO_DIR" >/dev/null 2>&1 \
    && ok "cli -> $(cli_version)" \
    || warn "cli install failed"

  if command -v claude >/dev/null 2>&1; then
    claude plugin marketplace update agent-history >/dev/null 2>&1 || true
    # The plugin is version-pinned, so a bump in plugin.json is what triggers
    # an update; reinstalling is the reliable way to land it either way.
    claude plugin install history@agent-history >/dev/null 2>&1 \
      && ok "plugin -> $(plugin_version)" \
      || warn "plugin install failed (is the marketplace added?)"
  fi
fi

say "Health"
if command -v agent-history >/dev/null 2>&1; then
  agent-history doctor 2>&1 | sed 's/^/  /'
else
  warn "agent-history is not on PATH"
fi
