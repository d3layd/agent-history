"""Path and environment resolution — the single source of truth.

Every path the tool touches is resolved here, so nothing else in the codebase
needs to know where things live. Each `AGENT_HISTORY_*` variable falls back to
the legacy `CLAUDE_HISTORY_*` name (with a one-time deprecation warning) so
existing installs keep working across the rename.
"""
from __future__ import annotations

import contextlib
import os
import re
import socket
import sys
from pathlib import Path

WINDOWS = os.name == "nt"

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
    if xdg:
        return Path(xdg).expanduser() / APP_NAME
    if WINDOWS:
        # %LOCALAPPDATA% is where per-machine application data belongs on
        # Windows; ~/.local/share would be a Unix habit imported badly.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / APP_NAME
    return Path.home() / ".local/share" / APP_NAME


def ensure_data_home() -> Path:
    """Create the data directory owner-only.

    The index holds the full text of every indexed conversation — the same
    content the agents themselves store at 0600. Creating it under the default
    0022 umask would leave it 0755/0644 and readable by every local account, so
    the mode is set explicitly rather than left to the umask.
    """
    path = data_home()
    path.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def secure_file(path: Path) -> None:
    """Restrict a file we created to owner-only access.

    POSIX only. Windows has no equivalent mode bits, so the index inherits the
    directory's ACL there — see the README's privacy section.
    """
    if WINDOWS:
        return
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


def ship_lock_path() -> Path:
    return data_home() / ".ship.lock"


def ship_state_path() -> Path:
    """What `ship` has already sent (hashes, backfill watermark)."""
    return data_home() / "ship.db"


def outbox_dir() -> Path:
    """Batches written by `ship` and not yet uploaded."""
    return data_home() / "outbox"


def jobs_dir() -> Path:
    """Where the claude -p job runner keeps one directory per job
    (``<jobs>/<job_id>/job.json`` and ``transcript.jsonl``)."""
    raw = (_env("AGENT_HISTORY_JOBS_DIR", "") or "").strip()
    return Path(raw).expanduser() if raw else Path("/var/lib/claude-jobs/jobs")


def machine_id() -> str:
    """Name of this machine in a shared index: $AGENT_HISTORY_MACHINE or the
    short hostname. Kept to filename-safe characters because the hub uses the
    shipper's name as a directory."""
    raw = (_env("AGENT_HISTORY_MACHINE", "") or "").strip()
    if not raw:
        raw = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip(".-") or "unknown"


