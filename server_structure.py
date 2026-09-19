"""Query family: structural walk tools (issue #345).

symbol_graph / impact / dead_code / duplicates — verbatim moves out of
server.py; server.py owns the gate rails and passes them via register()
(the pinning suites reach them through server's namespace — see
server_search.register). _capped lives in server_clusters (its heaviest
user); this module imports it from there — sibling leaf import, no
cycle (clusters never imports this module).
"""

from __future__ import annotations

import graph
from servercore import READONLY, mcp, _capped


# symbol_graph output shaping (issue #125): the walk stays in graph.py
# (resolution + BFS), but its rendering truncated silently twice — rows
# capped at 8 names with no count, response cut at 40 LINES, i.e.
# mid-node. Rendering here follows explore's _flow line law instead:
# full counts on every row, "+N more" past the name cap, node-boundary
# cut + marker past the node cap. An agent checking "is removing this
# function safe?" must never read 8 callers as the total when there are 20.
SYMBOL_MAX_NODES = 13   # nodes per response (3 lines each + marker keeps
                        # the old 40-line discipline)
SYMBOL_ROW_NAMES = 8    # caller/callee names per row before "+N more"


def _sg_short(key: str) -> str:
    path, _, name = key.partition("::")
    return f"{path}#{name}"


def _sg_row(label: str, keys: list[str]) -> str:
    shown = _capped(
        [_sg_short(k) for k in keys], SYMBOL_ROW_NAMES
    ) or "-"
    return f"    {label}: {len(keys)} ({shown})"


def _symbol_view(g, symbol: str, depth: int) -> str:
    """graph.symbol_graph's walk rendered with visible truncation (issue
    #125): same resolution and BFS, but every row carries its true count
    and the response says when it cut. A total miss suggests difflib
    closest matches instead of dead-ending — context()'s precedent."""
    keys = g._resolve(symbol)
    if not keys:
        return _miss_view(g, symbol)

    blocks: list[str] = []
    seen: set[str] = set()
    frontier = set(keys)
    for _ in range(depth):
        nxt: set[str] = set()
        for key in sorted(frontier):
            if key in seen:
                continue
            seen.add(key)
            callers = sorted(g.reverse.get(key, ()))
            callees = sorted(g.edges.get(key, ()))
            blocks.append(
                f"{_sg_short(key)}\n"
                + _sg_row("callers", callers)
                + "\n"
                + _sg_row("callees", callees)
            )
            nxt |= {
                c for c in callees + callers
                if not c.endswith(graph.TSCN_SUFFIX)
            }
        frontier = nxt - seen
        if not frontier:
            break
    out = blocks[:SYMBOL_MAX_NODES]
    if len(blocks) > SYMBOL_MAX_NODES:
        out.append(
            f"… truncated at {SYMBOL_MAX_NODES} of {len(blocks)} nodes — "
            "pass depth=1 or a narrower symbol"
        )
    return "\n".join(out)


def _miss_view(g, symbol: str) -> str:
    """A resolution miss rendered as closest-match suggestions, not a
    dead end — the context() precedent _symbol_view set (issue #125);
    impact shares the shape (issue #280)."""
    import difflib

    names = sorted({n for fs in g.files.values() for n in fs.funcs})
    by_lower = {n.lower(): n for n in names}
    close = difflib.get_close_matches(
        symbol.lower(), sorted(by_lower), n=3, cutoff=0.4
    )
    sug = (
        f" Closest matches: {', '.join(by_lower[c] for c in close)}"
        if close
        else ""
    )
    return f"no function matching '{symbol}'{sug}"


def _impact_view(g, symbol: str, direction: str, max_depth: int) -> str:
    """graph.impact's payload rendered under the #125 line law: the
    total is always the full closure size, per-hop rows carry true
    counts with "+N more" past the name cap, and a depth cut announces
    "+N more past the depth cap" instead of stopping silently."""
    res = g.impact(symbol, direction=direction, max_depth=max_depth)
    if res is None:
        return _miss_view(g, symbol)
    what = (
        "callers — what breaks"
        if res["direction"] == "callers"
        else "callees — what it depends on"
    )
    seeds = _capped([_sg_short(k) for k in res["seeds"]], 3)
    out = [
        f"impact of {res['symbol']} ({seeds}): {what}",
        f"total: {res['total']} within {res['max_depth']} hops",
    ]
    for hop, keys in enumerate(res["by_depth"], start=1):
        out.append(_sg_row(f"depth {hop}", keys))
    if res["beyond"]:
        cap = res["max_depth"]
        hint = (
            f"pass max_depth={cap + 1} to expand"
            if cap < graph.IMPACT_MAX_DEPTH
            else f"max_depth is capped at {graph.IMPACT_MAX_DEPTH}"
        )
        out.append(
            f"    … +{res['beyond']} more past the depth cap — {hint}"
        )
    if res["direction"] == "callers" and res["total"]:
        if res["entries"]:
            out.append(_sg_row("entries reached", res["entries"]))
        else:
            out.append(
                "    entries reached: 0 — no known entry (test / "
                "registration / export) anchors these callers; verify "
                "dispatch manually"
            )
    return "\n".join(out)

