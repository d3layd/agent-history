#!/usr/bin/env bash
# Installs agent-history and, optionally, wires up automatic indexing.
#
#   ./scripts/install.sh            interactive
#   ./scripts/install.sh --yes      accept defaults, no prompts
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL="${AGENT_HISTORY_MODEL:-nomic-embed-text}"
OLLAMA="${OLLAMA_URL:-http://localhost:11434}"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

# Probe once for a controlling terminal. A plain `[ -r /dev/tty ]` passes even
# when there is none (CI, `curl | bash`), so actually try to open it.
if { exec 3</dev/tty; } 2>/dev/null; then HAVE_TTY=1; else HAVE_TTY=0; fi

ask() {
  # ask <prompt> <default: y|n> -> returns 0 for yes
  local prompt="$1" default="$2" reply=""
  if [ "$ASSUME_YES" -eq 1 ] || [ "$HAVE_TTY" -eq 0 ]; then
    [ "$default" = "y" ]
    return
  fi
  local hint="[y/N]"; [ "$default" = "y" ] && hint="[Y/n]"
  read -r -p "  $prompt $hint " reply <&3 || reply=""
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

say "Checking prerequisites"

if ! command -v uv >/dev/null 2>&1; then
  warn "uv is not installed. Install it from https://docs.astral.sh/uv/ and re-run."
  exit 1
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

if ! curl -fsS --max-time 5 "$OLLAMA/api/tags" >/dev/null 2>&1; then
  warn "Ollama is not responding at $OLLAMA."
  warn "Start it with 'ollama serve', then re-run this script."
  exit 1
fi
ok "Ollama reachable at $OLLAMA"

if curl -fsS "$OLLAMA/api/tags" | grep -q "\"$MODEL"; then
  ok "model '$MODEL' present"
elif command -v ollama >/dev/null 2>&1 && ask "Pull embedding model '$MODEL' now?" y; then
  ollama pull "$MODEL"
else
  warn "Model '$MODEL' is missing. Run: ollama pull $MODEL"
fi

say "Installing agent-history"
uv tool install --force "$REPO_DIR"

BIN="$(command -v agent-history 2>/dev/null || echo "$HOME/.local/bin/agent-history")"
if ! command -v agent-history >/dev/null 2>&1; then
  warn "agent-history is not on PATH yet. Add ~/.local/bin to your PATH."
else
  ok "installed: $BIN"
fi

# Optional alias for anyone migrating from the pre-rename name.
if [ -x "$BIN" ] && ask "Also create a 'claude-history' alias (legacy name)?" n; then
  ln -sf "$BIN" "$(dirname "$BIN")/claude-history"
  ok "alias created: $(dirname "$BIN")/claude-history"
fi

say "Automatic indexing"
cat <<EOF
  The recommended way to keep the index current is the Claude Code plugin,
  which installs the SessionEnd and PreCompact hooks for you:

      /plugin marketplace add d3layd/agent-history
      /plugin install agent-history

  To wire the hooks by hand instead, add this to ~/.claude/settings.json:

  {
    "hooks": {
      "SessionEnd": [
        { "hooks": [ { "type": "command",
          "command": "$REPO_DIR/scripts/trigger-index.sh SessionEnd" } ] }
      ],
      "PreCompact": [
        { "hooks": [ { "type": "command",
          "command": "$REPO_DIR/scripts/trigger-index.sh PreCompact" } ] }
      ]
    }
  }

  SessionEnd only fires on a clean exit. For a safety net, add a cron entry
  that costs nothing when nothing changed:

      0 * * * * pgrep -x claude >/dev/null && $REPO_DIR/scripts/trigger-index.sh cron --if-changed
EOF

say "Building the initial index"
if ask "Index your history now? (first run can take a while)" y; then
  agent-history index
else
  echo "  Run 'agent-history index' when you're ready."
fi

say "Done"
echo "  Try:  agent-history search \"something you discussed recently\""
echo "  Check: agent-history doctor"
