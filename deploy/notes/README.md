# Session notes: install steps

Design: `docs/design/session-notes.md`. Nothing here is installed by this PR.

## Hub (Dennis, fleet-1)

Notes are indexed by the existing `agent-history-hub-index@<tenant>` unit
once the hub runs a version with this feature, into
`/home/ahub/tenants/<tenant>/notes.db`. The MCP server gains `history_note`
and `history_recall(notes=...)` after the same upgrade and a restart of
`agent-history-mcp`. The router picks up the new tool and parameter on its
next refresh (`admin__refresh_server` for `history` / `history-rec`, or a
router restart), and `history_note` needs a write-scope route like
`history_record`'s.

Pre-create the notes directory for each machine that ships notes, so rrsync
never has to create one:

```bash
for m in MSI clamps claude-jobs claude-ai; do
  sudo -u ahub install -d -m 700 /home/ahub/inbox/dennis/$m/notes
done
```

The `.path` unit watches only the machine directories; the one-minute timer
picks notes up.

## A machine that writes notes (MSI, claude-jobs, ...)

The summary is written by a model by default (`claude -p --model haiku`,
the user's own Claude login; `ANTHROPIC_API_KEY` is stripped from the call).
Check the login works for that user first:

```bash
claude -p --model haiku --max-turns 1 --no-session-persistence --tools "" "reply OK"
cp agent-history-notes-sweep.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-history-notes-sweep.timer
```

On MSI, which has no systemd user session in WSL by default, the cron form
is: `* * * * * agent-history notes sweep --idle-minutes 5 >> ~/.local/share/agent-history/notes.log 2>&1`.

Optional, at session end (Claude Code `SessionEnd` hook, in
`~/.claude/settings.json`). It returns at once and sweeps that session in
the background:

```json
{"hooks": {"SessionEnd": [{"hooks": [{"type": "command",
  "command": "agent-history notes hook"}]}]}}
```

## Check

```bash
agent-history notes sweep --dry-run
agent-history notes search "what was decided" --mode text
```