@mcp.tool(annotations=READONLY)
def symbol_graph(symbol: str, depth: int = 1, dir: str = "") -> str:
    """Structural map around a function or class: who calls it, what it calls.

    Wire-view of the repo: use it to trace call chains before refactoring,
    to check if removing a function is safe, or to understand a subsystem's
    shape. depth=2 gives one hop beyond direct neighbors. Pair with
    find_functions when you only know the concept, not the name.

    Resolution (issue #125, now documented): exact function name, then
    class name (all its methods), then case-insensitive substring — up to
    10 roots. Every row shows its true count with the first 8 names
    ("+N more" past that); the response caps at 13 nodes with a
    truncation marker; a total miss suggests closest matches.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        depth = max(1, min(depth, 3))
        return _symbol_view(graph.get_graph(), symbol, depth)


@mcp.tool(annotations=READONLY)
def impact(symbol: str, direction: str = "callers", max_depth: int = 4,
           dir: str = "") -> str:
    """Transitive blast radius of a function or class — the pre-refactor check.

    Everything that transitively calls it (direction="callers": what
    BREAKS if it changes, moves, or disappears) or everything it
    transitively calls (direction="callees": what it depends on). Run it
    before renaming or removing a hub: the response carries the full
    closure total plus a per-hop histogram (depth 1: N, depth 2: M, ...)
    for the falloff shape, and caller chains anchored at known entries
    (tests, registrations, exports) are listed as "entries reached" —
    a chain that dead-ends reads as such instead. Counts never truncate
    silently (issue #125): "+N more" past 8 names per row, "+N more
    past the depth cap" when the walk stops early. max_depth caps at 8;
    a bad direction answers with guidance, not an error.

    Same resolution as symbol_graph (exact name, then class methods,
    then substring — up to 10 seeds): use symbol_graph for the 1-hop
    detail view, dead_code for the no-caller verdict.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        try:
            return _impact_view(
                graph.get_graph(), symbol, direction, max_depth
            )
        except ValueError as e:
            return str(e)


@mcp.tool(annotations=READONLY)
def dead_code(n: int = 100, dir: str = "") -> str:
    """Functions unreachable from any entry point — deletion candidates.

    Entry points: autoloads, virtuals (_ready/_process/...), signal handlers
    (code + .tscn connections), GUT tests, string-dispatched names. Tiers:
    'likely' (no dynamic dispatch in file — strong candidate) and 'review'
    (file uses call()/Callable()/connect() — verify manually). NEVER delete
    without reading the file and running tests.

    Lists at most n rows (default 100, clamp 1..100); 'likely' rows come
    first, so a cut is never silent — the footer names how many of each
    tier you got (issue #266).

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        n = max(1, min(n, 100))
        g = graph.get_graph()
        res = g.dead_code(limit=n)
        lines = [
            f"dead-code candidates: {res['total']} total  "
            f"(likely: {res['by_tier'].get('likely', 0)}, "
            f"review: {res['by_tier'].get('review', 0)})",
            res["note"],
            "",
        ]
        for d in res["candidates"]:
            lines.append(f"[{d['tier']:6}] {d['path']}:{d['line']}  {d['func']}")
        shown = res["candidates"]
        if len(shown) < res["total"]:
            n_lik = sum(1 for d in shown if d["tier"] == "likely")
            n_rev = sum(1 for d in shown if d["tier"] == "review")
            lines.append("")
            lines.append(
                f"… truncated at {len(shown)} rows: showing "
                f"{n_lik} of {res['by_tier'].get('likely', 0)} likely + "
                f"{n_rev} of {res['by_tier'].get('review', 0)} review"
                " — pass n= for the rest"
            )
        return "\n".join(lines)

@mcp.tool(annotations=READONLY)
def duplicates(n: int = 20, dir: str = "") -> str:
    """Duplicated function bodies (exact, whitespace/comment-normalized),
    across ALL indexed languages — the extractor registry is the roster,
    so every language the walk indexes gets scanned (issue #116: the
    scan was never GDScript-only; no repo gets a false clean bill).
    Comment stripping follows each language's own comment syntax where
    the normalizer implements it; elsewhere comments compare as body
    text.

    Simplification targets: same logic living twice. Groups with 3+ members
    first. Cross-file groups are refactoring gold (extract shared helper);
    same-file groups are quick wins.

    Pure-delegate groups (a null-guard + single forwarding call — thin
    wrappers around a shared helper) are skipped, not reported as
    duplication; the footer counts them (issue #268).

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        n = max(1, min(n, 50))
        g = graph.get_graph()
        rep = g.duplicates_report(limit=n)
        groups = rep["groups"]
        skipped = rep["delegate_skipped"]
        skip_note = (
            f"{skipped} pure-delegate group(s) skipped"
            " — thin delegates, not duplicated logic"
        )
        if not groups:
            scanned = sum(1 for fs in g.files.values() if fs.funcs)
            tail = f"; {skip_note}" if skipped else ""
            return f"no exact duplicates found ({scanned} files with functions scanned{tail})"
        lines = [f"{len(groups)} duplicate group(s):", ""]
        for grp in groups:
            lines.append(f"group {grp['hash']} ({len(grp['members'])} copies):")
            lines.extend(f"  - {m.replace('::', '#')}" for m in grp["members"])
            lines.append("")
        if len(groups) < rep["groups_total"]:
            lines.append(
                f"… truncated at {len(groups)} of {rep['groups_total']}"
                " groups — pass n= for the rest"
            )
        if skipped:
            lines.append(skip_note)
        return "\n".join(lines)


def register(_route, _auto_rescan):
    """Compose this family onto the FastMCP instance (issue #345) —
    same contract as server_search.register: rails as parameters bound
    into module globals, historical def order, uniform read-only
    annotation."""
    g = globals()
    for _rail in ("_route", "_auto_rescan"):
        g[_rail] = locals()[_rail]
    return symbol_graph, impact, dead_code, duplicates
