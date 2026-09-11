"""neuronav MCP server (stdio): semantic + structural code intelligence.

Works from any clone/worktree: paths resolve relative to the checkout the
tool lives in. Clients: OpenCode, Claude Code, VS Code, Codex (all stdio MCP).

Tools:
- explore(query, n=4): START HERE for "how does X work" — one call returns
  line-numbered source slices + callers/callees flow for the best hits;
- repo_map(budget_tokens=2048): token-budget repo map — files ranked by
  structural PageRank with key signatures, tree-grouped by dir; the cheap
  orientation preamble to call before any search
- semantic_search(query, n=8): hybrid recall — vector + BM25F ranks fused,
  hits carry src provenance and 1-hop ctx neighbors
- find_functions(query, n=6): semantic search over individual functions
- symbol_graph(symbol, depth=1): callers/callees around a function or class
- search_text(pattern, glob="", files_only=False): regex text search over
  the indexed files — grep-class queries (exact strings, TODOs, literals),
  Zoekt-style caps: 20 files / 3 lines each, truncation markers + totals
- dead_code(): functions unreachable from any entry point (candidates only)
- duplicates(): exact-clone function bodies (normalized hash groups)
- clusters(k, min_sim): subsystem clusters over the embedding space
- crosstalk(): cross-cluster coupling-hotspot report
- context(path, depth=1): subsystem map for one file (cluster, structural
  + semantic neighbors, hub rank) — the fresh-agent orientation tool
- visualize(): generate the interactive 3D graph (graph.html) and return path
- rescan(): incremental re-index of everything above
- read tools auto-rescan first when the worktree drifted (cheap stat
  fingerprint, TTL-cached); config watch_interval_s > 0 additionally
  polls and rescans without waiting for tool calls
"""

from __future__ import annotations

import fnmatch
import re
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import explore as _explore
import graph
import nav

mcp = FastMCP("neuronav")

# below except rescan is pure read over the local index
READONLY = ToolAnnotations(readOnlyHint=True)


def _fmt(hits: list[dict]) -> str:
    """Format hybrid-recall hits: RRF-fused score, src provenance
    (vec/bm25/both), bidirectional 1-hop ctx labels."""
    if not hits:
        return "no results (index empty — call rescan first)"
    lines: list[str] = []
    if hits[0].get("degraded"):
        lines.append("degraded: BM25F-only (vector index unavailable)")
    for h in hits:
        label = h.get("class_name") or h.get("extends") or h.get("ext") or ""
        tag = f"  [{label}]" if label else ""
        ctx = ", ".join(h.get("ctx") or [])
        lines.append(f"{h['score']:0.4f}  {h['file']}  src={h['src']}  ctx=[{ctx}]{tag}")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def explore(query: str, n: int = 4) -> str:
    """One-call orientation for "how does X work" questions.

    Seeds on the function-level vector index (lexical fallback when the
    embedding backend is down), returns Read-equivalent `cat -n` source
    slices with real line numbers, plus a callers/callees flow line per
    hit. Weak hits become pointer lines instead of noise; total output is
    budget-capped so nothing externalizes to a file mid-answer.
    """
    _auto_rescan()
    return _explore.run(query, n)


MAX_MAP_BUDGET = 8192
MIN_MAP_BUDGET = 256


def _here(g) -> str:
    """One-line you-are-here header: which checkout, how big, how many
    subsystems — stamped on orientation-tool responses so a client can
    always tell which project it is talking to."""
    n_clusters = len(nav.clusters())
    return f"you are here: {nav.ROOT.as_posix()} — {len(g.files)} files, {n_clusters} clusters"


@mcp.tool(annotations=READONLY)
def repo_map(budget_tokens: int = 2048) -> str:
    """Token-budget repo map — the cheap orientation preamble.

    Aider-style: files ranked by structural PageRank (edge weight = wire
    count), each with its key signatures, tree-grouped by directory,
    truncated at the token budget. Call this first to learn the layout,
    then context(path) on any file that matters.
    """
    _auto_rescan()
    budget = max(MIN_MAP_BUDGET, min(budget_tokens, MAX_MAP_BUDGET))
    g = graph.get_graph()
    return _here(g) + "\n" + graph.repo_map(budget_tokens=budget)


