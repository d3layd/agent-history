---
description: Turn automatic history injection on or off (on by default)
argument-hint: "[on|off|toggle|status]"
---

Automatic context injection is the UserPromptSubmit hook that quietly recalls
relevant past-session excerpts into each new prompt (the "📚 N memories
recalled" line). It is **on by default**.

Run the requested action via the Bash tool, then report the resulting state
in one short line:

- no argument, or `status` → `agent-history inject status`
- `on` → `agent-history inject on`
- `off` → `agent-history inject off`
- `toggle` → `agent-history inject toggle`

`$ARGUMENTS` holds the requested action (empty means status). The change
takes effect on the **next** session — Claude Code loads hooks at session
start, so injection state for the current session is already fixed. Mention
that when you report the new state.