def _truthy(name: str) -> bool:
    return (_env(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def hub_ingest() -> str | None:
    """rsync destination `ship` uploads to, e.g. ``ahub@host:``. The hub forces
    the directory (rrsync), so the remote path is relative and usually empty."""
    return (_env("AGENT_HISTORY_HUB_INGEST", "") or "").strip() or None


def hub_key() -> str | None:
    """Optional ssh identity file for uploads."""
    return (_env("AGENT_HISTORY_HUB_KEY", "") or "").strip() or None


def hub_mode() -> bool:
    """This install is the hub: only the inbox is indexed, redaction forced."""
    return _truthy("AGENT_HISTORY_HUB")


def inbox_dir() -> Path | None:
    """Where shippers' batches land, as ``<inbox>/<machine>/*.jsonl``."""
    raw = (_env("AGENT_HISTORY_INBOX", "") or "").strip()
    return Path(raw).expanduser() if raw else None


_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def valid_tenant(name: str) -> bool:
    return bool(_TENANT.match(name))


def tenants_root() -> Path:
    """Directory holding one data home per tenant on a multi-tenant hub."""
    raw = (_env("AGENT_HISTORY_TENANTS", "") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "tenants"


def tenant_dir(name: str) -> Path:
    """The data home of tenant ``name`` — the only way a tenant name becomes a
    path, so the name is validated and the result must sit directly under the
    tenants root."""
    if not valid_tenant(name):
        raise ValueError(f"invalid tenant name {name!r}")
    root = tenants_root().resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError(f"invalid tenant name {name!r}")
    return path


@contextlib.contextmanager
def tenant_home(name: str):
    """Point every data path at one tenant's index for the duration.

    One index file per tenant, not a tenant column: separation is structural,
    so a bug in a query cannot cross tenants because the other tenant's
    database is never opened.
    """
    path = tenant_dir(name)
    previous = os.environ.get("AGENT_HISTORY_HOME")
    os.environ["AGENT_HISTORY_HOME"] = str(path)
    try:
        yield path
    finally:
        if previous is None:
            os.environ.pop("AGENT_HISTORY_HOME", None)
        else:
            os.environ["AGENT_HISTORY_HOME"] = previous


def remote_url() -> str | None:
    """``ssh://user@host[:port]`` of the hub to search, if configured."""
    return (_env("AGENT_HISTORY_REMOTE", "") or "").strip() or None


def remote_key() -> str | None:
    """Optional ssh identity file for hub searches."""
    return (_env("AGENT_HISTORY_REMOTE_KEY", "") or "").strip() or None


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


def strict_path() -> Path:
    """Presence enables strict/compliance mode."""
    return data_home() / "strict.on"


def strict_enabled() -> bool:
    """Compliance posture for regulated (e.g. CUI) work.

    In strict mode injection defaults OFF and, when explicitly enabled,
    retrieves only from the current project (same cwd) — closing the
    cross-project injection path. Storage-at-rest controls still apply;
    strict mode narrows behaviour, it does not make the index not-sensitive.
    Set with `AGENT_HISTORY_STRICT=1` or `agent-history strict on`.
    """
    env = (_env("AGENT_HISTORY_STRICT", "") or "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    return strict_path().exists()


def redact_enabled() -> bool:
    """Secret redaction for the local index.

    Off by default: on a single machine the index is often the only place a
    credential can still be found, and recalling it is a feature. On with
    `AGENT_HISTORY_REDACT=on`, or implied by strict mode. Anything that leaves
    the machine (bundles, a hub) is redacted regardless of this setting; the
    hub itself never indexes anything unredacted.
    """
    if hub_mode():
        return True
    env = (_env("AGENT_HISTORY_REDACT", "") or "").strip().lower()
    if env in {"1", "on", "true", "yes"}:
        return True
    if env in {"0", "off", "false", "no"}:
        return False
    if env and "AGENT_HISTORY_REDACT" not in _warned:
        # A typo here would silently leave secrets in the index.
        _warned.add("AGENT_HISTORY_REDACT")
        print(f"agent-history: AGENT_HISTORY_REDACT={env!r} not recognised; "
              "use on or off", file=sys.stderr)
    return strict_enabled()


def inject_off_path() -> Path:
    """Presence disables injection. Absence = on (the default)."""
    return data_home() / "inject.off"


def inject_env_override() -> bool | None:
    """The `AGENT_HISTORY_INJECT` override, or None if unset.

    When set it wins over the saved markers — so callers that change markers
    can tell the user the change won't take effect until the env var is unset.
    """
    env = (_env("AGENT_HISTORY_INJECT", "") or "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    return None


def inject_marker_pref() -> bool:
    """What the saved markers say, ignoring the env override.

    Normal mode is default-on (off-marker disables); strict mode is
    default-off (on-marker enables) — injecting cross-context is exactly what
    strict guards against, so it must be turned on deliberately there.
    """
    if strict_enabled():
        return inject_on_path().exists()
    return not inject_off_path().exists()


def inject_enabled() -> bool:
    """Effective injection state: env override if set, else the marker."""
    override = inject_env_override()
    return override if override is not None else inject_marker_pref()


def inject_on_path() -> Path:
    """Presence enables injection under strict mode (default-off there)."""
    return data_home() / "inject.on"


def inject_max_distance() -> float:
    """Reject retrieved chunks farther than this L2 distance from the prompt.

    Calibrated on a mature nomic-embed-text index: on-topic top hits measured
    14-16, nonsense prompts ~20. Other embedding models scale differently —
    recalibrate when changing AGENT_HISTORY_MODEL.
    """
    raw = _env("AGENT_HISTORY_INJECT_MAX_DIST", "17.0") or "17.0"
    try:
        return float(raw)
    except ValueError:
        return 17.0


def inject_cross_distance() -> float:
    """Tighter cutoff for chunks OUTSIDE the current directory/project.

    Injection prefers the current directory: same-directory chunks pass
    `inject_max_distance()`, but a chunk from another project must clear this
    stricter bar to ride into an unrelated prompt — "near-exact only." On the
    same nomic-embed-text calibration (on-topic top hits 14-16), the 14.0
    default admits only strong cross-project matches. Should be <= the main
    cutoff; recalibrate with the model.
    """
    raw = _env("AGENT_HISTORY_INJECT_CROSS_DIST", "14.0") or "14.0"
    try:
        return min(float(raw), inject_max_distance())
    except ValueError:
        return min(14.0, inject_max_distance())


def codex_session_dirs() -> list[Path]:
    """Codex CLI rollout roots: ~/.codex/sessions plus the WSL Windows side."""
    explicit = _env("AGENT_HISTORY_CODEX")
    if explicit:
        return [Path(explicit).expanduser()]
    dirs = [Path.home() / ".codex/sessions"]
    # Select the Windows profile by the Codex marker, not the Claude one — a
    # profile that only ever ran Codex must still be discoverable.
    dirs += [home / ".codex/sessions"
             for home in wsl_windows_homes(marker=".codex/sessions")]
    return dirs


def opencode_db_paths() -> list[Path]:
    """OpenCode stores everything in one SQLite file under its data dir."""
    explicit = _env("AGENT_HISTORY_OPENCODE_DB")
    if explicit:
        return [Path(explicit).expanduser()]
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local/share"
    return [base / "opencode/opencode.db"]


def hermes_state_dbs() -> list[Path]:
    """Hermes keeps one state.db per profile (~/.hermes, ~/.hermes-<profile>).

    Its sessions/ directories hold only a gateway routing index — the
    transcripts live exclusively in these SQLite files.
    """
    explicit = _env("AGENT_HISTORY_HERMES_DIRS")
    if explicit:
        homes = [Path(p).expanduser() for p in explicit.split(os.pathsep) if p.strip()]
    else:
        homes = sorted(Path.home().glob(".hermes*"))
    return [home / "state.db" for home in homes if (home / "state.db").is_file()]


def openclaw_dirs() -> list[Path]:
    """OpenClaw state roots (~/.openclaw), holding agents/*/sessions JSONL."""
    explicit = _env("AGENT_HISTORY_OPENCLAW_DIRS")
    if explicit:
        return [Path(p).expanduser() for p in explicit.split(os.pathsep) if p.strip()]
    return [Path.home() / ".openclaw"]


def docs_dirs_file() -> Path:
    """Persisted docs roots, one per line — managed by `agent-history docs`.

    Hook processes do not reliably inherit shell exports (Windows
    especially), so a file in the data directory is the sturdier home for
    this setting; the env var still works and is merged in first.
    """
    return data_home() / "docs-dirs"


def docs_dirs() -> list[Path]:
    """Documentation trees to index — opt-in only, nothing is auto-detected.

    A docs tree could live anywhere, and guessing would mean reading
    directories the user never offered. Union of $AGENT_HISTORY_DOCS_DIRS
    (os.pathsep-separated) and the roots added with `agent-history docs add`.
    """
    raw = _env("AGENT_HISTORY_DOCS_DIRS", "")
    dirs = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    try:
        lines = docs_dirs_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    dirs += [Path(line).expanduser() for line in lines if line.strip()]
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def extra_dirs() -> list[Path]:
    """Additional transcript roots.

    Separated by os.pathsep — ':' on Unix, ';' on Windows. Splitting on ':'
    everywhere would tear 'C:\\Users\\me' in half.
    """
    raw = _env("AGENT_HISTORY_EXTRA_DIRS", "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def wsl_enabled() -> bool:
    """Whether to look for Windows-side transcripts from inside WSL."""
    flag = _env("AGENT_HISTORY_NO_WSL", "")
    return flag.strip().lower() not in {"1", "true", "yes"}


def wsl_windows_homes(_users_dir: Path | None = None,
                      marker: str = ".claude/projects") -> list[Path]:
    """The Windows user profile to index from WSL, if it is unambiguous.

    /mnt/c/Users holds every account on the machine, and WSL cannot reliably
    tell which one belongs to the person running this. Indexing all of them
    would pull other people's conversations into a private index, so a profile
    is auto-selected only when exactly one candidate has transcripts. When
    several do, none is chosen and the caller is told to name one explicitly
    via AGENT_HISTORY_EXTRA_DIRS.

    `marker` is the per-agent directory that identifies a candidate profile —
    each adapter passes its own, so a profile that only ever ran one agent is
    still found by that agent's adapter.

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
            and (p / marker).is_dir()
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
            f"want, e.g. /mnt/c/Users/<you>/{marker}",
            file=sys.stderr,
        )
    return []
