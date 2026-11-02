# claude.ai profile instruction (draft)

Paste the block below into claude.ai → Settings → Profile → "What personal
preferences should Claude consider in responses?". It assumes the router
mounts the hub as `history` (read) and `history-rec` (write), so the tools
appear as `history__history_recall`, `history__history_show` and
`history-rec__history_record`. If you mount them under other ids, change the
names in the block to match.

Draft for review. Measure after two weeks: how many chats got a record, and
whether recalls came back useful (see epic #28).

---

```text
I keep one searchable history of all my AI conversations (Claude Code, claude.ai, other agents). Use it through the router tools:

Recall: when past context would help (I mention earlier work, a project, a decision or "what did we do about X", or you are about to ask me to re-explain something), call history__history_recall first with a short, specific query. Filter with cwd=<project> or surface=claude-ai-chat when that narrows it. Use history__history_show with a hit's session to read around it. Say which past session you are drawing on. Don't recall for self-contained questions.

Record: call history-rec__history_record
- at milestones: a decision is made, a root cause is found, a plan is agreed, or something is shipped;
- when the chat wraps up (I say thanks/done/that's it, or we change topic) if anything worth keeping happened since the last record.
Fill in: title (one line), summary (2-5 sentences: what we did and why), decisions[], findings[], links[] (URLs, PRs, issues, files), chat_url (this chat's URL if you know it), project (repo or project name). In Duet, also pass duet_channel_id (this channel's id) and duet_message_id (the message the work started from), so the record links back to the channel. Keep items short and factual. Records are append-only, so add a new record rather than repeating an old one, and don't record small talk or anything I ask you not to keep. Never put passwords, keys or tokens in a record (they get redacted anyway). After recording, tell me in one line, with the record id.

If a history tool errors or isn't available, carry on without it and mention it once.
```