@mcp.tool(annotations=READONLY)
def semantic_search(query: str, n: int = 8) -> str:
    """Find files in this repo by meaning, not keywords.

    Hybrid recall: vector similarity fused with lexical BM25F ranks —
    src=vec|bm25|both says which side found each hit, ctx= lists up to 3
    structural neighbors worth a look while you are there. Use before
    grep when hunting a concept: input handling, timed effects, save
    system, netcode, AI behavior, item storage.
    """
    _auto_rescan()
    n = max(1, min(n, 25))
    return _here(graph.get_graph()) + "\n" + _fmt(nav.search(query, n))


@mcp.tool(annotations=READONLY)
def find_functions(query: str, n: int = 6) -> str:
    """Semantic search over individual FUNCTIONS (not whole files).

    Use when you need the exact function implementing a concept, e.g.
    "apply status damage", "spawn projectile", "refresh item UI".
    Returns path::func with line numbers — pair with symbol_graph to see
    how a hit connects.
    """
    _auto_rescan()
    n = max(1, min(n, 15))
    hits = graph.find_functions(query, n)
    if not hits:
        return "no function index — call rescan first"
    return "\n".join(
        f"{h['score']:0.3f}  {h['path']}#{h['func']}:{h['line']}" for h in hits
    )


# Zoekt-style cap discipline (issue #68): summarized, capped search output
# beats verbose paging (SWE-agent 12.0% -> 18.0% on SWE-bench Lite) —
# hard caps + truncation markers + total counts keep the answer
# token-bounded and tell the agent when to narrow.
SEARCH_MAX_FILES = 20
SEARCH_MAX_LINES = 3
SEARCH_LINE_CHARS = 200


@mcp.tool(annotations=READONLY)
def search_text(pattern: str, glob: str = "", files_only: bool = False) -> str:
    """Regex text search over the indexed files — the grep-class tool.

    Exact strings and regex the semantic+symbol tools structurally miss:
    literals, TODOs, error messages, config keys. Rows are
    file:line:matched-line, ordered by path then line, hard-capped at
    20 files / 3 lines each (Zoekt-style) with per-file and global
    truncation markers plus the total match count — when the cap fires,
    narrow with glob= or a tighter pattern.
    """
    _auto_rescan()
    if not pattern:
        return "no pattern given — pass a regex, e.g. search_text('TODO')"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"invalid regex {pattern!r}: {exc}"
    g = graph.get_graph()
    if not g.files:
        return "no results (index empty — call rescan first)"
    matched: list[tuple[str, int, list[tuple[int, str]]]] = []
    scanned = 0
    total = 0
    for path in sorted(g.files):  # deterministic: path, then line order
        if glob and not fnmatch.fnmatch(path, glob):
            continue
        scanned += 1
        try:
            text = nav._read_text(nav.ROOT / path)
        except OSError:
            continue  # vanished mid-walk; the next rescan reconciles
        hits = [
            (i, ln.rstrip())
            for i, ln in enumerate(text.splitlines(), 1)
            if rx.search(ln)
        ]
        if not hits:
            continue
        total += len(hits)
        matched.append((path, len(hits), hits[:SEARCH_MAX_LINES]))
    if not matched:
        return f"no matches for {pattern!r} in {scanned} indexed files"
    lines: list[str] = [_here(g)]
    for path, n, hits in matched[:SEARCH_MAX_FILES]:
        if files_only:
            lines.append(path)
            continue
        for i, ln in hits:
            ln = ln if len(ln) <= SEARCH_LINE_CHARS else ln[:SEARCH_LINE_CHARS] + "…"
            lines.append(f"{path}:{i}:{ln}")
        if n > SEARCH_MAX_LINES:
            lines.append(f"… and {n - SEARCH_MAX_LINES} more matches in {path}")
    n_files = len(matched)
    plural = "" if n_files == 1 else "s"
    if n_files > SEARCH_MAX_FILES:
        lines.append(
            f"… truncated at {SEARCH_MAX_FILES} files: {total} matches in "
            f"{n_files} file{plural} — narrow the pattern or pass glob="
        )
    else:
        lines.append(f"{total} matches in {n_files} file{plural}")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def symbol_graph(symbol: str, depth: int = 1) -> str:
    """Structural map around a function or class: who calls it, what it calls.

    Wire-view of the repo: use it to trace call chains before refactoring,
    to check if removing a function is safe, or to understand a subsystem's
    shape. depth=2 gives one hop beyond direct neighbors. Pair with
    find_functions when you only know the concept, not the name.
    """
    _auto_rescan()
    depth = max(1, min(depth, 3))
    return graph.get_graph().symbol_graph(symbol, depth)


