"""Hosted-session sources: the provider registry, URL checks and the note path.

No MCP SDK here, so these run even without the ``mcp`` extra. The tool-level
tests are in test_hosted_notes_mcp.py. No network: the embedder is the stub.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from agent_history import cli, hosted, notes, redact

SID = "session_01HhNr55p49oCfdQAUj4K8uQ"
LINK = f"https://claude.ai/code/{SID}"
GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])
ANT = "".join(["sk-", "ant-", "api03-abcdefghijklmnopqrstuvwxyz"])


def source(**kw):
    return notes.make_source(**{"provider": "claude-ai", "session_id": SID,
                                "session_url": LINK, **kw})


# ------------------------------------------------------------------- accepted

def test_a_hosted_source_needs_no_machine():
    assert source() == {"kind": "hosted-session", "provider": "claude-ai",
                        "session_id": SID, "url": LINK}


def test_it_round_trips_through_the_wire_check():
    assert notes.validate_source(source()) == source()


def test_show_target_points_at_the_session_id():
    assert notes.show_target(source(), {"start": "2026-11-06T00:00:00Z"}) == {
        "session": SID, "around": "2026-11-06T00:00:00Z"}


# -------------------------------------------------------------------- refused

@pytest.mark.parametrize("provider", [
    "not-a-provider", "chatgpt", "codex-cloud",  # not registered in this version
    "CLAUDE-AI", "Claude-AI", "claude_ai", "claude.ai", "claudeai",  # no normalizing here
    " claude-ai", "claude-ai ", "claude-ai\n", 5, ["claude-ai"], {"a": 1},
])
def test_a_provider_outside_the_enum_is_refused(provider):
    with pytest.raises(notes.NoteError, match="provider must be one of"):
        source(provider=provider)


def test_refusing_a_provider_does_not_grow_the_registry():
    before = dict(hosted.PROVIDERS)
    with pytest.raises(notes.NoteError):
        source(provider="brand-new-provider")
    assert hosted.PROVIDERS == before


@pytest.mark.parametrize("url", [
    # the host: exact match, not substring, suffix or prefix
    f"https://claude.ai.evil.com/code/{SID}",
    f"https://evil.claude.ai/code/{SID}",
    f"https://notclaude.ai/code/{SID}",
    f"https://evil.com/claude.ai/code/{SID}",
    f"https://evil.com/code/{SID}",
    f"https://evil.com#claude.ai/code/{SID}",
    f"https://evil.com?claude.ai/code/{SID}",
    f"https://claude.ai./code/{SID}",
    f"https://CLAUDE.AI/code/{SID}",
    f"https://claude.ai:443/code/{SID}",
    f"https://claude.ai:8443/code/{SID}",
    f"https://claude.ai:notaport/code/{SID}",
    f"https://claude%2eai/code/{SID}",
    f"https://claude．ai/code/{SID}",        # fullwidth dot
    f"https://clаude.ai/code/{SID}",        # Cyrillic a
    f"https://[::1]/code/{SID}",
    f"https://[bad/code/{SID}",
    # userinfo, in every position it can hide
    f"https://claude.ai@evil.com/code/{SID}",
    f"https://user@claude.ai/code/{SID}",
    f"https://user:pw@claude.ai/code/{SID}",
    f"https://evil.com@claude.ai/code/{SID}",
    f"https://claude.ai:pw@evil.com/code/{SID}",
    f"https://claude.ai\\@evil.com/code/{SID}",
    f"https://evil.com\\@claude.ai/code/{SID}",
    # the scheme
    f"http://claude.ai/code/{SID}",
    f"HTTPS://claude.ai/code/{SID}",
    f"ftp://claude.ai/code/{SID}",
    f"javascript://claude.ai/code/{SID}",
    f"//claude.ai/code/{SID}",
    f"claude.ai/code/{SID}",
    f"https:/claude.ai/code/{SID}",
    f"https:claude.ai/code/{SID}",
    # the path
    "https://claude.ai/",
    "https://claude.ai/code/",
    f"https://claude.ai/chat/{SID}",
    f"https://claude.ai/code/{SID}/",
    f"https://claude.ai/code/{SID}/extra",
    f"https://claude.ai//code/{SID}",
    f"https://claude.ai/code/../code/{SID}",
    f"https://claude.ai/CODE/{SID}",
    f"https://claude.ai/code/{SID[:-1]}%75",     # an encoded last character
    # query and fragment can carry anything, including a token
    f"https://claude.ai/code/{SID}?x=1",
    f"https://claude.ai/code/{SID}?",
    f"https://claude.ai/code/{SID}#frag",
    f"https://claude.ai/code/{SID}?token={GH}",
    # whitespace and control characters, which urlsplit quietly drops
    f" https://claude.ai/code/{SID}",
    f"https://claude.ai/code/{SID} ",
    f"https://claude.ai/code/{SID}\n",
    f"https://claude.ai/code/\t{SID}",
    f"https://clau\nde.ai/code/{SID}",
    f"https://claude.ai/code/{SID}\x00",
    # not a URL at all
    "not a url", 5, ["https://claude.ai"],
    # another session's link
    "https://claude.ai/code/session_01HhNr55p49oCfdQAUj4K8uX",
    # too long
    "https://claude.ai/code/session_" + "a" * 600,
])
def test_a_url_that_is_not_this_providers_session_is_refused(url):
    with pytest.raises(notes.NoteError):
        source(session_url=url)


@pytest.mark.parametrize("url,reason", [
    (f"http://claude.ai/code/{SID}", "https"),
    (f"https://claude.ai.evil.com/code/{SID}", "claude.ai"),
    (f"https://claude.ai@evil.com/code/{SID}", "user"),
    (f"https://claude.ai/code/{SID}?x=1", "query"),
    ("https://claude.ai/code/session_other", "session_id"),
])
def test_a_refusal_says_why(url, reason):
    with pytest.raises(notes.NoteError, match=reason):
        source(session_url=url)


@pytest.mark.parametrize("session_id", [
    "", None, 5, " " + SID, SID + " ", SID + "\n", "session_", "session_a b",
    "session_a/b", "session_a.b", "session_a:b", "session_a=b", "session_a@b",
    "session_a_b", "session_a-b",
    "01HhNr55p49oCfdQAUj4K8uQ",            # no session_ prefix
    "session_" + "a" * 121,                # over the provider's bound
    "a" * 129,                             # over the general bound
    "-session_abc",
    "../../etc/passwd",
])
def test_session_id_is_bounded_and_plain(session_id):
    # The URL is built from a known-good value, so only the id can be at fault.
    with pytest.raises(notes.NoteError):
        notes.make_source(provider="claude-ai", session_id=session_id, session_url=LINK)


def test_the_longest_allowed_id_is_accepted():
    sid = "session_" + "a" * 120
    assert source(session_id=sid, session_url=f"https://claude.ai/code/{sid}")["session_id"] == sid


@pytest.mark.parametrize("kw", [
    {"provider": None},
    {"provider": ""},
    {"session_id": None},
    {"session_url": None},
    {"session_url": ""},
    {"machine": "MSI"},
    {"job_id": "cj_20261101T120000Z_a1b2c3"},
    {"chat_url": "https://claude.ai/chat/0f6a7c2e-1234-4abc-8def-0123456789ab"},
    {"duet_channel_id": "c1", "duet_message_id": "m1"},
])
def test_missing_or_mixed_fields_are_refused(kw):
    with pytest.raises(notes.NoteError):
        source(**kw)


def test_a_hosted_link_in_chat_url_is_refused_and_says_what_to_pass():
    with pytest.raises(notes.NoteError, match="provider"):
        notes.make_source(chat_url=LINK)


# --------------------------------------------------------------------- wire

@pytest.mark.parametrize("patch", [
    {"provider": "not-a-provider"},
    {"provider": 5},
    {"session_id": 5},
    {"url": 5},
    {"url": None},
    {"url": f"https://claude.ai.evil.com/code/{SID}"},
    {"session_id": "session_other"},
])
def test_a_bad_hosted_line_on_the_wire_is_skipped_not_fatal(patch):
    note = notes.build(title="t", summary="s", source=source())
    note["source"].update(patch)
    assert notes.parse(json.dumps(note)) is None


def test_a_good_hosted_line_parses():
    note = notes.build(title="t", summary="s", source=source())
    assert notes.parse(json.dumps(note))["source"] == source()


# ----------------------------------------------------------------- redaction

def test_the_redactor_keeps_the_link_and_still_redacts_the_note():
    counter = Counter()
    note = notes.build(title=f"rotated {GH}", summary=f"key {ANT} leaked; password=Hunter2Hunter2",
                       decisions=[f"revoke {GH}"], links=[LINK], source=source(), counter=counter)
    body = json.dumps(note)
    assert GH not in body and ANT not in body and "Hunter2Hunter2" not in body
    assert note["source"]["url"] == LINK and note["source"]["session_id"] == SID
    assert note["links"] == [LINK]
    assert counter["github_token"] == 2 and counter["anthropic_key"] == 1


def test_the_link_survives_redaction_of_the_searchable_text():
    """note_text is stored as written; if anything ever redacts it again, the
    source line must come through whole."""
    note = notes.build(title="t", summary="s", source=source())
    text = notes.note_text(note)
    assert LINK in text and SID in text
    assert redact.redact(text)[0] == text


@pytest.mark.parametrize("sid", [
    "session_01HhNr55p49oCfdQAUj4K8uQ",
    "session_tokenSECRETpassword0123456789",   # secret-like words, no secret
    "session_" + "A" * 120,
])
def test_no_valid_id_or_link_is_changed_by_the_redactor(sid):
    url = f"https://claude.ai/code/{sid}"
    src = source(session_id=sid, session_url=url)
    note = notes.build(title="t", summary="s", source=src)
    assert note["source"] == src
    assert redact.redact(notes.note_text(note))[0] == notes.note_text(note)


@pytest.mark.parametrize("value", [GH, ANT])
def test_a_secret_passed_as_the_id_is_refused_and_not_echoed(value):
    with pytest.raises(notes.NoteError) as err:
        source(session_id=value, session_url=f"https://claude.ai/code/{value}")
    assert value not in str(err.value)


def test_a_secret_in_the_url_is_not_echoed():
    with pytest.raises(notes.NoteError) as err:
        source(session_url=f"https://claude.ai/code/{SID}?token={GH}")
    assert GH not in str(err.value)


def test_a_wider_provider_still_refuses_a_secret_shaped_id(monkeypatch):
    """A future registry entry may allow - and _. The redactor's patterns
    still apply, so a note can't store an id it would have to mangle."""
    monkeypatch.setitem(hosted.PROVIDERS, "wide", hosted.Provider(
        slug="wide", host="wide.example", path_prefix="/s/",
        id_pattern=re.compile(r"[A-Za-z0-9_-]{1,120}")))
    good = notes.make_source(provider="wide", session_id="abc-def",
                             session_url="https://wide.example/s/abc-def")
    assert good["session_id"] == "abc-def"
    secret = "x-" + ANT
    with pytest.raises(notes.NoteError, match="secret") as err:
        notes.make_source(provider="wide", session_id=secret,
                          session_url=f"https://wide.example/s/{secret}")
    assert ANT not in str(err.value)


