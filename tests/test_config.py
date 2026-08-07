"""Path and environment resolution, including the legacy fallback."""
from __future__ import annotations

import os


import pytest

from agent_history import config


def test_explicit_home_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HISTORY_HOME", str(tmp_path / "explicit"))
    assert config.data_home() == tmp_path / "explicit"


def test_home_defaults_to_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HISTORY_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.data_home() == tmp_path / "xdg" / "agent-history"


def test_legacy_env_name_still_works(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AGENT_HISTORY_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HISTORY_HOME", str(tmp_path / "legacy"))
    config._warned.clear()
    assert config.data_home() == tmp_path / "legacy"
    assert "deprecated" in capsys.readouterr().err


def test_new_env_name_takes_precedence_over_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HISTORY_HOME", str(tmp_path / "new"))
    monkeypatch.setenv("CLAUDE_HISTORY_HOME", str(tmp_path / "old"))
    assert config.data_home() == tmp_path / "new"


def test_deprecation_warning_is_emitted_once(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AGENT_HISTORY_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HISTORY_HOME", str(tmp_path / "legacy"))
    config._warned.clear()
    config.data_home()
    capsys.readouterr()
    config.data_home()
    assert capsys.readouterr().err == ""


def test_extra_dirs_use_the_platform_separator(monkeypatch, tmp_path):
    """':' would tear 'C:\\Users\\me' in half on Windows."""
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("AGENT_HISTORY_EXTRA_DIRS", f"{a}{os.pathsep}{b}")
    assert config.extra_dirs() == [a, b]


def test_extra_dirs_ignores_empty_segments(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_EXTRA_DIRS", os.pathsep * 2)
    assert config.extra_dirs() == []


@pytest.mark.skipif(config.WINDOWS, reason="Windows has no POSIX mode bits")
def test_data_home_is_created_owner_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HISTORY_HOME", str(tmp_path / "secure"))
    path = config.ensure_data_home()
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(config.WINDOWS, reason="Windows has no POSIX mode bits")
def test_secure_file_restricts_mode(tmp_path):
    f = tmp_path / "index.db"
    f.write_text("x")
    f.chmod(0o644)
    config.secure_file(f)
    assert f.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True),
                                            ("YES", True), ("0", False),
                                            ("", False)])
def test_allow_remote_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("AGENT_HISTORY_ALLOW_REMOTE", value)
    assert config.allow_remote() is expected


# --- WSL profile disambiguation -------------------------------------------

def _fake_users(tmp_path, names_with_transcripts, bare_names=()):
    users = tmp_path / "Users"
    for n in names_with_transcripts:
        (users / n / ".claude/projects").mkdir(parents=True)
    for n in bare_names:
        (users / n).mkdir(parents=True)
    return users


def test_single_windows_profile_is_selected(monkeypatch, tmp_path):
    users = _fake_users(tmp_path, ["dana"], ["Public", "Default"])
    monkeypatch.delenv("AGENT_HISTORY_NO_WSL", raising=False)
    result = config.wsl_windows_homes(_users_dir=users)
    assert [p.name for p in result] == ["dana"]


def test_several_profiles_selects_none(monkeypatch, tmp_path, capsys):
    users = _fake_users(tmp_path, ["alice", "bob"])
    monkeypatch.delenv("AGENT_HISTORY_NO_WSL", raising=False)
    assert config.wsl_windows_homes(_users_dir=users) == []
    err = capsys.readouterr().err
    assert "alice" in err and "bob" in err


def test_system_profiles_are_ignored(monkeypatch, tmp_path):
    users = _fake_users(tmp_path, ["alice", "Public", "TEMP.MSI"])
    monkeypatch.delenv("AGENT_HISTORY_NO_WSL", raising=False)
    assert [p.name for p in config.wsl_windows_homes(_users_dir=users)] == ["alice"]


def test_no_wsl_flag_disables_detection(monkeypatch, tmp_path):
    users = _fake_users(tmp_path, ["dana"])
    monkeypatch.setenv("AGENT_HISTORY_NO_WSL", "1")
    assert config.wsl_windows_homes(_users_dir=users) == []


def test_absent_users_dir_is_not_an_error(tmp_path):
    assert config.wsl_windows_homes(_users_dir=tmp_path / "nope") == []


@pytest.mark.skipif(not config.WINDOWS, reason="Windows-only behaviour")
def test_data_home_prefers_localappdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_HISTORY_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert config.data_home() == tmp_path / "AppData" / "agent-history"
