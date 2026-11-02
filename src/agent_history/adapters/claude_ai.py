"""claude.ai data export — a one-off backfill source, not a live adapter.

claude.ai (web, desktop and mobile chats) keeps nothing on disk. Its only
full-fidelity copy is the data export (Settings → Privacy → Export data), an
unpacked directory like:

    conversations-000/conversations.json     list of conversations
    projects-000/projects/<uuid>.json        one project per file
    memories-000/memories/<uuid>.json        account + project memories
    design_chats-000/design_chats/*.json     Claude Design chats
    frames-000/, artifact_data-000/, ...     not indexed (see below)

Older exports put `conversations.json`, `projects.json` and friends at the
top level instead; both layouts are read.

This is deliberately *not* registered in `all_adapters()`. An export is a
snapshot that only changes when the user downloads a new one, so it is
imported on demand with `agent-history import claude-ai <dir>` rather than
re-parsed (180 MB, seconds of CPU) on every hook-triggered index run.

What becomes a Record (all under agent `claude-ai`):

- chat messages: the `text` blocks of each message (`voice_note` text too);
  `thinking`, `tool_use`, `tool_result` and injected prompt blocks are
  skipped, the same line the Claude Code adapter draws. The flattened
  `message.text` field is used only when a message has no blocks at all —
  where blocks exist it carries "This block is not supported" placeholders
  and tool output.
- one `title` record per conversation: its name and claude.ai's own summary.
  Kept out of the message text so that a rename never changes a message's
  hash (which would re-import the whole conversation).
- `attachment`: text claude.ai extracted from pasted or uploaded files,
  capped at MAX_ATTACHMENT_CHARS. Uploaded images and the `files` list carry
  no text and are skipped.
- `doc` / `instructions`: project knowledge files and project
  description + custom instructions.
- `memory`: the account memory, per-project memories and memory files.
- design chats, as `user` / `assistant` under `claude.ai/design/<project>`.

Everything is parsed defensively: a malformed file or an item of unexpected
shape is counted and skipped, never fatal.
"""
from __future__ import annotations

import datetime
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "claude-ai"
SURFACE = "claude.ai"

# The largest pasted file in a real export was ~11.7M characters — a dump,
# which chunk-level search serves poorly and would take hours to embed. The
# head of a file is what identifies it, so long attachments are truncated.
MAX_ATTACHMENT_CHARS = 100_000

# Design-chat attachments of these types are Claude-side material (skill
# definitions), not anything the user wrote or supplied.
_DESIGN_SKIP_ATTACHMENTS = {"skill", "folder"}

_READ_CHUNK = 1 << 20  # streaming parser read size