@mcp.tool(annotations=READONLY)
def dead_code(n: int = 40) -> str:
    """Functions unreachable from any entry point — deletion candidates.

    Entry points: autoloads, virtuals (_ready/_process/...), signal handlers
    (code + .tscn connections), GUT tests, string-dispatched names. Tiers:
    'likely' (no dynamic dispatch in file — strong candidate) and 'review'
    (file uses call()/Callable()/connect() — verify manually). NEVER delete
    without reading the file and running tests.
    """
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
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def duplicates(n: int = 20) -> str:
    """Duplicated function bodies (exact, whitespace/comment-normalized).

    Simplification targets: same logic living twice. Groups with 3+ members
    first. Cross-file groups are refactoring gold (extract shared helper);
    same-file groups are quick wins.
    """
    _auto_rescan()
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


@mcp.tool(annotations=READONLY)
def clusters(k: int = 6, min_sim: float = 0.6) -> str:
    """Subsystem clusters discovered from embedding geometry (mutual kNN).

    Shows which files belong to the same feature family — UI, core systems,
    asset handling, networking. Use to survey unfamiliar areas or find
    every file related to a system before refactoring it. Returns cluster
    sizes with member paths + class names.
    """
    _auto_rescan()
    k = max(2, min(k, 12))
    min_sim = max(0.4, min(min_sim, 0.85))
    cs = nav.clusters(k=k, min_sim=min_sim)
    if not cs:
        return "index empty — call rescan first"
    lines = [f"{len(cs)} cluster(s):", ""]
    for c in cs[:30]:
        label = c.get("label") or "misc"
        meta = f" [{c.get('method')}, conf {c.get('confidence', 0):.2f}]"
        lines.append(f"c{c['id']} {label} — {c['size']} files{meta}:")
        for path, _cls in c["paths"][:12]:
            lines.append(f"  res://{path}")
        if c["size"] > 12:
            lines.append(f"  … +{c['size'] - 12} more")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def crosstalk() -> str:
    """Coupling-hotspot report: which subsystem clusters are wired together.

    Counts structural (call/signal/var/instance) edges that CROSS cluster
    boundaries. Use before splitting/merging modules: a cluster with high
    external share is not self-contained; heavy cluster pairs are coupling
    hotspots. Pairs with `clusters` (what the families are) — this reports
    how leaky the boundaries are.
    """
    _auto_rescan()
    import clusters as _clusters

    g = graph.get_graph()
    rep = _clusters.crosstalk(nav.clusters(), g)
    lines = [
        f"crosstalk: {rep['clusters']} clusters, "
        f"internal {rep['internal_edges']} edges, "
        f"cross-cluster {rep['external_edges']} "
        f"({rep['external_ratio'] * 100:.1f}% of clustered)",
        "",
        "per cluster (top 10 by external):",
    ]
    for r in rep["by_cluster"][:10]:
        lines.append(
            f"  [{r['id']:>2}] {r['label'][:34]}  n={r['size']}  "
            f"internal {r['internal']}  out {r['external_out']}  "
            f"in {r['external_in']}  ext {r['external_share'] * 100:.0f}%"
        )
    if rep["worst_pairs"]:
        lines += ["", "worst pairs:"]
        for wp in rep["worst_pairs"]:
            tops = ", ".join(f"{t['pair']} x{t['w']}" for t in wp["top_files"][:2])
            lines.append(f"  {wp['a']} <-> {wp['b']}: {wp['edges']} edges (top: {tops})")
    return "\n".join(lines)


def _ctx_file_of(key: str) -> str:
    return key.rsplit("::", 1)[0]


def _ctx_adjacency(g) -> tuple[dict, dict]:
    """File-level adjacency (both directions, per edge-type counts) and
    cross-file in-degree, aggregated once from the func-level edge set."""
    adj: dict[str, dict[str, dict]] = {}  # file -> nb -> {"->": t:n, "<-": t:n}
    indeg: dict[str, int] = {}
    for (s, d), tys in g.edge_types.items():
        sf, df = _ctx_file_of(s), _ctx_file_of(d)
        if sf == df:
            continue
        cell = adj.setdefault(sf, {}).setdefault(df, {">": {}, "<": {}})
        for t in tys:
            cell[">"][t] = cell[">"].get(t, 0) + 1
        cell = adj.setdefault(df, {}).setdefault(sf, {">": {}, "<": {}})
        for t in tys:
            cell["<"][t] = cell["<"].get(t, 0) + 1
        indeg[df] = indeg.get(df, 0) + len(tys)
    return adj, indeg


