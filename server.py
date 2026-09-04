"""swmg-nav MCP server (stdio): semantic + structural code intelligence.

Works from any clone/worktree: paths resolve relative to the checkout the
tool lives in. Clients: OpenCode, Claude Code, VS Code, Codex (all stdio MCP).

Tools:
- semantic_search(query, n=8): nearest files by embedding similarity
- find_functions(query, n=6): semantic search over individual functions
- symbol_graph(symbol, depth=1): callers/callees around a function or class
- dead_code(): functions unreachable from any entry point (candidates only)
- duplicates(): exact-clone function bodies (normalized hash groups)
- clusters(k, min_sim): subsystem clusters over the embedding space
- visualize(): generate the interactive 3D graph (graph.html) and return path
- rescan(): incremental re-index of everything above
"""

from __future__ import annotations

import sys
import time

from mcp.server.fastmcp import FastMCP

import graph
import nav

mcp = FastMCP("swmg-nav")


def _fmt(hits: list[nav.Hit]) -> str:
    if not hits:
        return "no results (index empty — call rescan first)"
    lines = []
    for h in hits:
        label = h.class_name or h.extends or h.ext
        lines.append(f"{h.score:0.3f}  res://{h.path}  [{label}]")
    return "\n".join(lines)


@mcp.tool()
def semantic_search(query: str, n: int = 8) -> str:
    """Find code/scene files in this Godot project by meaning, not keywords.

    Use before grep when hunting a concept: input handling, spell cooldowns,
    save system, netcode, bot AI, inventory. Returns ranked res:// paths —
    follow up with the Read tool on the best hits.
    """
    n = max(1, min(n, 25))
    return _fmt(nav.search(query, n))


@mcp.tool()
def find_functions(query: str, n: int = 6) -> str:
    """Semantic search over individual FUNCTIONS (not whole files).

    Use when you need the exact function implementing a concept, e.g.
    "apply spell damage", "spawn projectile", "reload inventory UI".
    Returns path::func with line numbers — pair with symbol_graph to see
    how a hit connects.
    """
    n = max(1, min(n, 15))
    hits = graph.find_functions(query, n)
    if not hits:
        return "no function index — call rescan first"
    return "\n".join(
        f"{h['score']:0.3f}  {h['path']}#{h['func']}:{h['line']}" for h in hits
    )


@mcp.tool()
def symbol_graph(symbol: str, depth: int = 1) -> str:
    """Structural map around a function or class: who calls it, what it calls.

    Wire-view of the repo: use it to trace call chains before refactoring,
    to check if removing a function is safe, or to understand a subsystem's
    shape. depth=2 gives one hop beyond direct neighbors. Pair with
    find_functions when you only know the concept, not the name.
    """
    depth = max(1, min(depth, 3))
    return graph.get_graph().symbol_graph(symbol, depth)


@mcp.tool()
def dead_code(n: int = 40) -> str:
    """Functions unreachable from any entry point — deletion candidates.

    Entry points: autoloads, virtuals (_ready/_process/...), signal handlers
    (code + .tscn connections), GUT tests, string-dispatched names. Tiers:
    'likely' (no dynamic dispatch in file — strong candidate) and 'review'
    (file uses call()/Callable()/connect() — verify manually). NEVER delete
    without reading the file and running tests.
    """
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
    return "\n".join(lines)


@mcp.tool()
def duplicates(n: int = 20) -> str:
    """Duplicated function bodies (exact, whitespace/comment-normalized).

    Simplification targets: same logic living twice. Groups with 3+ members
    first. Cross-file groups are refactoring gold (extract shared helper);
    same-file groups are quick wins.
    """
    n = max(1, min(n, 50))
    groups = graph.get_graph().exact_duplicates(limit=n)
    if not groups:
        return "no exact duplicates found"
    lines = [f"{len(groups)} duplicate group(s):", ""]
    for g in groups:
        lines.append(f"group {g['hash']} ({len(g['members'])} copies):")
        lines.extend(f"  - {m.replace('::', '#')}" for m in g["members"])
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def clusters(k: int = 6, min_sim: float = 0.6) -> str:
    """Subsystem clusters discovered from embedding geometry (mutual kNN).

    Shows which files belong to the same feature family — inventory, dungeon
    generation, VFX elements, netcode. Use to survey unfamiliar areas or find
    every file related to a system before refactoring it. Returns cluster
    sizes with member paths + class names.
    """
    k = max(2, min(k, 12))
    min_sim = max(0.4, min(min_sim, 0.85))
    cs = nav.clusters(k=k, min_sim=min_sim)
    if not cs:
        return "index empty — call rescan first"
    lines = [f"{len(cs)} cluster(s):", ""]
    for c in cs[:30]:
        label = ", ".join(cls for _, cls in c["paths"][:4] if cls) or "misc"
        lines.append(f"c{c['id']} ({c['size']} files, e.g. {label}):")
        for path, _cls in c["paths"][:12]:
            lines.append(f"  res://{path}")
        if c["size"] > 12:
            lines.append(f"  … +{c['size'] - 12} more")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def visualize() -> str:
    """Generate the interactive 3D code-graph (rotatable neuron map).

    Nodes = files (colored by subsystem cluster, red-tinted when they contain
    dead-code candidates), edges = calls/instancing/signals. Search box,
    cluster filter chips, dead-code toggle, click for connections.
    Returns the absolute path — open it in a browser. Regenerate after
    rescan if the graph changed materially.
    """
    import viz

    out = viz.generate()
    return f"3D graph written to {out} — open in a browser (double-click or `start {out}`)"


def _sync_chain(stats: dict) -> tuple[object, object, str]:
    """nav.rescan -> graph rebuild -> fns sync. fns failures degrade
    gracefully (dirty marker self-heals next run) instead of killing the
    server or blocking file search."""
    g = graph.get_graph(rebuild=True)
    note = ""
    try:
        fns = graph.sync_functions(
            stats.get("changed", []), stats.get("deleted_paths", [])
        )
    except Exception as e:  # fns stale-but-healing; file search unaffected
        fns = {"fns_upserted": 0}
        note = f" [fns sync FAILED, will self-heal next rescan: {e}]"
    return g, fns, note


@mcp.tool()
def rescan() -> str:
    """Re-index changed/new/deleted files: vectors, function index, graph.

    Fast when nothing changed. Run after pulling, branching, or mass edits.
    """
    t0 = time.perf_counter()
    stats = nav.rescan()
    g, fns, note = _sync_chain(stats)
    dt = time.perf_counter() - t0
    return (
        f"rescan: files {stats['added']}/{stats['updated']}/"
        f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d), "
        f"fns {fns['fns_upserted']} upserted, graph {len(g.files)} files, "
        f"in {dt:.1f}s{note}"
    )


if __name__ == "__main__":
    t0 = time.perf_counter()
    stats = nav.rescan()
    g, fns, _ = _sync_chain(stats)
    print(
        f"swmg-nav: startup files {stats['added']}/{stats['updated']}/"
        f"{stats['unchanged']}/{stats['deleted']}, "
        f"fns {fns['fns_upserted']}, "
        f"in {time.perf_counter() - t0:.1f}s",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")