class ClaudeAiExport:
    """Records from one unpacked claude.ai export directory."""

    name = AGENT

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()
        self.stats: Counter[str] = Counter()
        self._projects: dict[str, str] = {}

    # ----------------------------------------------------------- discovery

    def roots(self) -> list[Path]:
        return [self.root] if self.root.is_dir() else []

    def available(self) -> bool:
        return bool(self.conversation_files() or self.project_files()
                    or self.memory_files() or self.design_chat_files())

    def _find(self, flat: str, split_dir: str, inner: str) -> list[Path]:
        if not self.root.is_dir():
            return []
        found: list[Path] = []
        top = self.root / flat
        if top.is_file():
            found.append(top)
        for part in sorted(self.root.glob(split_dir)):
            if part.is_dir():
                found.extend(sorted(p for p in part.glob(inner) if p.is_file()))
        return found

    def conversation_files(self) -> list[Path]:
        return self._find("conversations.json", "conversations-*", "*.json")

    def project_files(self) -> list[Path]:
        return self._find("projects.json", "projects-*", "projects/*.json")

    def memory_files(self) -> list[Path]:
        return self._find("memories.json", "memories-*", "memories/*.json")

    def design_chat_files(self) -> list[Path]:
        return self._find("design_chats.json", "design_chats-*",
                          "design_chats/*.json")

    # ------------------------------------------------------------- records

    def records(self) -> Iterator[Record]:
        self.stats = Counter()
        # Projects first: their names are what conversations, memories and
        # docs are filed under.
        projects = list(self._items(self.project_files(), "projects"))
        for _path, project in projects:
            uuid = _str(project.get("uuid"))
            if uuid:
                self._projects[uuid] = _str(project.get("name")).strip() or uuid
        for path, project in projects:
            yield from self._project(path, project)
        for path, memory in self._items(self.memory_files(), "memories"):
            yield from self._memories(path, memory)
        for path, conversation in self._items(self.conversation_files(),
                                              "conversations"):
            yield from self._conversation(path, conversation)
        for path, chat in self._items(self.design_chat_files(), "design_chats"):
            yield from self._design_chat(path, chat)

    # ---------------------------------------------------------- the parts

    def _cwd(self, project_uuid: str = "", project_name: str = "") -> str:
        name = self._projects.get(project_uuid) or project_name.strip()
        return f"{SURFACE}/project/{name}" if name else SURFACE

    def _project(self, path: Path, project: dict) -> Iterator[Record]:
        uuid = _str(project.get("uuid"))
        if not uuid:
            self.stats["skipped"] += 1
            return
        self.stats["projects"] += 1
        cwd = self._cwd(uuid)
        origin = str(path)
        session = f"project:{uuid}"
        instructions = "\n\n".join(
            t for t in (_str(project.get("description")).strip(),
                        _str(project.get("prompt_template")).strip()) if t)
        if instructions:
            yield from self._emit(origin, session, cwd,
                                  _str(project.get("updated_at")),
                                  "instructions",
                                  f"{self._projects[uuid]}\n\n{instructions}")
        for doc in _dicts(project.get("docs")):
            content = _str(doc.get("content")).strip()
            if not content:
                continue
            self.stats["project_docs"] += 1
            name = _str(doc.get("filename")) or "document"
            yield from self._emit(origin, session, cwd,
                                  _str(doc.get("created_at")), "doc",
                                  f"[{name}]\n{content}")

    def _memories(self, path: Path, memory: dict) -> Iterator[Record]:
        origin = str(path)
        when = _mtime(path)
        account = _str(memory.get("conversations_memory")).strip()
        if account:
            self.stats["memories"] += 1
            yield from self._emit(origin, "memory:account", SURFACE, when,
                                  "memory", account)
        project_memories = memory.get("project_memories")
        if isinstance(project_memories, dict):
            for uuid, text in project_memories.items():
                text = _str(text).strip()
                if not text:
                    continue
                self.stats["memories"] += 1
                yield from self._emit(origin, f"memory:project:{uuid}",
                                      self._cwd(_str(uuid)), when, "memory",
                                      text)
        for item in _dicts(memory.get("memory_files")):
            text = _str(item.get("content")).strip()
            mpath = _str(item.get("path"))
            if not text:
                continue
            self.stats["memories"] += 1
            yield from self._emit(origin, f"memory:{mpath}",
                                  self._cwd(_project_in_path(mpath)),
                                  _str(item.get("updated_at")) or when,
                                  "memory", text)

    def _conversation(self, path: Path, conv: dict) -> Iterator[Record]:
        uuid = _str(conv.get("uuid"))
        messages = conv.get("chat_messages")
        if not uuid or not isinstance(messages, list):
            self.stats["skipped"] += 1
            return
        self.stats["conversations"] += 1
        # The export does not currently link chats to projects, but accept
        # the shapes it could plausibly use if it starts to.
        project = conv.get("project") if isinstance(conv.get("project"), dict) else {}
        cwd = self._cwd(_str(conv.get("project_uuid") or project.get("uuid")),
                        _str(project.get("name")))
        origin = str(path)

        name = _str(conv.get("name")).strip()
        summary = _str(conv.get("summary")).strip()
        title = "\n\n".join(t for t in (name, summary) if t)
        if title:
            yield from self._emit(origin, uuid, cwd,
                                  _str(conv.get("created_at")), "title", title)

        for message in messages:
            if not isinstance(message, dict):
                self.stats["skipped"] += 1
                continue
            role = {"human": "user", "assistant": "assistant"}.get(
                _str(message.get("sender")))
            if role is None:
                self.stats["skipped"] += 1
                continue
            self.stats["messages"] += 1
            when = _str(message.get("created_at"))
            text = _message_text(message)
            if text:
                yield from self._emit(origin, uuid, cwd, when, role, text)
            for att in _dicts(message.get("attachments")):
                content = _str(att.get("extracted_content")).strip()
                if not content:
                    continue
                self.stats["attachments"] += 1
                if len(content) > MAX_ATTACHMENT_CHARS:
                    self.stats["attachments_truncated"] += 1
                    content = content[:MAX_ATTACHMENT_CHARS]
                label = _str(att.get("file_name")) or "attachment"
                yield from self._emit(origin, uuid, cwd, when, "attachment",
                                      f"[{label}]\n{content}")

    def _design_chat(self, path: Path, chat: dict) -> Iterator[Record]:
        uuid = _str(chat.get("uuid"))
        messages = chat.get("messages")
        if not uuid or not isinstance(messages, list):
            self.stats["skipped"] += 1
            return
        self.stats["design_chats"] += 1
        project = chat.get("project") if isinstance(chat.get("project"), dict) else {}
        pname = _str(project.get("name")).strip() or _str(project.get("uuid"))
        cwd = f"{SURFACE}/design/{pname}" if pname else f"{SURFACE}/design"
        origin = str(path)
        title = _str(chat.get("title")).strip()
        if title:
            yield from self._emit(origin, uuid, cwd,
                                  _str(chat.get("created_at")), "title", title)
        for message in messages:
            if not isinstance(message, dict):
                self.stats["skipped"] += 1
                continue
            body = message.get("content")
            inner = body if isinstance(body, dict) else {}
            role = _str(message.get("role") or inner.get("role"))
            if role not in ("user", "assistant"):
                self.stats["skipped"] += 1
                continue
            self.stats["design_messages"] += 1
            when = _str(message.get("created_at") or inner.get("timestamp"))
            text = body if isinstance(body, str) else _str(inner.get("content"))
            if text.strip():
                yield from self._emit(origin, uuid, cwd, when, role, text.strip())
            for att in _dicts(inner.get("attachments")):
                if _str(att.get("type")) in _DESIGN_SKIP_ATTACHMENTS:
                    continue
                content = _str(att.get("content")).strip()
                if not content:
                    continue
                self.stats["attachments"] += 1
                content = content[:MAX_ATTACHMENT_CHARS]
                label = _str(att.get("name")) or "attachment"
                yield from self._emit(origin, uuid, cwd, when, "attachment",
                                      f"[{label}]\n{content}")

    # ------------------------------------------------------------- helpers

    def _emit(self, origin: str, session: str, cwd: str, when: str,
              role: str, text: str) -> Iterator[Record]:
        if len(text) < MIN_CHARS:
            return
        yield Record(agent=AGENT, origin=origin, session_id=session, cwd=cwd,
                     timestamp=when, role=role, text=scrub(text))

    def _items(self, files: Iterable[Path],
               kind: str) -> Iterator[tuple[Path, dict]]:
        """(file, item) for every dict item; each file a list or one object."""
        for path in files:
            try:
                for item in _iter_json(path):
                    if isinstance(item, dict):
                        yield path, item
                    else:
                        self.stats["skipped"] += 1
            except (OSError, ValueError) as exc:
                self.stats["bad_files"] += 1
                print(f"warning: skipping unreadable {kind} file {path}: "
                      f"{type(exc).__name__}", file=sys.stderr)



