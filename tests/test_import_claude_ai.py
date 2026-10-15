"""claude.ai data-export import — parsing, linkage, attachments, re-import.

Fixtures are synthetic, modelled on the structure of a real export (keys,
nesting, block types), never on its content.
"""
from __future__ import annotations

import json

import pytest

from agent_history import cli, config, index, store
from agent_history.adapters import claude_ai
from agent_history.adapters.claude_ai import ClaudeAiExport

PROJECT = "11111111-1111-4111-8111-111111111111"
CONV = "22222222-2222-4222-8222-222222222222"

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


# ------------------------------------------------------------------ builders

def block(text, kind="text"):
    return {"start_timestamp": "2026-01-01T00:00:00.000000Z",
            "stop_timestamp": "2026-01-01T00:00:01.000000Z",
            "flags": None, "type": kind, "text": text, "citations": []}


def message(uuid, sender, blocks, *, text=None, created="2026-01-01T10:00:00.000000Z",
            attachments=(), files=(), parent="00000000-0000-4000-8000-000000000000"):
    if text is None:
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return {"uuid": uuid, "text": text, "content": list(blocks), "sender": sender,
            "created_at": created, "updated_at": created,
            "attachments": list(attachments), "files": list(files),
            "parent_message_uuid": parent}


def conversation(uuid=CONV, name="Planning the garden shed build",
                 summary="", messages=(), **extra):
    conv = {"uuid": uuid, "name": name, "summary": summary,
            "created_at": "2026-01-01T09:59:00.000000Z",
            "updated_at": "2026-01-01T10:05:00.000000Z",
            "account": {"uuid": "acct"}, "chat_messages": list(messages)}
    conv.update(extra)
    return conv


def basic_messages():
    return [
        message("m1", "human", [block("how deep should the shed footings go")],
                created="2026-01-01T10:00:00.000000Z"),
        message("m2", "assistant", [
            {"type": "thinking", "thinking": "private reasoning that must not be indexed",
             "summaries": [], "signature": "x"},
            block("about sixty centimetres below the frost line"),
            {"type": "tool_use", "name": "web_search", "input": {"query": "frost line depth tool input"}},
            {"type": "tool_result", "name": "web_search",
             "content": [{"type": "text", "text": "tool output that must not be indexed"}]},
            {"type": "token_budget", "remaining": 1000},
            block("and a gravel base helps drainage under the slab"),
        ], text="about sixty centimetres below the frost line\n```\n"
                "This block is not supported on your current device yet.\n```\n"
                "and a gravel base helps drainage under the slab",
            created="2026-01-01T10:01:00.000000Z"),
    ]


def write_export(root, conversations=(), projects=(), memories=None,
                 design_chats=()):
    (root / "conversations-000").mkdir(parents=True, exist_ok=True)
    (root / "conversations-000" / "conversations.json").write_text(
        json.dumps(list(conversations)), encoding="utf-8")
    if projects:
        pdir = root / "projects-000" / "projects"
        pdir.mkdir(parents=True, exist_ok=True)
        for project in projects:
            (pdir / f"{project['uuid']}.json").write_text(json.dumps(project),
                                                         encoding="utf-8")
    if memories is not None:
        mdir = root / "memories-000" / "memories"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "acct.json").write_text(json.dumps(memories), encoding="utf-8")
    if design_chats:
        ddir = root / "design_chats-000" / "design_chats"
        ddir.mkdir(parents=True, exist_ok=True)
        for chat in design_chats:
            (ddir / f"{chat['uuid']}.json").write_text(json.dumps(chat),
                                                      encoding="utf-8")
    return root


def project(uuid=PROJECT, name="Shed Build", docs=(), description="",
            prompt_template=""):
    return {"uuid": uuid, "name": name, "description": description,
            "is_private": True, "is_starter_project": False,
            "prompt_template": prompt_template,
            "created_at": "2025-12-01T00:00:00.000000Z",
            "updated_at": "2025-12-02T00:00:00.000000Z",
            "creator": {"uuid": "acct", "full_name": "Test User"},
            "docs": list(docs)}


def records(root):
    return list(ClaudeAiExport(root).records())


# --------------------------------------------------------------- basic import

