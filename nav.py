"""nav CLI — the command surface of the split index (issue #344).

nav.py was the 1676-line monolith mixing config resolution, chroma
store management, the embed client, the rescan/import/export walk and
this CLI; the leaves now live as their own modules and every importer
migrated (clean cutover — no re-export shell by law):

  - navconfig — config resolution, BOM reads, .neuroignore, #91 contract,
    config_scope (the rebindable config globals live there),
  - navstore — chroma collections (stamps/heals, bounded reads), the
    embed transport, hybrid recall + clusters queries, the process-wide
    store singletons (locks, clients, memos),
  - navindex — the walk, the stat gate, rescan, tracked base import/
    export, build-observability hooks.

What remains here is the CLI orchestration (``nav.py <cmd>``) and the
entry the docs and scripts pin: ``python nav.py --config <path> cmd``.
Config resolution law (issue #27 — the install is read-only at
onboarding time; config travels with the project):
  1. $NEURONAV_CONFIG env var (explicit, always wins),
  2. ``<cwd>/.neuronav/config.json`` (project-local; ``onboard.py init``
     writes it, ``onboard.py wire`` scaffolds + wires MCP),
  3. ``config.json`` next to this file, but ONLY when cwd IS the checkout
     (legacy machine-local default for the install's own target),
  4. no config: pure defaults — root = cwd, include ``.``, extensions =
     every registered extractor suffix, state = ``<root>/.neuronav``.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import chromadb.errors  # NotFoundError is the only swallowable failure (#298)

import navconfig
import navindex
import navstore

_NO_ARG_COMMANDS = ("rescan", "count", "export-base", "import-base",
                    "crosstalk", "drop")  # `search` alone takes free text


def _reject_trailing(cmd: str, extra: list[str]) -> None:
    """Loud rejection of unconsumed trailing argv (issue #332): the
    no-argument subcommands used to ignore extra argv silently, so
    `nav.py rescan --config X` ran the PURE-DEFAULTS rescan — walking
    the cwd and writing <cwd>/.neuronav, the #91 wipe-door class
    reachable by an argument-order slip. Name the token and the correct
    global-prefix form instead; no positional tolerance, no fallback."""
    print(
        f"nav.py {cmd}: unrecognized argument(s): {' '.join(extra)}\n"
        "`--config <path>` is a global prefix, not a trailing flag — "
        f"use: nav.py --config <path> {cmd}",
        file=sys.stderr,
    )
    sys.exit(2)


def _cli(argv: list[str] | None = None) -> None:
    """CLI dispatch (nav.py <cmd>); argv override for in-process tests (#298)."""
    # issue #119: a direct run piped through a cp1252/ascii console raises
    # UnicodeEncodeError the moment a hit path is non-ASCII — the CLI is a
    # console program, so force UTF-8 output regardless of the locale (the
    # -X utf8 flag only arrives when launched via a wired onboard entry)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--config":
        # switch to a second config (self-index etc.) before running:
        # rebind globals + set NEURONAV_CONFIG so sibling modules (graph.py,
        # clusters.py) and subprocesses resolve the same root/collection
        if len(argv) < 3:
            print("usage: nav.py --config <path> <command>", file=sys.stderr)
            sys.exit(2)
        cfg_file = Path(argv[1])
        if not cfg_file.is_file():
            print(f"config not found: {cfg_file}", file=sys.stderr)
            sys.exit(2)
        navconfig.use_config(cfg_file)
        argv = argv[2:]
    cmd = argv[0] if argv else "rescan"
    if cmd in _NO_ARG_COMMANDS and len(argv) > 1:  # issue #332
        _reject_trailing(cmd, argv[1:])
    if cmd == "rescan":
        t0 = time.perf_counter()
        s = navindex.rescan()
        dt = time.perf_counter() - t0
        print(f"{s} in {dt:.1f}s, total={navstore.count()}")
    elif cmd == "search":
        # #298 D1: join the sliced argv — sys.argv still carries the
        # "--config <cfg> search" prefix, feeding the config path INTO
        # the query (reproduced: 12 spurious bm25 hits on "json"/"search")
        for h in navstore.search(" ".join(argv[1:])):
            ctx = f"  ctx=[{', '.join(h['ctx'])}]" if h["ctx"] else ""
            print(f"{h['score']:0.4f}  {h['file']}  src={h['src']}{ctx}")
    elif cmd == "count":
        print(navstore.count())
    elif cmd == "export-base":
        print(json.dumps(navindex.export_base(), indent=2))
    elif cmd == "import-base":
        print(json.dumps(navindex.import_base(), indent=2))
    elif cmd == "crosstalk":
        # coupling-hotspot report: cross-cluster structural edges
        import clusters as _clusters
        import graph as _graph

        rep = _clusters.crosstalk(navstore.clusters(), _graph.get_graph())
        print(_clusters.fmt_crosstalk(rep, align=True))
    elif cmd == "drop":
        navstore._memo_drop_current()  # the store is going away
        cl = navstore.client()
        for name in (navconfig.COLLECTION, navstore.fns_name()):
            try:
                cl.delete_collection(name)
                print(f"dropped {name}")
            except chromadb.errors.NotFoundError:
                print(f"{name}: not present")
            except Exception as e:
                # #298: a Windows file lock / IO error is NOT "not present" —
                # swallowing it here reads as a successful drop and the user
                # debugs a store that was never dropped
                raise RuntimeError(
                    f"drop: deleting collection {name!r} failed: {e}"
                ) from e
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _cli()
