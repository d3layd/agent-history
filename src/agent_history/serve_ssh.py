"""`agent-history serve-ssh` — the command an ssh key is pinned to.

In the hub's authorized_keys a read-only key looks like::

    command="agent-history serve-ssh --tenants dennis",restrict ssh-ed25519 ...

sshd then runs this instead of whatever the client asked for, and hands the
client's request over in ``$SSH_ORIGINAL_COMMAND``. This module is the whole
gate: the request is split with ``shlex`` (never a shell), must start with
``search`` or ``show``, and must parse under the normal CLI parser — so a
flag that does not exist, an ``index``, a ``purge``, a stray ``;`` as the
command are all refused. Shell metacharacters inside a search are just query
text, because nothing here ever runs a shell.

Tenancy: the tenants a key may read are fixed by ``--tenants`` in the forced
command (the part only the hub's admin writes), never by anything the client
sends. Each tenant is its own index file; a request touches only the files of
tenants on that list, so asking for another tenant by name is refused rather
than filtered.
"""
from __future__ import annotations

import io
import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout

from . import config, embed, search, show, store

ALLOWED = ("search", "show")
REJECTED = 2


def _reject(why: str) -> int:
    print(f"serve-ssh: rejected: {why}", file=sys.stderr)
    return REJECTED


def parse_tenants(raw: str) -> list[str]:
    """Validate a ``--tenants`` list; ValueError on anything that is not a
    plain tenant name (no paths, no case tricks, no empties)."""
    names = [part.strip() for part in raw.split(",")]
    for name in names:
        if not config.valid_tenant(name):
            raise ValueError(f"invalid tenant name {name!r}")
    if len(set(names)) != len(names):
        raise ValueError("duplicate tenant in --tenants")
    return names


def run(tenants_arg: str | None = None, command: str | None = None) -> int:
    from . import cli

    try:
        tenants = parse_tenants(tenants_arg) if tenants_arg is not None else None
        for name in tenants or ():
            config.tenant_dir(name)  # also refuses a tenant dir that escapes the root
    except ValueError as exc:
        return _reject(str(exc))

    if command is None:
        command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _reject("unparseable command")
    if not tokens or tokens[0] not in ALLOWED:
        return _reject("only `search` and `show` are allowed")

    parser = cli._build_parser()
    noise = io.StringIO()  # argparse prints usage to stderr; keep the reply short
    try:
        with redirect_stderr(noise), redirect_stdout(noise):
            args = parser.parse_args(tokens)
    except SystemExit:
        return _reject("invalid arguments")
    if args.command not in ALLOWED:
        return _reject("only `search` and `show` are allowed")

    # Whatever the hub itself has configured, a request is answered from the
    # hub's own indexes, never forwarded onward.
    args.where = "local"

    if tenants is None:
        if args.tenant:
            return _reject("this hub has no tenants")
        return cli.dispatch(parser, args)

    if args.tenant and args.tenant not in tenants:
        return _reject("tenant not permitted")
    scope = [args.tenant] if args.tenant else tenants
    try:
        if args.command == "search":
            return _search(args, scope)
        return _show(args, scope)
    except (store.StoreError, embed.EmbedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _search(args, scope: list[str]) -> int:
    options = dict(k=args.k, cwd=args.cwd, agent=args.agent,
                   machine=args.machine, exclude_machine=args.exclude_machine,
                   mode=args.mode, sort=args.sort)
    lists = []
    for tenant in scope:
        with config.tenant_home(tenant):
            if not config.db_path().exists():
                continue  # nothing shipped for this tenant yet
            rows = search.search_rows(" ".join(args.query), **options)
        lists.append([{**row, "tenant": tenant} for row in rows])
    results = (lists[0] if len(lists) == 1
               else search.fuse(lists, sort=args.sort, k=args.k))
    return search.emit(results, full=args.full, as_json=args.as_json)


def _show(args, scope: list[str]) -> int:
    found: list[tuple[str, list[dict]]] = []
    for tenant in scope:
        with config.tenant_home(tenant):
            if not config.db_path().exists():
                continue
            rows = show.rows_for(args.target, around=args.around,
                                 limit=args.limit)
        if rows:
            found.append((tenant, rows))
    if len(found) > 1:
        names = ", ".join(t for t, _ in found)
        print(f"ambiguous: found in {names}; pass --tenant", file=sys.stderr)
        return 1
    if not found:
        return show.render(args.target, [], as_json=args.as_json)
    tenant, rows = found[0]
    rows = [{**row, "tenant": tenant} for row in rows]
    return show.render(args.target, rows, as_json=args.as_json,
                       tenant=tenant if len(scope) > 1 else None)