def test_basic_conversation(tmp_path):
    root = write_export(tmp_path / "export", [conversation(
        summary="A discussion of footings and drainage for a small shed.",
        messages=basic_messages())])
    got = records(root)
    by_role = {r.role: r for r in got}
    assert set(by_role) == {"title", "user", "assistant"}
    for r in got:
        assert r.agent == "claude-ai"
        assert r.session_id == CONV
        assert r.cwd == "claude.ai"
        assert r.origin.endswith("conversations.json")
    assert by_role["user"].timestamp == "2026-01-01T10:00:00.000000Z"
    assert by_role["assistant"].timestamp == "2026-01-01T10:01:00.000000Z"
    # Text blocks are joined; thinking, tool traffic and placeholders are not.
    assistant = by_role["assistant"].text
    assert "sixty centimetres" in assistant and "gravel base" in assistant
    for noise in ("private reasoning", "tool output", "tool input",
                  "not supported on your current device"):
        assert noise not in assistant
    # The conversation name stays searchable, in its own record.
    assert by_role["title"].text.startswith("Planning the garden shed build")
    assert "footings and drainage" in by_role["title"].text


def test_flat_text_is_the_fallback_only_without_blocks(tmp_path):
    root = write_export(tmp_path / "export", [conversation(messages=[
        message("m1", "human", [], text="an old message that only has flat text"),
    ])])
    texts = [r.text for r in records(root) if r.role == "user"]
    assert texts == ["an old message that only has flat text"]


def test_voice_notes_are_kept(tmp_path):
    note = {"type": "voice_note", "title": "t",
            "text": "a dictated note about the shed roof pitch"}
    root = write_export(tmp_path / "export", [conversation(messages=[
        message("m1", "human", [note])])])
    assert any("roof pitch" in r.text for r in records(root))


def test_stats_count_the_export(tmp_path):
    root = write_export(tmp_path / "export", [conversation(messages=basic_messages())])
    source = ClaudeAiExport(root)
    list(source.records())
    assert source.stats["conversations"] == 1
    assert source.stats["messages"] == 2


def test_flat_legacy_layout(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    (root / "conversations.json").write_text(
        json.dumps([conversation(messages=basic_messages())]), encoding="utf-8")
    (root / "projects.json").write_text(
        json.dumps([project(docs=[{"uuid": "d", "filename": "plan.md",
                                   "content": "the legacy layout project document",
                                   "created_at": "2025-12-01T00:00:00Z"}])]),
        encoding="utf-8")
    roles = {r.role for r in records(root)}
    assert {"user", "assistant", "doc"} <= roles


# ------------------------------------------------------------ project linkage

def test_project_docs_instructions_and_memories(tmp_path):
    doc = {"uuid": "d1", "filename": "materials.md",
           "content": "timber list: four by two studs and OSB sheathing",
           "created_at": "2025-12-01T00:00:00.000000Z"}
    memories = {
        "conversations_memory": "the user is building a shed in their garden",
        "project_memories": {PROJECT: "this project tracks the shed build budget"},
        "memory_files": [
            {"path": f"mem/projects/{PROJECT}/notes.md",
             "content": "a project memory file about the shed",
             "updated_at": "2026-01-02T00:00:00.000000Z"},
            {"path": "mem/general/prefs.md",
             "content": "an account-level memory file about preferences",
             "updated_at": "2026-01-03T00:00:00.000000Z"},
        ],
        "account_uuid": "acct",
    }
    root = write_export(tmp_path / "export",
                        projects=[project(docs=[doc],
                                          description="Planning a garden shed",
                                          prompt_template="Answer in metric units")],
                        memories=memories)
    got = records(root)
    shed = "claude.ai/project/Shed Build"

    (doc_rec,) = [r for r in got if r.role == "doc"]
    assert doc_rec.cwd == shed
    assert doc_rec.session_id == f"project:{PROJECT}"
    assert doc_rec.text.startswith("[materials.md]")

    (instr,) = [r for r in got if r.role == "instructions"]
    assert instr.cwd == shed and "metric units" in instr.text

    mems = {r.session_id: r for r in got if r.role == "memory"}
    assert mems["memory:account"].cwd == "claude.ai"
    assert mems[f"memory:project:{PROJECT}"].cwd == shed
    assert mems[f"memory:mem/projects/{PROJECT}/notes.md"].cwd == shed
    assert mems["memory:mem/general/prefs.md"].cwd == "claude.ai"
    assert mems["memory:mem/general/prefs.md"].timestamp.startswith("2026-01-03")


def test_conversation_linked_to_a_project(tmp_path):
    root = write_export(tmp_path / "export",
                        [conversation(messages=basic_messages(),
                                      project_uuid=PROJECT)],
                        projects=[project()])
    chat = [r for r in records(root) if r.role in ("user", "assistant")]
    assert chat and all(r.cwd == "claude.ai/project/Shed Build" for r in chat)


def test_design_chats(tmp_path):
    chat = {
        "uuid": "33333333-3333-4333-8333-333333333333", "title": "Landing page",
        "project": {"uuid": "44444444-4444-4444-8444-444444444444",
                    "name": "Brand Site"},
        "created_at": "2026-02-01T00:00:00.000000Z",
        "updated_at": "2026-02-01T00:00:00.000000Z",
        "messages": [
            {"uuid": "dm1", "role": "user", "created_at": "2026-02-01T00:00:01Z",
             "content": {"id": "dm1", "role": "user", "timestamp": "x",
                         "content": "make the hero section calmer and quieter",
                         "attachments": [
                             {"id": "a1", "name": "skill-def", "type": "skill",
                              "content": "a skill definition that is not user content"},
                             {"id": "a2", "name": "brief.txt", "type": "text",
                              "content": "the design brief the user pasted in"},
                         ]}},
            {"uuid": "dm2", "role": "assistant", "created_at": "2026-02-01T00:00:02Z",
             "content": {"id": "dm2", "role": "assistant", "timestamp": "x",
                         "content": "softened the hero palette and spacing"}},
        ],
    }
    got = records(write_export(tmp_path / "export", design_chats=[chat]))
    assert {r.cwd for r in got} == {"claude.ai/design/Brand Site"}
    assert {r.role for r in got} == {"user", "assistant", "attachment"}
    assert not any("skill definition" in r.text for r in got)


# --------------------------------------------------------------- attachments

def test_attachment_text_is_indexed_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_ai, "MAX_ATTACHMENT_CHARS", 100)
    small = {"file_name": "notes.txt", "file_size": 40, "file_type": "txt",
             "extracted_content": "pasted notes about the shed door hinges"}
    huge = {"file_name": "dump.csv", "file_size": 9999, "file_type": "text/csv",
            "extracted_content": "row,value\n" * 500}
    image = {"file_uuid": "f1", "file_name": "photo.jpg"}
    root = write_export(tmp_path / "export", [conversation(messages=[
        message("m1", "human", [block("here are my notes and a data dump")],
                attachments=[small, huge], files=[image]),
    ])])
    source = ClaudeAiExport(root)
    atts = [r for r in source.records() if r.role == "attachment"]
    assert [a.text.split("\n", 1)[0] for a in atts] == ["[notes.txt]", "[dump.csv]"]
    assert atts[0].session_id == CONV
    assert len(atts[1].text) <= 100 + len("[dump.csv]\n")
    assert source.stats["attachments_truncated"] == 1
    # Uploaded images carry no text and never become records.
    assert not any("photo.jpg" in r.text for r in records(root))