# ------------------------------------------------------------------ store

def test_store_search_returns_the_link_and_show(fake_embed):
    notes.save(notes.build(title="Cloud session wrapped up", summary="fixed the zebra import",
                           source=source()))
    (hit,) = notes.search("zebra import", mode="text")
    assert hit["link"] == LINK
    assert hit["source"] == source()
    assert hit["show"] == {"session": SID, "around": None}
    assert notes.hosted_link(SID) == {"provider": "claude-ai", "link": LINK}


def test_hosted_link_is_none_without_a_note_or_a_store(fake_embed):
    assert notes.hosted_link(SID) is None
    notes.save(notes.build(title="t", summary="other", source=source()))
    assert notes.hosted_link("session_other") is None


def test_other_kinds_get_no_link_field_and_are_no_hosted_link(fake_embed):
    notes.save(notes.build(title="code note", summary="zebra code",
                           source=notes.make_source(session_id=SID, machine="MSI")))
    (hit,) = notes.search("zebra code", mode="text")
    assert "link" not in hit
    assert notes.hosted_link(SID) is None


def test_old_rows_sit_beside_new_ones(fake_embed):
    """No schema change: every older kind stays readable next to a hosted row."""
    for src in (notes.make_source(chat_url="https://claude.ai/chat/0f6a7c2e-1234-4abc-8def-0123456789ab"),
                notes.make_source(duet_channel_id="c1", duet_message_id="m1"),
                notes.make_source(session_id="sess-1", machine="MSI"),
                source()):
        notes.save(notes.build(title=src["kind"], summary=f"zebra {src['kind']}", source=src))
    kinds = sorted(hit["source"]["kind"] for hit in notes.search("zebra", mode="text"))
    assert kinds == ["claude-ai", "claude-code", "duet", "hosted-session"]


