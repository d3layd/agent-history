---
description: Semantic search over past agent conversation history using local Ollama embeddings
argument-hint: <query>
---

Run `agent-history search "$ARGUMENTS" -k 12` via the Bash tool to semantically search all past session transcripts.

The user's goal is almost always **reconstructing project context on demand** — they want a synthesized answer, not a chunk dump. So:

1. Run the search.
2. Identify the 2–4 most relevant chunks (filter out false friends from other projects that share a tech stack — same Next.js stack ≠ same project).
3. If the user named a specific project in the query, consider re-running with `--cwd <project>` to tighten results.
4. **Synthesize**: lead with a direct answer to what the user is trying to recall ("Here's what you decided about X: …"). Pull supporting quotes from the chunks as needed. Don't make the user parse raw JSONL.
5. Cite the source file + timestamp for each claim so they can dig in if they want.
6. If the top chunks look like surface references rather than the full context, offer to read the surrounding lines from the source file.

Useful flags: `-k N` for more results, `--full` for complete chunk text, `--cwd PATTERN` to scope to a project, `--agent NAME` to scope to one agent.

If the tool reports the index is empty, offer to run `agent-history index` first. If it reports a problem, `agent-history doctor` diagnoses it.