# ------------------------------------------------- malformed / unknown shapes

def test_malformed_and_unknown_shapes_are_skipped(tmp_path, capsys):
    good = conversation(messages=basic_messages())
    weird = [
        "not a dict",
        {"name": "no uuid at all", "chat_messages": []},
        conversation(uuid="c-bad-messages", chat_messages="not a list"),
        conversation(uuid="c-mixed", messages=[
            "not a dict",
            message("x1", "system", [block("a sender this importer does not know")]),
            {"uuid": "x2", "sender": "human", "content": ["not a block", 7],
             "text": None, "created_at": None},
            message("x3", "human", [block("a perfectly normal surviving message")]),
        ]),
    ]
    root = write_export(tmp_path / "export", [good, *weird])
    # A broken project file and a truncated memories file alongside.
    pdir = root / "projects-000" / "projects"
    pdir.mkdir(parents=True)
    (pdir / "broken.json").write_text("{not json", encoding="utf-8")
    mdir = root / "memories-000" / "memories"
    mdir.mkdir(parents=True)
    (mdir / "m.json").write_text('{"conversations_memory": 5, "memory_files": "x"}',
                                 encoding="utf-8")

    source = ClaudeAiExport(root)
    got = list(source.records())
    texts = " ".join(r.text for r in got)
    assert "sixty centimetres" in texts
    assert "perfectly normal surviving message" in texts
    assert "does not know" not in texts
    assert source.stats["bad_files"] == 1
    assert source.stats["skipped"] >= 4
    assert "skipping unreadable projects file" in capsys.readouterr().err


