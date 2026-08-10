"""Command line interface."""
from __future__ import annotations

import argparse
import datetime
import sys

from . import (__version__, adapters, config, embed, index, inject, search,
               show, store, trigger, viewer)

COMMANDS = {"index", "reindex", "search", "sources", "doctor", "datadir",
            "trigger", "show", "serve", "inject", "docs"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-history",
        description="Local semantic search over AI coding agent transcripts.",
    )
    parser.add_argument(
        "--version", action="version", version=f"agent-history {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("index", help="incrementally update the index")
    sub.add_parser("reindex", help="wipe and rebuild the index")
    sub.add_parser("sources", help="print the directories that will be scanned")
    sub.add_parser("datadir", help="print the data directory (index, log, lock)")
    sub.add_parser("doctor", help="diagnose the installation")

    trig = sub.add_parser(
        "trigger", help="index in the background (used by editor hooks)")
    trig.add_argument("label", nargs="?", default="manual",
                      help="what triggered this, recorded in the log")
    trig.add_argument("--if-changed", action="store_true", dest="if_changed",
                      help="do nothing unless a transcript changed")
    trig.add_argument("--foreground", action="store_true",
                      help="wait for indexing instead of detaching")

    find = sub.add_parser("search", help="search the index")
    find.add_argument("query", nargs="+")
    find.add_argument("-k", type=int, default=8, help="results to return (default 8)")
    find.add_argument("--cwd", help="filter by substring of the session's directory")
    find.add_argument("--agent", help="filter by agent, e.g. claude-code")
    find.add_argument("--mode", choices=search.MODES, default="hybrid",
                      help="hybrid (default), vector, or text (no Ollama needed)")
    find.add_argument("--full", action="store_true", help="print full chunk text")
    find.add_argument("--json", action="store_true", dest="as_json")

    ctx = sub.add_parser(
        "show", help="print the conversation a search hit belongs to")
    ctx.add_argument("target",
                     help="an origin path or session id, as printed by search")
    ctx.add_argument("--around", metavar="TIMESTAMP",
                     help="centre the window on this timestamp instead of the end")
    ctx.add_argument("--limit", type=int, default=40,
                     help="chunks to print (default 40)")
    ctx.add_argument("--json", action="store_true", dest="as_json")

    serve = sub.add_parser(
        "serve", help="local web viewer on 127.0.0.1 (loopback only)")
    serve.add_argument("--port", type=_port, default=viewer.DEFAULT_PORT,
                       help="1-65535, or 0 for an OS-assigned port")

    inj = sub.add_parser(
        "inject",
        help="opt-in: auto-inject relevant past context into new prompts")
    inj.add_argument("action", nargs="?", default="status",
                     choices=["on", "off", "status", "run"],
                     help="'run' is the hook entry point and reads stdin")

    doc = sub.add_parser(
        "docs", help="manage documentation roots to index (persisted)")
    doc.add_argument("action", nargs="?", default="list",
                     choices=["add", "remove", "list"])
    doc.add_argument("path", nargs="?", help="directory, for add/remove")
    return parser


def _port(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port: {value}")
    return number


def _normalise(argv: list[str]) -> list[str]:
    """Allow `agent-history "query"` as shorthand for `search "query"`."""
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        return ["search", *argv]
    return argv


def _cmd_sources() -> int:
    """Print scan roots, one per line — consumed by scripts/trigger-index.sh."""
    for adapter in adapters.available_adapters():
        for root in adapter.roots():
            print(root)
    return 0


def _cmd_docs(action: str, path: str | None) -> int:
    from pathlib import Path

    listing = config.docs_dirs_file()

    def stored() -> list[str]:
        try:
            return [line for line in
                    listing.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except OSError:
            return []

    if action in ("add", "remove") and not path:
        print(f"error: docs {action} needs a directory path", file=sys.stderr)
        return 1

    if action == "add":
        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            print(f"error: not a directory: {target}", file=sys.stderr)
            return 1
        lines = stored()
        if str(target) in lines:
            print(f"already added: {target}")
            return 0
        config.ensure_data_home()
        listing.write_text("\n".join([*lines, str(target)]) + "\n",
                           encoding="utf-8")
        config.secure_file(listing)
        print(f"added: {target}")
        print("run `agent-history index` (or wait for the next hook) to index it")
        return 0

    if action == "remove":
        target = str(Path(path).expanduser().resolve())
        lines = stored()
        if target not in lines:
            print(f"not in the persisted list: {target}", file=sys.stderr)
            if any(str(d) == target for d in config.docs_dirs()):
                print("(it comes from $AGENT_HISTORY_DOCS_DIRS — unset it there)",
                      file=sys.stderr)
            return 1
        remaining = [line for line in lines if line != target]
        config.ensure_data_home()
        listing.write_text(
            "".join(f"{line}\n" for line in remaining), encoding="utf-8")
        config.secure_file(listing)
        print(f"removed: {target}")
        print("already-indexed chunks remain until the file's next edit "
              "refreshes them or a `reindex`")
        return 0

    roots = config.docs_dirs()
    if not roots:
        print("no docs roots configured — add one with: "
              "agent-history docs add <dir>")
        return 0
    persisted = set(stored())
    for root in roots:
        source = "persisted" if str(root) in persisted else "env"
        state = "" if root.is_dir() else "  (MISSING)"
        print(f"{root}  [{source}]{state}")
    return 0


def _cmd_doctor() -> int:
    print(f"data home:  {config.data_home()}")
    print(f"index:      {config.db_path()}")
    print(f"ollama:     {config.ollama_url()}")
    print(f"model:      {config.model()}")

    ok, detail = embed.reachable()
    print(f"ollama up:  {'yes' if ok else 'NO'} ({detail})")

    if not store.extensions_supported():
        print("sqlite-vec: UNAVAILABLE — this Python cannot load SQLite extensions")
        print()
        print(store.EXTENSION_HELP)
        return 1
    print("sqlite-vec: loadable")

    detected = adapters.available_adapters()
    if detected:
        for adapter in detected:
            roots = ", ".join(str(r) for r in adapter.roots())
            print(f"adapter:    {adapter.name} -> {roots}")
    else:
        print("adapter:    none detected")

    # Auto-detected adapters are rightly silent when absent, but DOCS_DIRS is
    # an explicit statement of intent — a typo there must not just quietly
    # index nothing.
    for missing in (d for d in config.docs_dirs() if not d.is_dir()):
        print(f"docs:       WARNING configured root does not exist: {missing}")

    if not config.db_path().exists():
        print("index:      not built yet — run: agent-history index")
        return 0 if ok else 1

    try:
        db = store.open_for_read()
    except store.StoreError as exc:
        print(f"index:      ERROR {exc}")
        return 1

    info = store.stats(db)
    print(f"chunks:     {info['total']}")
    for agent, count in info["per_agent"].items():
        print(f"              {agent}: {count}")
    print(f"built with: {info['model']} ({info['dimension']}-dim)")

    stamp = config.stamp_path()
    if stamp.exists():
        when = datetime.datetime.fromtimestamp(stamp.stat().st_mtime)
        print(f"last index: {when.isoformat(timespec='seconds')}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    # Hooks and cron redirect stdout to a log file, which makes Python
    # block-buffer it. Without this, a long index writes nothing for minutes and
    # looks hung.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = _build_parser()
    args = parser.parse_args(_normalise(list(sys.argv[1:] if argv is None else argv)))

    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "index":
            return index.run()
        if args.command == "reindex":
            return index.run(reindex=True)
        if args.command == "sources":
            return _cmd_sources()
        if args.command == "trigger":
            return trigger.run(args.label, if_changed=args.if_changed,
                               foreground=args.foreground)
        if args.command == "datadir":
            print(config.data_home())
            return 0
        if args.command == "doctor":
            return _cmd_doctor()
        if args.command == "search":
            return search.run(
                " ".join(args.query),
                k=args.k,
                cwd=args.cwd,
                agent=args.agent,
                mode=args.mode,
                full=args.full,
                as_json=args.as_json,
            )
        if args.command == "show":
            return show.run(args.target, around=args.around,
                            limit=args.limit, as_json=args.as_json)
        if args.command == "serve":
            return viewer.run(port=args.port)
        if args.command == "inject":
            if args.action == "run":
                return inject.run_hook()
            if args.action in ("on", "off"):
                return inject.set_enabled(args.action == "on")
            return inject.status()
        if args.command == "docs":
            return _cmd_docs(args.action, args.path)
    except (store.StoreError, embed.EmbedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
