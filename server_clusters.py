"""Query family: subsystem + single-file orientation tools (issue #345).

clusters / crosstalk / arch_check / context and their view renderers —
verbatim moves out of server.py; server.py owns the gate rails, so
register() receives the server module and binds late-binding _Rail
handles (the pinning suites patch rails on server's namespace and the
handlers observe it — see server_search.register). _capped/_capped_row
live here (this family is their heaviest user); server_structure and
server.py import them from this module.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context
from servercore import READONLY, _Rail, _capped, _capped_row, mcp

import graph
import navconfig, navstore
from extractors import res_to_rel


CLUSTER_LIST_CAP = 30  # clusters shown per response; +N more past it


def _cluster_member(path_class: tuple) -> str:
    path, _cls = path_class
    return f"  res://{path}"

@mcp.tool(annotations=READONLY)
async def clusters(
    k: int = 6,
    min_sim: float = 0.6,
    dir: str = "",
    ctx: Context = None,
) -> str:
    """Subsystem clusters discovered from embedding geometry (mutual kNN).

    Shows which files belong to the same feature family — UI, core systems,
    asset handling, networking. Use to survey unfamiliar areas or find
    every file related to a system before refactoring it. Returns cluster
    sizes with member paths + class names.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    def _body() -> str:
        with _route(dir) as prelude:
            if prelude:
                return prelude
            _auto_rescan()
            k_c = max(2, min(k, 12))
            ms = max(0.4, min(min_sim, 0.85))
            cs = navstore.clusters(k=k_c, min_sim=ms)
            if not cs:
                return "index empty — call rescan first"
            lines = [f"{len(cs)} cluster(s):", ""]
            for c in cs[:CLUSTER_LIST_CAP]:
                label = c.get("label") or "misc"
                meta = f" [{c.get('method')}, conf {c.get('confidence', 0):.2f}]"
                lines.append(f"c{c['id']} {label} — {c['size']} files{meta}:")
                lines.extend(_capped_row(c["paths"], 12, _cluster_member))
                lines.append("")
            if len(cs) > CLUSTER_LIST_CAP:
                lines.append(
                    f"… +{len(cs) - CLUSTER_LIST_CAP} more cluster(s) — "
                    f"listing capped at {CLUSTER_LIST_CAP}"
                )
            return "\n".join(lines)

    return await _serve(_body, ctx)


@mcp.tool(annotations=READONLY)
def crosstalk(dir: str = "") -> str:
    """Coupling-hotspot report: which subsystem clusters are wired together.

    Counts structural edges (call/signal/var/instance/attach) that CROSS
    cluster boundaries; one edge = one distinct fn pair — call-site and
    call-kind multiplicity collapsed. Scene->scene resource references (pack
    composition) are tallied separately and feed no number; each row
    carries the cluster's derivation method + confidence (#267).
    Use before splitting/merging modules: a cluster with high
    external share is not self-contained; heavy cluster pairs are coupling
    hotspots. Pairs with `clusters` (what the families are) — this reports
    how leaky the boundaries are.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        import clusters as _clusters

        g = graph.get_graph()
        rep = _clusters.crosstalk(navstore.clusters(), g)
        return _clusters.fmt_crosstalk(rep, top_n=2)



@mcp.tool(annotations=READONLY)
def arch_check(dir: str = "") -> str:
    """Architecture-contract check: project rules over cluster crosstalk.

    Reads <state_dir>/arch-rules.json and evaluates each rule against
    the same partition + wiring the crosstalk tool reports — `forbid`
    (cluster A must send zero wires to cluster B) and `budget` (at most
    `max` wires), optionally narrowed to edge types call/var/signal/
    inst/attach/alias. Clusters are named by label or the cN id the
    clusters tool prints. Use before splitting/merging modules to prove
    a boundary still holds, or in review to catch new forbidden
    coupling with the offending file pairs. No rules file configured
    answers with how to write one; a typo'd rule file is reported
    loudly (unknown kind/cluster/type), never silently skipped.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        import archrules as _arch

        return _arch.run(navstore.clusters(), graph.get_graph())


def _ctx_adjacency(g) -> tuple[dict, dict]:
    """File-level adjacency (both directions, per edge-type counts) and
    cross-file in-degree, aggregated once from the func-level edge set."""
    adj: dict[str, dict[str, dict]] = {}  # file -> nb -> {"->": t:n, "<-": t:n}
    indeg: dict[str, int] = {}
    for (s, d), tys in g.edge_types.items():
        sf, df = graph.split_key(s), graph.split_key(d)
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


def _ctx_semantic(path: str, k: int = 6) -> tuple[list[tuple[float, str]], str | None]:
    """Nearest files by embedding cosine — query with the file's own
    stored vector (no embed call, no new deps). Returns (rows, reason):
    reason is None on success — rows may then legitimately be empty when
    the file was never embedded — and carries the truthful backend
    failure otherwise (issue #116: an embed model mismatch must not
    masquerade as 'file not embedded — rescan first')."""
    try:
        col = navstore._collection()
        got = navstore.chroma_read(
            "ctx vectors", lambda: col.get(ids=[path], include=["embeddings"])
        )
        if not got["ids"]:
            return [], None
        res = navstore.chroma_read(
            "ctx neighbors",
            lambda: col.query(
                query_embeddings=[got["embeddings"][0]],
                n_results=k + 1,
                include=["distances"],
            ),
        )
        return [
            (round(1.0 - float(d), 3), fid)
            for fid, d in zip(res["ids"][0], res["distances"][0])
            if fid != path
        ][:k], None
    except Exception as exc:
        return [], navstore.embed_failure_reason(exc)