def _ctx_types(counts: dict[str, int]) -> str:
    return ", ".join(f"{t} x{n}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _ctx_semantic(path: str, k: int = 6) -> list[tuple[float, str]]:
    """Nearest files by embedding cosine — query with the file's own
    stored vector (no embed call, no new deps)."""
    try:
        col = nav._collection()
        got = col.get(ids=[path], include=["embeddings"])
        if not got["ids"]:
            return []
        res = col.query(
            query_embeddings=[got["embeddings"][0]],
            n_results=k + 1,
            include=["distances"],
        )
        return [
            (round(1.0 - float(d), 3), fid)
            for fid, d in zip(res["ids"][0], res["distances"][0])
            if fid != path
        ][:k]
    except Exception:
        return []


def _ctx_overview(g) -> str:
    """All-clusters overview: label, size, top members, external edges."""
    import clusters as _clusters

    cs = nav.clusters()
    _adj, indeg = _ctx_adjacency(g)
    ct = _clusters.crosstalk(cs, g)
    ext = {b["id"]: b["external_out"] + b["external_in"] for b in ct["by_cluster"]}
    clustered = {pp for c in cs for pp, _ in c["paths"]}
    unclustered = max(0, len(g.files) - len(clustered))
    lines = [
        f"clusters overview — {len(cs)} clusters, {len(g.files)} files "
        f"({unclustered} unclustered), "
        f'{ct["internal_edges"]} internal / {ct["external_edges"]} external edges '
        f'({ct["external_ratio"]:.0%} external)'
    ]
    for c in sorted(cs, key=lambda c: c["size"], reverse=True):
        top = sorted(
            ((indeg.get(pp, 0), pp) for pp, _ in c["paths"]), reverse=True
        )[:3]
        top_s = ", ".join(f"res://{pp} (in {v})" for v, pp in top)
        lines.append(
            f'c{c["id"]} "{c["label"]}" — {c["size"]} files '
            f'[{c["method"]}, conf {c["confidence"]:.2f}] '
            f'ext={ext.get(c["id"], 0)}'
        )
        if top_s:
            lines.append(f"    top: {top_s}")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def context(path: str = "", depth: int = 1) -> str:
    """Subsystem map for one repo file — the orientation tool for agents.

    Fresh-agent entry point: pass a res:// path (or repo-relative) and get
    a text map — its cluster (label, confidence, member hubs by in-degree),
    structural neighbors grouped by edge type (call/signal/var/attach/inst
    with counts and direction, depth 1-3), top semantic neighbors (embedding
    cosine), and hub status (in-degree rank). Called with no path, returns
    the all-clusters overview instead (label, size, top members, external
    edges). Build from existing clusters + graph + vector index; no new deps.
    """
    _auto_rescan()
    depth = max(1, min(depth, 3))
    p = path.strip()
    g = graph.get_graph()
    if not p:
        return _ctx_overview(g)
    if p.startswith("res://"):
        p = p[len("res://"):]
    p = p.replace("\\", "/").lstrip("/")
    if p not in g.files:
        import difflib

        close = difflib.get_close_matches(p, list(g.files), n=3, cutoff=0.4)
        sug = f" Closest matches: {', '.join(close)}" if close else ""
        return f"unknown file: {p} — pass a repo-relative or res:// path, or rescan first.{sug}"
    fs = g.files[p]
    adj, indeg = _ctx_adjacency(g)
    lines: list[str] = []
    if fs.class_name and fs.extends:
        tag = f"{fs.class_name} extends {fs.extends}"
    else:
        tag = fs.class_name or fs.extends or fs.ext
    lines.append(f"res://{p}  [{tag}]")

    cs = nav.clusters()
    mine = next((c for c in cs if any(pp == p for pp, _ in c["paths"])), None)
    if mine is None:
        lines.append("cluster: unclustered")
    else:
        members = sorted(
            ((indeg.get(pp, 0), pp, cc) for pp, cc in mine["paths"]), reverse=True
        )
        my_rank = next(i for i, (_v, pp, _c) in enumerate(members, 1) if pp == p)
        lines.append(
            f'cluster: c{mine["id"]} "{mine["label"]}" (conf {mine["confidence"]:.2f}, '
            f'{mine["method"]}) — {mine["size"]} files, '
            f"this file ranks #{my_rank} by in-degree"
        )
        lines.append(f"  members (top {min(12, len(members))} by in-degree):")
        for v, pp, cc in members[:12]:
            cls = f" ({cc})" if cc else ""
            lines.append(f"    {v:>3}  res://{pp}{cls}")
        if len(members) > 12:
            lines.append(f"    … +{len(members) - 12} more")

    lines.append(f"structural neighbors (depth {depth}):")
    if p not in adj:
        lines.append("  none — isolated file")
    else:
        direct = adj[p]
        entries = sorted(
            (
                (
                    sum(cell[">"].values()) + sum(cell["<"].values()),
                    nb,
                    cell[">"],
                    cell["<"],
                )
                for nb, cell in direct.items()
            ),
            key=lambda e: -e[0],
        )
        for _w, nb, out_t, in_t in entries[:15]:
            parts = []
            if out_t:
                parts.append("-> " + _ctx_types(out_t))
            if in_t:
                parts.append("<- " + _ctx_types(in_t))
            lines.append(f"  res://{nb}  {'  '.join(parts)}")
        if len(entries) > 15:
            lines.append(f"  … +{len(entries) - 15} more")
        if depth >= 2:
            seen = {p} | set(direct)
            hop2: dict[str, str] = {}
            for nb in direct:
                for nb2 in adj.get(nb, {}):
                    if nb2 not in seen and nb2 not in hop2:
                        hop2[nb2] = nb
            if hop2:
                h2 = sorted(hop2.items(), key=lambda kv: -indeg.get(kv[0], 0))[:10]
                lines.append(f"  2-hop ({len(hop2)} files, top {len(h2)} by in-degree):")
                for f2, via in h2:
                    lines.append(f"    res://{f2}  via res://{via}")

    lines.append("semantic neighbors (cosine):")
    sem = _ctx_semantic(p)
    if not sem:
        lines.append("  n/a (file not embedded — rescan first)")
    else:
        for s, fid in sem:
            lines.append(f"  {s:.3f}  res://{fid}")

    ind = indeg.get(p, 0)
    ranked = sorted(g.files, key=lambda f: -indeg.get(f, 0))
    rank = ranked.index(p) + 1
    total = len(ranked)
    if ind:
        lines.append(
            f"hub: in-degree {ind} — rank {rank} of {total} files "
            f"(top {100.0 * rank / total:.0f}%)"
        )
    else:
        lines.append(f"hub: in-degree 0 — rank {rank} of {total} files (leaf)")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def visualize() -> str:
    """Generate the interactive 3D code-graph (rotatable neuron map).

    Nodes = files (colored by subsystem cluster, red-tinted when they contain
    dead-code candidates), edges = calls/instancing/signals. Search box,
    cluster filter chips, dead-code toggle, click for connections.
    Returns the absolute path — open it in a browser. Regenerate after
    rescan if the graph changed materially.
    """
    _auto_rescan()
    try:
        import viz
    except ImportError:
        return ("viz add-on not installed — delete-able surface is viz.py + vendor/ + "
                "tools/serve.py; core tools (search/repo_map/context/...) work without it. "
                "Restore viz.py to re-enable the bake.")

    out = viz.generate()
    return f"3D graph written to {out} — open in a browser (double-click or `start {out}`)"


# ---- auto-rescan freshness gate (issue #19) --------------------------------
# Every read tool calls _auto_rescan() on entry: nav's stat fingerprint
# (mtime/size walk, TTL-cached) is compared against the last synced
# baseline, and a drifted worktree triggers the sha-gated nav.rescan() +
# graph/fns sync before the tool answers. Failures never crash the call:
# one stderr warning, a RESCAN_COOLDOWN_S retry suppression, and the
# tool answers from the current index (the recall degraded-mode
# precedent). Config watch_interval_s > 0 adds a daemon thread that
# polls the same fingerprint and rescans without tool traffic.

RESCAN_COOLDOWN_S = 60.0  # auto-rescan retry suppression after a failure
WATCH_DEBOUNCE_S = 2.0  # writer-quiet window before a watcher rescan
WATCH_DEBOUNCE_MAX_S = 30.0  # cap on waiting for the writer to quiet down

_rescan_busy = threading.Lock()  # in-flight trigger (cross-process is nav._db_lock's job)
_rescan_failed_at: float | None = None  # monotonic; None = healthy


def _cooldown_active() -> bool:
    return (
        _rescan_failed_at is not None
        and time.monotonic() - _rescan_failed_at < RESCAN_COOLDOWN_S
    )


def _auto_rescan() -> None:
    """Read-tool freshness gate: stat-scan -> dirty ? incremental rescan +
    graph/fns sync + baseline update. Never raises."""
    global _rescan_failed_at
    if _cooldown_active() or not _rescan_busy.acquire(blocking=False):
        return  # failed recently, or another trigger is already mid-rescan
    try:
        try:
            if not nav.stat_scan():
                return
            stats = nav.rescan()
            if stats["added"] or stats["updated"] or stats["deleted"]:
                _sync_chain(stats)
                print(
                    f"neuronav: auto-rescan: files {stats['added']}/{stats['updated']}/"
                    f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d)",
                    file=sys.stderr,
                )
            nav.stat_mark_synced()
            _rescan_failed_at = None
        except Exception as e:  # embedding backend down etc: degrade loudly
            _rescan_failed_at = time.monotonic()
            print(
                f"neuronav: auto-rescan FAILED ({e}); answering from the current "
                f"index, retry suppressed for {RESCAN_COOLDOWN_S:.0f}s",
                file=sys.stderr,
            )
    finally:
        _rescan_busy.release()