# ------------------------------------------------------------------ parsing

def _message_text(message: dict) -> str:
    blocks = message.get("content")
    if isinstance(blocks, list) and blocks:
        parts = [
            _str(b.get("text")).strip()
            for b in blocks
            if isinstance(b, dict) and b.get("type") in ("text", "voice_note")
        ]
        return "\n\n".join(p for p in parts if p)
    return _str(message.get("text")).strip()


def _project_in_path(path: str) -> str:
    """The project uuid in a memory path like `<x>/projects/<uuid>/<file>`."""
    parts = path.split("/")
    for i, part in enumerate(parts[:-1]):
        if part == "projects":
            return parts[i + 1]
    return ""


def _iter_json(path: Path) -> Iterator[object]:
    """Yield the elements of a top-level JSON array without loading it whole.

    conversations.json runs to hundreds of MB; `json.load` would hold the
    entire parsed tree (several times the file size) at once. This decodes
    one element at a time with `raw_decode`, so peak memory is bounded by
    the largest single conversation. A file that is not an array is yielded
    as its single parsed value.
    """
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8", errors="replace") as handle:
        buf = handle.read(_READ_CHUNK)
        pos = _skip(buf, 0, " \t\r\n\ufeff")
        while pos >= len(buf):
            more = handle.read(_READ_CHUNK)
            if not more:
                break
            buf, pos = more, _skip(more, 0, " \t\r\n\ufeff")
        if buf[pos:pos + 1] != "[":
            handle.seek(0)
            text = handle.read().lstrip("\ufeff")
            yield json.loads(text)
            return
        pos += 1
        eof = False
        while True:
            pos = _skip(buf, pos, " \t\r\n,")
            while pos >= len(buf):
                if eof:
                    raise ValueError("unterminated JSON array")
                more = handle.read(_READ_CHUNK)
                eof = not more
                buf, pos = buf[pos:] + more, 0
                pos = _skip(buf, pos, " \t\r\n,")
            if buf[pos] == "]":
                return
            want = _READ_CHUNK
            while True:
                try:
                    item, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise
                    # Probably an element cut by the buffer edge: read more,
                    # doubling so a huge element costs O(n log n), not O(n²).
                    more = handle.read(want)
                    want *= 2
                    eof = not more
                    buf, pos = buf[pos:] + more, 0
            yield item
            pos = end
            if pos > _READ_CHUNK:
                buf, pos = buf[pos:], 0


def _skip(buf: str, pos: int, chars: str) -> int:
    while pos < len(buf) and buf[pos] in chars:
        pos += 1
    return pos


def _dicts(value) -> Iterator[dict]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _mtime(path: Path) -> str:
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return ""