# ------------------------------------------------------------------ registry

def test_every_registered_provider_is_its_own_normal_slug():
    for slug, entry in hosted.PROVIDERS.items():
        assert entry.slug == slug
        assert hosted.normalize_slug(slug) == slug


def test_no_two_registered_providers_are_duplicates():
    compact = [slug.replace("-", "") for slug in hosted.PROVIDERS]
    assert len(compact) == len(set(compact))


def test_every_registered_host_is_a_bare_lowercase_hostname():
    for entry in hosted.PROVIDERS.values():
        assert hosted.valid_host(entry.host), entry.host
        assert entry.path_prefix.startswith("/") and entry.path_prefix.endswith("/")


def test_this_version_ships_only_the_provider_the_repo_can_justify():
    assert hosted.slugs() == ("claude-ai",)


@pytest.mark.parametrize("name,slug", [
    ("ChatGPT", "chatgpt"),
    ("Codex Cloud", "codex-cloud"),
    ("  codex_cloud  ", "codex-cloud"),
    ("Some.Provider!!", "some-provider"),
    ("Café AI", "cafe-ai"),
])
def test_normalize_slug(name, slug):
    assert hosted.normalize_slug(name) == slug


@pytest.mark.parametrize("name", ["", "   ", "!!!", "中文", "a" * 41, None, 5])
def test_normalize_slug_refuses_what_has_no_slug(name):
    with pytest.raises(hosted.HostedError):
        hosted.normalize_slug(name)