def test_truncated_conversations_file_keeps_what_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_ai, "_READ_CHUNK", 64)
    root = write_export(tmp_path / "export", [conversation(messages=basic_messages())])
    path = root / "conversations-000" / "conversations.json"
    good = path.read_text(encoding="utf-8")
    second = json.dumps(conversation(uuid="c2", messages=basic_messages()))
    path.write_text(good[:-1] + ", " + second[: len(second) // 2], encoding="utf-8")
    source = ClaudeAiExport(root)
    got = list(source.records())
    assert {r.session_id for r in got} == {CONV}
    assert source.stats["bad_files"] == 1


def test_missing_export_is_not_available(tmp_path):
    assert not ClaudeAiExport(tmp_path / "nope").available()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not ClaudeAiExport(empty).available()


# ---------------------------------------------------------- streaming parser

@pytest.mark.parametrize("chunk", [1, 7, 64, 1 << 20])
def test_streaming_parser_matches_json_load(tmp_path, monkeypatch, chunk):
    monkeypatch.setattr(claude_ai, "_READ_CHUNK", chunk)
    data = [conversation(uuid=f"c{i}", messages=basic_messages() * (i + 1))
            for i in range(5)]
    path = tmp_path / "c.json"
    path.write_text("\ufeff \n" + json.dumps(data, indent=1), encoding="utf-8")
    assert list(claude_ai._iter_json(path)) == data


def test_streaming_parser_accepts_a_single_object(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"uuid": "x"}), encoding="utf-8")
    assert list(claude_ai._iter_json(path)) == [{"uuid": "x"}]


# ------------------------------------------------------- end-to-end and dedup

def _import(root):
    return index.run_import(ClaudeAiExport(root))


def _chunks():
    db = store.open_for_read()
    return db.execute("SELECT agent, session_id, role, text FROM chunks").fetchall()


@needs_vec
def test_import_then_reimport_is_idempotent(tmp_path, fake_embed):
    root = write_export(tmp_path / "export", [conversation(messages=basic_messages())])
    assert _import(root) == 0
    first = len(fake_embed)
    assert first == 3  # title, user, assistant
    assert _import(root) == 0
    assert len(fake_embed) == first, "re-import must not re-embed anything"
    assert {row[0] for row in _chunks()} == {"claude-ai"}


@needs_vec
def test_newer_export_adds_only_new_messages(tmp_path, fake_embed):
    old = write_export(tmp_path / "old", [conversation(messages=basic_messages())])
    _import(old)
    before = {row[3] for row in _chunks()}

    # A later export of the same account: the conversation grew by one turn,
    # one message was edited in place, and a regenerated reply sits on a
    # sibling branch. Different directory, different origin.
    msgs = basic_messages()
    msgs[0] = message("m1", "human", [block("how deep should the shed footings go, roughly")])
    msgs.append(message("m2b", "assistant",
                        [block("about sixty centimetres below the frost line"),
                         block("and a gravel base helps drainage under the slab")],
                        parent="m1"))
    msgs.append(message("m3", "human", [block("thanks, and what about the roof felt")]))
    new = write_export(tmp_path / "new", [conversation(messages=msgs)])
    _import(new)

    after = {row[3] for row in _chunks()}
    added = after - before
    assert added == {
        "how deep should the shed footings go, roughly",
        "thanks, and what about the roof felt",
    }, "only the edited and the new message may add chunks"
    # The regenerated reply duplicates an existing text exactly: stored once.
    assert sum("sixty centimetres" in t for t in after) == 1


@needs_vec
def test_renaming_a_conversation_does_not_duplicate_its_messages(tmp_path, fake_embed):
    _import(write_export(tmp_path / "a", [conversation(messages=basic_messages())]))
    before = len(fake_embed)
    _import(write_export(tmp_path / "b", [conversation(
        name="Shed footings, renamed later", messages=basic_messages())]))
    assert len(fake_embed) == before + 1, "only the new title record is new"


@needs_vec
def test_reindex_refuses_to_wipe_imported_history(tmp_path, fake_embed, transcripts):
    from conftest import msg

    transcripts("proj", "s", [msg("user", "a claude code message on disk")])
    _import(write_export(tmp_path / "export", [conversation(messages=basic_messages())]))
    # The export still exists, but no adapter re-reads it: a rebuild would
    # silently lose it.
    assert index.run(reindex=True) == 1
    assert any(row[0] == "claude-ai" for row in _chunks())


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    root = write_export(tmp_path / "export", [conversation(messages=basic_messages())],
                        projects=[project()])
    assert cli.main(["import", "claude-ai", str(root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "conversations:" in out and "assistant: 1" in out
    assert "date range:" in out
    assert not config.db_path().exists()
    assert not config.data_home().exists()


def test_cli_rejects_a_missing_directory(tmp_path):
    assert cli.main(["import", "claude-ai", str(tmp_path / "nope")]) == 1
