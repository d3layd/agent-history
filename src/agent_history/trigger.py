"""Automatic indexing, invoked by editor hooks and schedulers.

This used to be a bash script, which meant Windows needed a second
implementation that would drift from the first. Doing it in Python instead
gives one behaviour on every platform:

- detach, so a SessionEnd hook never delays the editor closing
- serialise, because the indexer writes a single SQLite file
- skip cheaply when nothing changed, so idle schedules cost nothing
"""
from __future__ import annotations

import contextlib
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config

DETACH_ENV = "AGENT_HISTORY_DETACHED"
# A run that finds the lock held gives up quickly rather than queueing. The
# work is not lost: the holder stamps at the start, so the next --if-changed
# run sees anything written since and picks it up. Waiting minutes inside an
# editor hook would be worse than skipping.
LOCK_TIMEOUT = 5


# --------------------------------------------------------------------- detach

def _relaunch_detached(argv: list[str]) -> None:
    """Re-run ourselves in the background and return immediately."""
    env = dict(os.environ, **{DETACH_ENV: "1"})
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "close_fds": True,
    }
    if config.WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survives the parent
        # console going away, which is exactly what a SessionEnd hook does.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "agent_history", *argv], **kwargs)


# ----------------------------------------------------------------------- lock

@contextlib.contextmanager
def _exclusive(path: Path, timeout: int = LOCK_TIMEOUT):
    """Cross-platform advisory lock over a file.

    Yields True when held, False when another run already owns it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    acquired = False
    deadline = time.time() + timeout
    try:
        if config.WINDOWS:
            import msvcrt

            while time.time() < deadline:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            while time.time() < deadline:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.1)
        yield acquired
    finally:
        if acquired:
            try:
                if config.WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ------------------------------------------------------------- change detection

def _anything_changed(stamp: Path) -> bool:
    """Whether any transcript is newer than the last run's stamp."""
    if not stamp.exists():
        return True
    since = stamp.stat().st_mtime
    from . import adapters

    for adapter in adapters.available_adapters():
        for root in adapter.roots():
            for pattern in ("*.jsonl", "*.md"):
                for path in root.rglob(pattern):
                    try:
                        if path.stat().st_mtime > since:
                            return True
                    except OSError:
                        continue
    return False


# ------------------------------------------------------------------------ run

def run(label: str, *, if_changed: bool = False, foreground: bool = False,
        lock_timeout: int = LOCK_TIMEOUT) -> int:
    if not foreground and os.environ.get(DETACH_ENV) != "1":
        argv = ["trigger", label]
        if if_changed:
            argv.append("--if-changed")
        _relaunch_detached(argv)
        return 0

    data = config.ensure_data_home()
    stamp = config.stamp_path()
    log = config.log_path()

    # Cheap pre-check: bail before touching Ollama when nothing is new.
    if if_changed and not _anything_changed(stamp):
        return 0

    with _exclusive(config.lock_path(), timeout=lock_timeout) as acquired:
        if not acquired:
            return 0

        # Stamp the start, not the end, so anything written during the run is
        # picked up next time rather than missed.
        stamp.touch()
        config.secure_file(stamp)

        from . import index

        with open(log, "a", encoding="utf-8") as handle:
            stamp_line = datetime.datetime.now().isoformat(timespec="seconds")
            handle.write(f"\n[{stamp_line}] {label} trigger (pid={os.getpid()})\n")
            handle.flush()
            original = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = handle
            try:
                code = index.run()
            except Exception as exc:  # noqa: BLE001 - a hook must never crash loudly
                handle.write(f"index failed: {exc}\n")
                code = 1
            finally:
                sys.stdout, sys.stderr = original
        config.secure_file(log)
        return code