@pytest.mark.parametrize("name", ["claude-ai", "Claude AI", "CLAUDE_AI", "claude.ai", "claudeai"])
def test_the_addition_check_catches_duplicates(name):
    with pytest.raises(hosted.HostedError, match="already registered"):
        hosted.plan_addition(name, "example.com")


@pytest.mark.parametrize("host", ["", "localhost", "https://x.com", "x.com:443", "X.COM",
                                  "x.com/", "user@x.com", "x..com", "-x.com"])
def test_the_addition_check_refuses_a_bad_host(host):
    with pytest.raises(hosted.HostedError, match="host"):
        hosted.plan_addition("New Provider", host)


def test_the_addition_check_does_not_change_the_registry():
    before = dict(hosted.PROVIDERS)
    assert hosted.plan_addition("New Provider", "new.example.com") == {
        "slug": "new-provider", "host": "new.example.com"}
    assert hosted.PROVIDERS == before


def test_providers_command_lists_and_checks(capsys):
    assert cli.main(["notes", "providers"]) == 0
    assert "claude-ai" in capsys.readouterr().out
    assert cli.main(["notes", "providers", "--check", "Codex Cloud",
                     "--host", "new.example.com"]) == 0
    out = capsys.readouterr().out
    assert "codex-cloud" in out and "hosted.py" in out
    assert cli.main(["notes", "providers", "--check", "Claude AI",
                     "--host", "new.example.com"]) == 2
    assert "already registered" in capsys.readouterr().err
    assert cli.main(["notes", "providers", "--check", "x"]) == 2   # --host is required
    assert hosted.slugs() == ("claude-ai",)


# ----------------------------------------------------------------------- CLI

def test_notes_add_takes_a_hosted_source(fake_embed, capsys):
    assert cli.main(["notes", "add", "--title", "Cloud note", "--summary", "zebra cli",
                     "--provider", "claude-ai", "--session", SID,
                     "--session-url", LINK]) == 0
    capsys.readouterr()
    (hit,) = notes.search("zebra cli", mode="text")
    assert hit["source"]["kind"] == "hosted-session"
    assert cli.main(["notes", "search", "zebra cli", "--mode", "text"]) == 0
    assert LINK in capsys.readouterr().out


@pytest.mark.parametrize("extra", [
    ["--provider", "not-a-provider", "--session", SID, "--session-url", LINK],
    ["--provider", "claude-ai", "--session", SID, "--session-url", "https://evil.com/code/" + SID],
    ["--provider", "claude-ai", "--session", SID, "--session-url", LINK,
     "--session-machine", "MSI"],
    ["--provider", "claude-ai", "--session", SID],
    ["--session-url", LINK, "--session", SID],
])
def test_notes_add_refuses_a_bad_hosted_source(extra):
    assert cli.main(["notes", "add", "--title", "t", "--summary", "s", *extra]) == 2
    assert not notes.db_path().exists()


def test_notes_add_for_claude_code_still_defaults_the_machine(fake_embed, capsys):
    assert cli.main(["notes", "add", "--title", "t", "--summary", "zebra machine",
                     "--session", "sess-1"]) == 0
    (hit,) = notes.search("zebra machine", mode="text")
    assert hit["source"]["kind"] == "claude-code" and hit["source"]["machine"]