def _wait_quiet() -> None:
    """Sleep until the fingerprint stops changing (bounded): rescan a
    coherent tree, not a writer's half-saved state."""
    last = nav.stat_fingerprint()
    deadline = time.monotonic() + WATCH_DEBOUNCE_MAX_S
    while time.monotonic() < deadline:
        time.sleep(WATCH_DEBOUNCE_S)
        cur = nav.stat_fingerprint()
        if cur == last:
            return
        last = cur


def _watch_loop(interval: float) -> None:
    """Poll the stat fingerprint every interval; on a dirty transition let
    the writer go quiet, then run the shared gate (which re-checks
    dirtiness and cooldown before rescanning)."""
    while True:
        time.sleep(interval)
        try:
            dirty = nav.stat_scan(force=True)
        except Exception as e:
            print(f"neuronav: watch scan failed ({e}); continuing", file=sys.stderr)
            continue
        if dirty:
            _wait_quiet()
            _auto_rescan()


def _start_watcher(interval: float) -> threading.Thread:
    """Daemon polling watcher — stdlib only (threading/time/os in nav).

    The lambda (not ``target=_watch_loop``) keeps the callee a visible
    call for the static graph: bare callback refs are invisible to it."""
    t = threading.Thread(
        target=lambda: _watch_loop(interval), daemon=True, name="neuronav-watch"
    )
    t.start()
    return t


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

    Fast when nothing changed. Run after pulling, branching, or mass
    edits. Read tools also auto-rescan on worktree drift (stat-gated,
    mtime/size fingerprint); this is the explicit always-sync variant.
    """
    t0 = time.perf_counter()
    stats = nav.rescan()
    g, fns, note = _sync_chain(stats)
    nav.stat_mark_synced()
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
    nav.stat_mark_synced()
    watch_note = ""
    if nav.WATCH_INTERVAL_S > 0:
        _start_watcher(nav.WATCH_INTERVAL_S)
        watch_note = f", watcher {nav.WATCH_INTERVAL_S:g}s"
    # warm the clusters stack (networkx/numpy/sklearn/scipy) on the main
    # thread before the event loop serves: importing these C extensions
    # lazily inside a fastmcp tool call (on the anyio loop thread) blocks
    # the stdio server indefinitely on Windows — clusters/context/crosstalk
    # all ride these imports
    import networkx  # noqa: F401
    import numpy  # noqa: F401
    import scipy.cluster.hierarchy  # noqa: F401
    import sklearn.cluster  # noqa: F401
    print(
        f"neuronav: startup files {stats['added']}/{stats['updated']}/"
        f"{stats['unchanged']}/{stats['deleted']}, "
        f"fns {fns['fns_upserted']}, "
        f"in {time.perf_counter() - t0:.1f}s{watch_note}",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")
