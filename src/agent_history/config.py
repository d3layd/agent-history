"""Path and environment resolution — the single source of truth.

Every path the tool touches is resolved here, so nothing else in the codebase
needs to know where things live. Each `AGENT_HISTORY_*` variable falls back to
the legacy `CLAUDE_HISTORY_*` name (with a one-time deprecation warning) so
existing installs keep working across the rename.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "agent-history"

# Windows profile directories that never contain a real user's sessions.
_WSL_SKIP = {"Default", "Default User", "Public", "All Users", "desktop.ini"}

_warned: set[str] = set()


def _env(name: str, default: str | None = None) -> str | None:
    """Read AGENT_HISTORY_FOO, falling back to the legacy CLAUDE_HISTORY_FOO."""
    value = os.environ.get(name)
    if value is not None:
        return value

    legacy = name.replace("AGENT_HISTORY_", "CLAUDE_HISTORY_", 1)
    if legacy != name:
        value = os.environ.get(legacy)
        if value is not None:
            if legacy not in _warned:
                _warned.add(legacy)
                print(
                    f"warning: ${legacy} is deprecated; rename it to ${name}",
                    file=sys.stderr,
                )
            return value
    return default


def data_home() -> Path:
    """Directory holding the index, log, and run-state files."""
    explicit = _env("AGENT_HISTORY_HOME")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local/share"
    return base / APP_NAME


def ensure_data_home() -> Path:
    """Create the data directory owner-only.

    The index holds the full text of every indexed conversation — the same
    content the agents themselves store at 0600. Creating it under the default
    0022 umask would leave it 0755/0644 and readable by every local account, so
    the mode is set explicitly rather than left to the umask.
    """
    path = data_home()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def secure_file(path: Path) -> None:
    """Restrict a file we created to owner-only access."""
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError:
        pass


def db_path() -> Path:
    return data_home() / "index.db"


def log_path() -> Path:
    return data_home() / "index.log"


def stamp_path() -> Path:
    return data_home() / ".last-index"


def lock_path() -> Path:
    return data_home() / ".index.lock"


def ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def model() -> str:
    return _env("AGENT_HISTORY_MODEL", "nomic-embed-text")


def allow_remote() -> bool:
    """Whether sending transcript text to a non-loopback endpoint is permitted."""
    flag = _env("AGENT_HISTORY_ALLOW_REMOTE", "")
    return flag.strip().lower() in {"1", "true", "yes"}


def claude_projects_dir() -> Path:
    explicit = _env("AGENT_HISTORY_PROJECTS")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".claude/projects"


def extra_dirs() -> list[Path]:
    """Additional transcript roots, colon-separated."""
    raw = _env("AGENT_HISTORY_EXTRA_DIRS", "")
    return [Path(p).expanduser() for p in raw.split(":") if p.strip()]


def wsl_enabled() -> bool:
    """Whether to look for Windows-side transcripts from inside WSL."""
    flag = _env("AGENT_HISTORY_NO_WSL", "")
    return flag.strip().lower() not in {"1", "true", "yes"}


def wsl_windows_homes(_users_dir: Path | None = None) -> list[Path]:
    """The Windows user profile to index from WSL, if it is unambiguous.

    /mnt/c/Users holds every account on the machine, and WSL cannot reliably
    tell which one belongs to the person running this. Indexing all of them
    would pull other people's conversations into a private index, so a profile
    is auto-selected only when exactly one candidate has transcripts. When
    several do, none is chosen and the caller is told to name one explicitly
    via AGENT_HISTORY_EXTRA_DIRS.

    Returns an empty list on native Linux and macOS, where /mnt/c is absent.

    `_users_dir` overrides the profile root; it exists so tests can exercise the
    disambiguation without a real Windows mount.
    """
    if not wsl_enabled():
        return []
    users = _users_dir if _users_dir is not None else Path("/mnt/c/Users")
    if not users.is_dir():
        return []
    try:
        candidates = [
            p
            for p in sorted(users.iterdir())
            if p.is_dir()
            and p.name not in _WSL_SKIP
            and not p.name.startswith("TEMP")
            and (p / ".claude/projects").is_dir()
        ]
    except OSError:
        return []

    if len(candidates) == 1:
        return candidates
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        print(
            f"note: several Windows profiles have transcripts ({names}); "
            "skipping them all. Set AGENT_HISTORY_EXTRA_DIRS to the one you "
            "want, e.g. /mnt/c/Users/<you>/.claude/projects",
            file=sys.stderr,
        )
    return []