def _ctx_overview(g) -> str:
    """All-clusters overview: label, size, top members, external edges."""
    import clusters as _clusters

    cs = navstore.clusters()
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


CTX_DEFINES_CAP = 12  # names per kind in context()'s defines section


def _render_defines(fs) -> list[str]:
    """What the file declares (issue #125): capped funcs/signals/members
    rows. The fresh-agent entry point used to emit every orientation
    fact EXCEPT the file's own API surface, forcing a blind read — this
    completes find_functions -> context -> read in one call. Funcs in
    definition order, signals/members sorted: deterministic."""
    lines: list[str] = []
    funcs = sorted(fs.funcs, key=lambda n: (fs.funcs[n].line, n))
    if funcs:
        lines.append(f"defines: {len(funcs)} func(s) — {_capped(funcs, CTX_DEFINES_CAP)}")
    if fs.signals:
        lines.append(
            f"  {len(fs.signals)} signal(s): "
            f"{_capped(sorted(fs.signals), CTX_DEFINES_CAP)}"
        )
    if fs.members:
        lines.append(
            f"  {len(fs.members)} member(s): "
            f"{_capped(sorted(fs.members), CTX_DEFINES_CAP)}"
        )
    return lines


def _render_membership(p: str, cs: list, indeg: dict[str, int]) -> list[str]:
    """Cluster block: label, confidence, this file's in-degree rank,
    top members."""
    lines: list[str] = []
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
        def _row(m: tuple) -> str:
            v, pp, cc = m
            return f"    {v:>3}  res://{pp}" + (f" ({cc})" if cc else "")

        lines.append(f"  members (top {min(12, len(members))} by in-degree):")
        lines.extend(_capped_row(members, 12, _row, indent="    "))
    return lines


def _render_neighbors(p: str, adj: dict, indeg: dict[str, int], depth: int) -> list[str]:
    """Structural block: direct neighbors by edge type + direction,
    2-hop ring when depth >= 2."""
    lines = [f"structural neighbors (depth {depth}):"]
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
        def _nb_row(e: tuple) -> str:
            _w, nb, out_t, in_t = e
            parts = []
            if out_t:
                parts.append("-> " + _ctx_types(out_t))
            if in_t:
                parts.append("<- " + _ctx_types(in_t))
            return f"  res://{nb}  {'  '.join(parts)}"

        lines.extend(_capped_row(entries, 15, _nb_row))
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
    return lines


def _render_semantic(p: str) -> list[str]:
    """Embedding-cosine neighbor block (empty -> rescan hint; backend
    failure -> degraded line with the true reason)."""
    lines = ["semantic neighbors (cosine):"]
    sem, err = _ctx_semantic(p)
    if err:
        lines.append(f"  degraded: {err}")
    elif not sem:
        lines.append("  n/a (file not embedded — rescan first)")
    else:
        for s, fid in sem:
            lines.append(f"  {s:.3f}  res://{fid}")
    return lines


def _render_hub(p: str, g, indeg: dict[str, int]) -> list[str]:
    """Hub status: in-degree + percentile rank over all files."""
    lines: list[str] = []
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
    return lines



@mcp.tool(annotations=READONLY)
def context(path: str = "", depth: int = 1, dir: str = "") -> str:
    """Subsystem map for one repo file — the orientation tool for agents.

    Fresh-agent entry point: pass a res:// path (or repo-relative) and get
    a text map — what it defines (funcs/signals/members, capped with
    "+N more"), its cluster (label, confidence, member hubs by in-degree),
    structural neighbors grouped by edge type (call/signal/var/attach/inst
    with counts and direction, depth 1-3), top semantic neighbors (embedding
    cosine), and hub status (in-degree rank). Called with no path, returns
    the all-clusters overview instead (label, size, top members, external
    edges). Build from existing clusters + graph + vector index; no new deps.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        depth = max(1, min(depth, 3))
        p = path.strip()
        g = graph.get_graph()
        if not p:
            return _ctx_overview(g)
        # res:// is the gdscript project-root scheme — strip via the
        # registry helper, not a local literal
        p = res_to_rel(p)
        p = p.replace("\\", "/").lstrip("/")
        if p not in g.files:
            import difflib

            close = difflib.get_close_matches(p, list(g.files), n=3, cutoff=0.4)
            sug = f" Closest matches: {', '.join(close)}" if close else ""
            return f"unknown file: {p} — pass a repo-relative or res:// path, or rescan first.{sug}"
        fs = g.files[p]
        adj, indeg = _ctx_adjacency(g)
        if fs.class_name and fs.extends:
            tag = f"{fs.class_name} extends {fs.extends}"
        else:
            tag = fs.class_name or fs.extends or fs.ext
        lines = [f"res://{p}  [{tag}]"]
        lines += _render_defines(fs)
        lines += _render_membership(p, navstore.clusters(), indeg)
        lines += _render_neighbors(p, adj, indeg, depth)
        lines += _render_semantic(p)
        lines += _render_hub(p, g, indeg)
        return "\n".join(lines)


def register(server_mod):
    """Compose this family onto the FastMCP instance (issue #345) —
    same contract as server_search.register: the rails bind as
    late-binding _Rail handles resolved through server's module at
    call time (issue #359), historical def order, uniform read-only
    annotation."""
    g = globals()
    for _rail in ("_route", "_serve", "_auto_rescan"):
        g[_rail] = _Rail(server_mod, _rail)
    return clusters, crosstalk, arch_check, context
