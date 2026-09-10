"""neuronav viz: self-contained force-directed 3D graph of the indexed repo.

Generates `graph.html` (single file, three.js from CDN). Nodes = indexed
files colored by semantic cluster; red-mixed nodes contain dead-code
candidates. Edges = aggregated structural links (call edges, scene
instancing, scene→script attachment).

Usage:  python viz.py            # writes graph.html next to this file
        python viz.py out.html   # custom output path
"""

from __future__ import annotations

import os
import base64
import re
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import nav
import graph


def _git_head() -> str:
    """Short HEAD hash of the neuronav repo for the freshness stamp."""
    try:
        got = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=5,
        )
        return got.stdout.strip()
    except Exception:
        return ""


def _churn_hot(paths: list[str]) -> list[float] | None:
    """Per-file git churn of the TARGET project, normalized to 0..1.

    Counts how often each indexed file appears in the last 90 days of
    commits (`git log --name-only --since=90.days`) at nav.ROOT — the
    scanned game repo, not the neuronav tooling repo. Git prints paths
    relative to the repo top level, which may sit above ROOT, so those
    are rebased onto ROOT before matching node paths. Returns None
    (channel disabled — no visual change) when git or history is
    unavailable.
    """
    try:
        root = str(nav.ROOT)
        got = subprocess.run(
            ["git", "log", "--name-only", "--since=90.days", "--pretty=format:"],
            cwd=root,
            capture_output=True, text=True, timeout=15,
        )
        if got.returncode != 0:
            return None
        pre = ""
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().replace("\\", "/")
        if top:
            try:
                pre = Path(root).resolve().relative_to(Path(top).resolve()).as_posix()
                if pre in (".", ""):
                    pre = ""
                else:
                    pre += "/"
            except ValueError:
                pre = ""
        touches: dict[str, int] = defaultdict(int)
        for line in got.stdout.splitlines():
            line = line.strip().replace("\\", "/")
            if line and line.startswith(pre):
                touches[line[len(pre):]] += 1
        if not touches:
            return None
        mx = max(touches.values())
        return [round(touches.get(p, 0) / mx, 3) for p in paths]
    except Exception:
        return None


def _build_data() -> dict:
    g = graph.get_graph()
    clusters = nav.clusters()

    # file -> cluster id
    file_cluster: dict[str, int] = {}
    for c in clusters:
        for path, _cls in c["paths"]:
            file_cluster[path] = int(c["id"])

    # cid -> human label (labeler cascade: autoload > dir > scene > tfidf)
    cluster_names: dict[str, str] = {
        str(int(c["id"])): c.get("label") or f"c{c['id']}" for c in clusters
    }

    # dead-code candidates per file, normalized by func count: a file with
    # 2 dead helpers out of 65 is NOT a "dead file" — only flag when a
    # meaningful share of its funcs is dead. likely counts 1, review 0.5,
    # so share is "fraction of funcs with any dead candidate".
    dead = g.dead_code(limit=10**9)
    dead_weight: dict[str, float] = defaultdict(float)
    dead_likely: set[str] = set()
    func_counts: dict[str, int] = {}
    for rel, fs in g.files.items():
        if fs.ext == ".gd":
            func_counts[rel] = len(fs.funcs)
    for cand in dead["candidates"]:
        if cand["tier"] == "likely":
            dead_weight[cand["path"]] += 1.0
            dead_likely.add(cand["path"])
        else:
            dead_weight[cand["path"]] += 0.5
    DEAD_SHARE_THRESHOLD = 0.4
    dead_flag: dict[str, float] = {}
    for pth, w in dead_weight.items():
        n = func_counts.get(pth, 0)
        if n and w / n >= DEAD_SHARE_THRESHOLD:
            dead_flag[pth] = w

    # nodes: files known to nav's index (searchable corpus) ∪ graph files
    paths = sorted(
        set(file_cluster)
        | {rel for rel, fs in g.files.items() if not rel.startswith("assets/")}
    )
    idx: dict[str, int] = {}
    nodes: list[dict] = []
    for p in paths:
        fs = g.files.get(p)
        idx[p] = len(nodes)
        degree = 0
        nodes.append(
            {
                "id": idx[p],
                "path": p,
                "label": p.rsplit("/", 1)[-1],
                "dir": p.rsplit("/", 1)[0] if "/" in p else "",
                "ext": fs.ext if fs else Path(p).suffix,
                "cls": fs.class_name if fs else "",
                "cluster": file_cluster.get(p, -1),
                "dead": dead_flag.get(p, 0.0),
                "dl": p in dead_likely and p in dead_flag,
            }
        )

    # edges: aggregate typed func-level/scene edges to file level.
    # types: call | signal | inst (scene contains instance) | attach (scene→script)
    def kfile(k: str) -> str:
        if k.endswith("::tscn"):
            return k[: -len("::tscn")]
        return k.split("::")[0]

    pair_types: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    def add_typed(src_key: str, dst_key: str, tys: set[str]) -> None:
        if "SIGNAL:" in src_key or "SIGNAL:" in dst_key:
            return
        s, t = kfile(src_key), kfile(dst_key)
        if s == t or s not in idx or t not in idx:
            return
        for ty in tys:
            pair_types[(idx[s], idx[t])][ty] += 1

    for (src_key, dst_key), edge_tys in g.edge_types.items():
        add_typed(src_key, dst_key, edge_tys)

    for rel, fs in g.files.items():
        if fs.ext != ".tscn":
            continue
        att = fs.attached_script
        if att:
            t = att.removeprefix("res://")
            if rel in idx and t in idx and rel != t:
                pair_types[(idx[rel], idx[t])]["attach"] += 1
        for inst in fs.instances:
            t = inst.removeprefix("res://")
            if rel in idx and t in idx and rel != t:
                pair_types[(idx[rel], idx[t])]["inst"] += 1

    links = [
        {"s": s, "t": t, "w": w, "ty": ty}
        for (s, t), tys in sorted(pair_types.items())
        for ty, w in sorted(tys.items())
    ]

    # function-level call edges: [src_file_idx, src_fn, dst_file_idx, dst_fn, line]
    # mwires: named-wire map rows [ty, sf, sfn, df, dfn, line, extra]
    # (map-spec-v2 §0). call rows ride the exact fedges filters — emitted
    # in the same iteration so the two exports cannot drift — while the
    # ::VAR: pseudo-node dsts fedges skips are harvested as member wires.
    fedges: list[list] = []
    mwires: list[list] = []
    for src_key, dsts in g.edges.items():
        if src_key.endswith("::tscn"):  # pseudo source, fn would be "tscn"
            continue
        s_path, s_fn = src_key.split("::", 1)
        if s_path not in idx:
            continue
        src_fs = g.files.get(s_path)
        s_line = src_fs.funcs[s_fn].line if src_fs and s_fn in src_fs.funcs else 0
        for dst_key in dsts:
            if "::VAR:" in dst_key:
                # member wire — dst file owns the member; intra-file
                # skipped like calls (intra-file wires: spec §11 parking lot)
                d_path, member = dst_key.split("::VAR:", 1)
                if d_path in idx and d_path != s_path:
                    mwires.append(
                        ["var", idx[s_path], s_fn, idx[d_path], member, s_line, None]
                    )
                continue
            if (
                dst_key.endswith("::tscn")
                or "::SIGNAL:" in dst_key
            ):
                continue
            d_path, d_fn = dst_key.split("::", 1)
            if d_path not in idx or d_path == s_path:
                continue
            fedges.append([idx[s_path], s_fn, idx[d_path], d_fn, s_line])
            mwires.append(
                ["call", idx[s_path], s_fn, idx[d_path], d_fn, s_line, None]
            )

    # signal wires: scene connections resolved against the scene's script
    # ext_resources, mirroring graph._wire_tscn's script_rels cascade
    # (multi-script scenes try every script; attached_script only when no
    # ext_resource script is indexed). A connection resolving in N scripts
    # yields N rows; one resolving in none counts into meta.sig_unresolved
    # — the anonymous amber corridor channel (map-spec-v2 §1/F13).
    sig_resolved = 0
    sig_unresolved = 0
    for rel, fs in g.files.items():
        if fs.ext != ".tscn" or rel not in idx:
            continue
        script_rels = [
            s_rel
            for s in fs.scripts
            if (s_rel := s.removeprefix("res://")) in g.files
        ]
        if not script_rels and fs.attached_script:
            s_rel = fs.attached_script.removeprefix("res://")
            if s_rel in g.files:
                script_rels.append(s_rel)
        for sig_name, handler in fs.connections:
            hit = [
                s_rel
                for s_rel in script_rels
                if handler in g.files[s_rel].funcs and s_rel in idx
            ]
            if hit:
                sig_resolved += 1
                for s_rel in hit:
                    mwires.append(
                        ["signal", idx[rel], sig_name, idx[s_rel], handler, 0, None]
                    )
            else:
                sig_unresolved += 1

    # deterministic named-wire order: ty, sf, df, dfn, sfn, line (spec §0)
    mwires.sort(key=lambda w: (w[0], w[1], w[3], w[4], w[2], w[5]))

    # complete per-file fn roster [name, line], line order (spec §0)
    fns: dict[str, list[list]] = {}
    for p in paths:
        fs = g.files.get(p)
        if fs and fs.funcs:
            fns[p] = [
                [fn.name, fn.line]
                for fn in sorted(fs.funcs.values(), key=lambda fn: (fn.line, fn.name))
            ]

    n_clusters = len(clusters)

    # semantic kNN pairs from nav's embedding store — layout-only forces,
    # never rendered as edges: mutual top-6 neighbours with cosine >= 0.45
    # (mutual links resist transitive chaining, mirroring nav.clusters()).
    # Degrades to [] if the chroma store is missing/empty.
    sims: list[list] = []
    cid_gid: dict[int, int] = {}   # fine cluster id -> supergroup id
    groups2: list[dict] = []
    try:
        import numpy as np

        col = nav._collection()
        if col.count():
            got = col.get(include=["embeddings"])
            emb_idx = {rid: i for i, rid in enumerate(got["ids"])}
            rows = [emb_idx[p] for p in paths if p in emb_idx]
            embs = np.array(
                [got["embeddings"][r] for r in rows], dtype=np.float32
            )
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            embs /= norms
            sim = embs @ embs.T
            np.fill_diagonal(sim, -1.0)
            knn = np.argsort(-sim, axis=1)[:, :6]
            for a in range(len(rows)):
                for b in knn[a]:
                    b = int(b)
                    if a < b and a in knn[b] and sim[a, b] >= 0.45:
                        sims.append([a, b, round(float(sim[a, b]), 4)])
            # two-level navigation: coarse supergroups over the fine
            # clusters (scipy average-linkage over embedding centroids,
            # clusters.coarse_groups). Optional UI level — degrades to []
            # when scipy/embeddings are unavailable.
            try:
                from clusters import coarse_groups

                emb_paths = [p for p in paths if p in emb_idx]
                for grp in coarse_groups(clusters, emb_paths, embs):
                    cids = [
                        int(clusters[ci]["id"])
                        for ci in grp["cluster_ids"]
                        if 0 <= ci < len(clusters)
                    ]
                    for cid in cids:
                        cid_gid[cid] = grp["id"]
                    groups2.append(
                        {"id": grp["id"], "label": grp["label"], "cids": cids}
                    )
            except Exception:
                cid_gid = {}
                groups2 = []
    except Exception:
        sims = []

    # supergroup id per node (gid; -1 = unclustered / groups unavailable)
    for nd in nodes:
        nd["gid"] = cid_gid.get(nd["cluster"], -1)

    # cluster-level semantic sims for the layout: cosine between cluster
    # embedding centroids (mean of member embeddings). Drives cluster
    # springs + repulsion caps so semantically related clusters (VFX
    # family) sit as neighbors in the galaxy. Degrades to None.
    ckeys: list = []
    cmat = None
    try:
        import numpy as cnp

        col = nav._collection()
        if col.count():
            got = col.get(include=["embeddings"])
            emb_idx = {rid: i for i, rid in enumerate(got["ids"])}
            rows = [emb_idx[p] for p in paths if p in emb_idx]
            embs = cnp.array(
                [got["embeddings"][r] for r in rows], dtype=cnp.float32
            )
            norms = cnp.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            embs /= norms
            p2c = {nd["path"]: nd["cluster"] for nd in nodes}
            groups: dict = {}
            for r_i, p in enumerate([p for p in paths if p in emb_idx]):
                ci = p2c.get(p, -1)
                if ci >= 0:
                    groups.setdefault(ci, []).append(embs[r_i])
            ckeys = sorted(groups)
            cent = cnp.stack([
                cnp.mean(cnp.stack(groups[c]), axis=0) for c in ckeys
            ])
            cn = cnp.linalg.norm(cent, axis=1, keepdims=True)
            cn[cn == 0] = 1.0
            cent /= cn
            cmat = (cent @ cent.T).astype(cnp.float32)
            cnp.fill_diagonal(cmat, 0.0)
            cmat = cmat.tolist()
    except Exception:
        ckeys = []
        cmat = None

    # frozen layout: deterministic offline sim bakes positions into DATA so
    # the browser loads a settled picture (no live global sim, no 900-tick
    # settle, identical output across regenerations). No in-browser fallback:
    # a failed offline pass aborts the build loudly.
    pos_baked = None
    depths, cyc_ids = _strata_analysis(len(nodes), links)
    # churn boost is part of the rendered radius - the overlap relax MUST
    # use the same radii the browser draws or hot files overlap neighbors
    hot = _churn_hot([nd["path"] for nd in nodes])
    try:
        pos_baked = _layout(
            len(nodes), links, sims, [nd["cluster"] for nd in nodes],
            ckeys=ckeys, cmat=cmat, depths=depths, hot=hot,
        )
    except Exception as e:
        raise RuntimeError(f"offline layout failed: {e}") from e

    # highways: long inter-cluster links render as bundled quadratic bezier
    # arcs (16 segments) instead of straight chords — straight ring-diameter
    # edges visually re-fused the galaxy core. Control point sits on the
    # endpoint midpoint pulled 0.45 toward the cluster-centroid midpoint so
    # same-corridor edges share an arc. Layout-only sibling of links[]: the
    # browser renders these curved, hover/BFS/degree keep using links[].
    hw = []
    if pos_baked is not None:
        try:
            import numpy as _np
            P = _np.asarray(pos_baked, dtype=_np.float32)
            cl = [nd["cluster"] for nd in nodes]
            cen = {}
            for ci in set(cl):
                if ci >= 0:
                    m = P[[k for k, c in enumerate(cl) if c == ci]]
                    cen[ci] = m.mean(axis=0)
            for li, l in enumerate(links):
                a, b = cl[l["s"]], cl[l["t"]]
                if a < 0 or b < 0 or a == b:
                    continue
                if _np.linalg.norm(P[l["t"]] - P[l["s"]]) <= 140.0:
                    continue
                mid = (P[l["s"]] + P[l["t"]]) * 0.5
                ctrl = mid + (cen[a] + cen[b]) * 0.5 * 0.45 - mid * 0.45
                # ring-distributed clusters put both the edge midpoint and
                # the centroid-corridor midpoint near the galaxy center, so
                # the pull alone leaves long arcs nearly straight — add a
                # deterministic perpendicular bow so every highway reads as
                # a curve; capped so long chords don't sweep far past their
                # chord offscreen at close zoom (bow apex = half this value)
                ch = P[l["t"]] - P[l["s"]]
                chl = float(_np.linalg.norm(ch))
                # radial-out bias: corridors that cut through the galaxy
                # core (centroid-midpoint pull aims them there) pass over
                # the hub pile and re-create the hairball under additive
                # blending. Bow them outward around the core instead; a
                # corridor whose midpoint sits AT the core gets no radial
                # direction, so fall back to the chord perpendicular.
                out = mid - P.mean(axis=0)
                out[1] = 0.0
                ol = float(_np.linalg.norm(out))
                if ol > 40.0:
                    ctrl = ctrl + (out / ol) * min(40.0 + 0.30 * ol, 260.0)
                perp = _np.cross(ch, [0.0, 0.0, 1.0])
                pl = float(_np.linalg.norm(perp))
                if pl < 0.001:
                    perp = _np.array([1.0, 0.0, 0.0])
                else:
                    perp = perp / pl
                ctrl = ctrl + perp * min(chl * 0.2, 80.0)
                if ol <= 40.0:
                    ctrl = ctrl + perp * min(chl * 0.25, 120.0)
                pts = []
                for k in range(17):
                    u = k / 16.0
                    p = (1-u)*(1-u)*P[l["s"]] + 2*(1-u)*u*ctrl + u*u*P[l["t"]]
                    pts.append([round(float(x), 1) for x in p])
                hw.append([li, pts])
        except Exception:
            hw = []

    # git-churn channel: optional (None when git/history unavailable → DATA.hot
    # absent → renderer leaves sizes untouched, no legend note)
    hot = _churn_hot([nd["path"] for nd in nodes])

    # per-function IO surface (params / ret / member writes / mutated params),
    # keyed "path::func". consumed by the fn click panel (signature line +
    # write chips), the focus-label writes-state badge and the mutators
    # filter. skipped entirely when a function has nothing to say.
    fio: dict[str, dict] = {}
    for rel, fs in g.files.items():
        for fn in fs.funcs.values():
            if not (fn.params or fn.ret or fn.writes or fn.mut_params):
                continue
            sig = ", ".join(f"{p}: {t}" if t else p for p, t in fn.params)
            fio[f"{rel}::{fn.name}"] = {
                "sig": f"{fn.name}({sig})",
                "ret": fn.ret,
                "w": sorted(fn.writes),
                "mp": sorted(fn.mut_params),
            }

    # crosstalk corridors: top inter-cluster file pairs by edge count, baked
    # for the overview labels. Deterministic order: count desc, then cid asc.
    cl_of = [nd["cluster"] for nd in nodes]
    pair_n: dict = {}
    for l in links:
        ca, cb = cl_of[l["s"]], cl_of[l["t"]]
        if ca < 0 or cb < 0 or ca == cb:
            continue
        key = (ca, cb) if ca < cb else (cb, ca)
        pair_n[key] = pair_n.get(key, 0) + 1
    crosstalk = [
        {"a": a, "b": b, "n": k}
        for (a, b), k in sorted(pair_n.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[:5]
    ]

    data = {
        "nodes": nodes,
        "links": links,
        "fedges": fedges,
        "mwires": mwires,
        "fns": fns,
        "hw": hw,
        "fio": fio,
        "pos": pos_baked,
        "meta": {
            "files": len(nodes),
            "edges": len(links),
            "clusters": n_clusters,
            "deadFiles": len(dead_flag),
            "deadLikely": dead["by_tier"].get("likely", 0),
            "deadReview": dead["by_tier"].get("review", 0),
            # cid -> human name from nav.clusters() labeler cascade
            "clusterNames": cluster_names,
            # freshness stamp: when this DATA was generated and from which
            # neuronav commit (rendered in #stats so stale pages are obvious)
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "git": _git_head(),
            # strata channel: height = call depth from entry files
            "strata": True,
            "depth": depths,
            # files inside call cycles (SCC size > 1) - madge's
            # cyclicNodeColor set; the cycles toggle frames them
            "cycIds": cyc_ids,
            # top inter-cluster corridors, labeled at their arc midpoints
            "crosstalk": crosstalk,
            # signal-resolution counters (map-spec-v2 §0/F13): scene
            # connections traced to a handler fn vs left anonymous
            "sig_resolved": sig_resolved,
            "sig_unresolved": sig_unresolved,
        },
    }
    if hot is not None:
        data["hot"] = hot
    if groups2:
        data["groups"] = groups2
    return data


def _links_adj(n: int, links: list) -> list:
    """Directed adjacency from links rows ({"s","t"} dicts or [s,t,...])."""
    adj: list = [[] for _ in range(n)]
    for l in links:
        s, t = (l["s"], l["t"]) if isinstance(l, dict) else (l[0], l[1])
        adj[s].append(t)
    return adj


def _tarjan_scc(n: int, adj: list) -> tuple:
    """Iterative Tarjan SCC (repo call chains can outrun the recursion
    limit) -> (comp, ncomp). One pass feeds depth layering, cycle flags,
    and the layout."""
    index = [-1] * n
    low = [0] * n
    on = [False] * n
    st: list = []
    comp = [-1] * n
    cnt = 0
    nc = 0
    for s0 in range(n):
        if index[s0] != -1:
            continue
        call = [[s0, 0]]
        while call:
            v, i = call[-1]
            if i == 0:
                index[v] = low[v] = cnt
                cnt += 1
                st.append(v)
                on[v] = True
            descend = False
            while i < len(adj[v]):
                w = adj[v][i]
                i += 1
                if index[w] == -1:
                    call[-1] = [v, i]
                    call.append([w, 0])
                    descend = True
                    break
                if on[w] and index[w] < low[v]:
                    low[v] = index[w]
            if descend:
                continue
            call.pop()
            if call and low[v] < low[call[-1][0]]:
                low[call[-1][0]] = low[v]
            if low[v] == index[v]:
                while True:
                    w = st.pop()
                    on[w] = False
                    comp[w] = nc
                    if w == v:
                        break
                nc += 1
    return comp, nc


def _strata_depths(n: int, links: list) -> list:
    """Per-node call depth for the strata layout (mermaid reading order):
    condense SCCs, longest-path layering over the condensation DAG in topo
    order from its in-degree-0 roots - depth[t] >= depth[s] + 1 across
    SCCs, entries (in-degree 0) at depth 0. Thin wrapper over
    _strata_analysis; kept so the fixture test can call the depth channel
    directly. Pure stdlib."""
    return _strata_analysis(n, links)[0]


def _strata_analysis(n: int, links: list) -> tuple:
    """(per-node call depth, ids of nodes inside call cycles).

    Cycle nodes = members of any SCC of size > 1 (madge's cyclicNodeColor
    set). One Tarjan pass feeds depth layering, cycle flags, and the
    layout."""
    adj = _links_adj(n, links)
    comp, nc = _tarjan_scc(n, adj)
    sizes = [0] * nc
    for c in comp:
        sizes[c] += 1
    # condensation DAG (deduped) + Kahn topo order with longest-path layering
    cadj: list = [set() for _ in range(nc)]
    for u in range(n):
        cu = comp[u]
        for v in adj[u]:
            if cu != comp[v]:
                cadj[cu].add(comp[v])
    indeg = [0] * nc
    for u in range(nc):
        for w in cadj[u]:
            indeg[w] += 1
    cdepth = [0] * nc
    q = [c for c in range(nc) if indeg[c] == 0]
    while q:
        c = q.pop()
        for w in cadj[c]:
            if cdepth[c] + 1 > cdepth[w]:
                cdepth[w] = cdepth[c] + 1
            indeg[w] -= 1
            if indeg[w] == 0:
                q.append(w)
    depths = [cdepth[comp[i]] for i in range(n)]
    cyc_ids = [i for i in range(n) if sizes[comp[i]] > 1]
    return depths, cyc_ids


def _layout(n: int, links: list, sims: list, cluster_ids: list,
            ckeys: list = None, cmat: list = None,
            depths: list = None, hot: list = None) -> list:
    """Deterministic offline force layout; positions are frozen into DATA.

    Mirrors the constants the in-browser sim was QA'd against, plus the
    redesign rules: hub-weighted repulsion (hubs earn breathing room),
    weak single-call springs cut from layout only (they still render),
    semantic springs after a structure-first phase, cluster gravity,
    velocity clamp against transient spikes. Seeded RNG => the same DATA
    always produces the same picture. After the sim the vertical axis is
    re-layered by call depth (_strata_depths): entries on top, callees
    below — XZ stays from the force sim.
    """
    import numpy as np

    rng = np.random.default_rng(1234)
    pos = rng.uniform(-800, 800, (n, 3)).astype(np.float32)
    pos[:, 1] *= 0.5625  # ±450 vertical spread, like the old browser init
    vel = np.zeros((n, 3), dtype=np.float32)

    # structural springs: one per file pair (typed duplicates aggregated),
    # remembering whether the pair is composition (inst/attach)
    pairs: dict = {}
    for l in links:
        a, b = (l["s"], l["t"]) if l["s"] < l["t"] else (l["t"], l["s"])
        rec = pairs.setdefault((a, b), {"w": 0.0, "compo": False})
        rec["w"] += l["w"]
        rec["compo"] = rec["compo"] or l["ty"] in ("inst", "attach")
    deg = np.zeros(n, dtype=np.float32)
    for l in links:
        deg[l["s"]] += l["w"]
        deg[l["t"]] += l["w"]

    kept = []
    for (a, b), rec in pairs.items():
        # edge-cut: weak single non-structural springs are excluded from
        # the layout only so periphery does not chain the clusters inward —
        # EXCEPT weak inter-cluster springs, which keep 25% physics weight
        # (edge-cut stays a render-only declutter between clusters)
        if rec["w"] > 1 or rec["compo"]:
            fc = 0.02 * min(3.0, 1 + rec["w"] * 0.3)
        elif cluster_ids[a] != cluster_ids[b]:
            fc = 0.02 * min(3.0, 1 + rec["w"] * 0.3) * 0.25
        else:
            continue
        kept.append((
            a, b,
            # rest lengths scaled 1.7x: the offline run reaches the springs'
            # true (tight) equilibrium, while the old browser sim froze
            # mid-expansion — scaling keeps the QA'd visual density.
            # cross-cluster call springs rest 2.2x longer still: heavy hub-to-hub
            # call chains otherwise fuse the core clusters into one blob
            (230.0 if rec["compo"] else max(65.0, 165.0 / (1 + rec["w"] * 0.5)))
            * (1.7 if (rec["compo"] or cluster_ids[a] == cluster_ids[b]) else 3.7),
            fc,
        ))
    sa = np.array([k[0] for k in kept], dtype=np.int64)
    sb = np.array([k[1] for k in kept], dtype=np.int64)
    srest = np.array([k[2] for k in kept], dtype=np.float32)
    sfc = np.array([k[3] for k in kept], dtype=np.float32)

    sem = np.asarray(sims, dtype=np.float32).reshape(-1, 3) if sims else \
        np.zeros((0, 3), dtype=np.float32)
    ma = sem[:, 0].astype(np.int64)
    mb = sem[:, 1].astype(np.int64)
    mrest = 950.0 * (1.0 - sem[:, 2])

    # hub-weighted repulsion coefficients: degree product (0.35 exponent —
    # sqrt was so steep that degree-50+ hubs got evicted from their own dense
    # cluster into the nearest sparse pocket) normalised by the mean, so an
    # average pair repels like the old uniform 52000 while hub-hub pairs
    # still push harder (hubs keep breathing room without exile)
    dbar = float(deg.mean()) + 1.0
    ds = ((deg + 1.0) ** 0.35).astype(np.float32)
    # spread constant ~2x the old browser value: the frozen layout has fewer
    # integration steps, so repulsion needs more authority to open the graph
    kcoef = (110000.0 * np.outer(ds, ds) / (dbar * dbar)).astype(np.float32)

    carr = np.asarray(cluster_ids, dtype=np.int64)
    cids, cinv = np.unique(carr, return_inverse=True)
    nc = len(cids)
    ccount = np.maximum(np.bincount(cinv, minlength=nc), 1).astype(np.float32)

    # cluster semantic sims aligned to cids (0 where unclustered / unknown)
    CP = np.zeros((nc, nc), dtype=np.float32)
    if ckeys and cmat:
        ckidx = {int(c): i for i, c in enumerate(ckeys)}
        cmapi = np.array([ckidx.get(int(c), -1) for c in cids])
        ok = cmapi >= 0
        if ok.any():
            sub = np.asarray(cmat, dtype=np.float32)[np.ix_(cmapi[ok], cmapi[ok])]
            CP[np.ix_(ok, ok)] = sub
    # similarity factor: cluster pairs with sim >= 0.6 halve both the node
    # repulsion between them and the centroid blob repulsion — related
    # clusters are allowed to sit close
    hh = np.where(CP >= 0.6, 0.5, 1.0).astype(np.float32)
    kcoef = (kcoef * hh[np.ix_(cinv, cinv)]).astype(np.float32)

    alpha = 1.0
    for step in range(700):
        alpha *= 0.997
        # pairwise repulsion (dense; N is small)
        diff = pos[None, :, :] - pos[:, None, :]        # D[i,j] = pos[j]-pos[i]
        d2 = (diff * diff).sum(-1)
        d2 += 1.0
        m = np.minimum(kcoef / (d2 * d2), 400.0 / d2, dtype=np.float32)
        m[d2 > 2.5e6] = 0.0
        np.fill_diagonal(m, 0.0)
        vel -= np.einsum("ij,ijk->ik", m, diff, dtype=np.float32)
        # structural springs (positive f pulls the pair back together)
        sd = pos[sa] - pos[sb]
        dist = np.linalg.norm(sd, axis=1) + 0.01
        fs = (sd / dist[:, None]) * (((dist - srest) / dist) * sfc)[:, None]
        np.add.at(vel, sa, -fs)
        np.add.at(vel, sb, fs)
        # semantic springs once the structure has rough shape
        if step >= 160 and ma.size:
            md = pos[ma] - pos[mb]
            mdist = np.linalg.norm(md, axis=1) + 0.01
            fms = (md / mdist[:, None]) * (((mdist - mrest) / mdist) * 0.006)[:, None]
            np.add.at(vel, ma, -fms)
            np.add.at(vel, mb, fms)
        # cluster gravity toward per-cluster centroid, scaled by degree:
        # hubs carry huge repulsion coefficients, so a flat gravity lets the
        # repulsion evict them from their own cluster (one high-degree scene
        # drifted 310 units from its pack into a sparse neighbor pocket).
        # Degree-scaled anchor keeps hubs home; average nodes barely move.
        csum = np.zeros((nc, 3), dtype=np.float32)
        np.add.at(csum, cinv, pos)
        cen = csum / ccount[:, None]
        gcoef = (0.006 * (1.0 + deg / 40.0)).astype(np.float32)
        vel += (cen[cinv] - pos) * gcoef[:, None]
        # cluster-centroid semantic springs: similar clusters (by embedding
        # centroid cosine) attract toward the same rest law as node springs;
        # members inherit the pull through cluster gravity. Only pairs with
        # sim > 0.45 act (same gate as node semantic springs).
        if step >= 160 and CP.any():
            cDn = np.linalg.norm(cen[:, None, :] - cen[None, :, :], axis=2) + 0.01
            crest = 480.0 * (1.0 - CP)
            cfc = ((cDn - crest) / cDn) * np.maximum(CP - 0.35, 0.0) * 0.09
            np.fill_diagonal(cfc, 0.0)
            cS = np.einsum(
                "ij,ijk->ik", cfc,
                cen[None, :, :] - cen[:, None, :],   # [i,j] = cen_j - cen_i
                dtype=np.float32,
            )
            vel += cS[cinv] * 0.5
        # cluster-blob repulsion: charged centroids push apart so the
        # overview reads as separated islands instead of one stacked core
        # (halved for similar cluster pairs)
        D = cen[:, None, :] - cen[None, :, :]      # D[i,j] = cen_i - cen_j
        cd2 = (D * D).sum(2) + 1.0
        np.fill_diagonal(cd2, 1e9)
        # two-scale blob repulsion: a gentle global term sets the galaxy
        # radius, a stronger near-range term splits overlapping islands
        big = np.minimum(110.0 * (ccount[:, None] + ccount[None, :]) / cd2, 10.0)
        near = np.minimum(1500.0 * (ccount[:, None] + ccount[None, :]) / cd2, 14.0)
        cw = (big + np.where(cd2 < 48400.0, near, 0.0)) * hh   # < 220 units apart
        cF = np.einsum("ij,ijk->ik", cw, D)
        vel += cF[cinv] * 0.004
        # center gravity + clamped integrate
        vel -= pos * 0.0011
        v2 = (vel * vel).sum(1)
        fast = v2 > 2500.0
        if fast.any():
            vel[fast] *= (50.0 / np.sqrt(v2[fast]))[:, None]
        pos += vel * alpha
        vel *= 0.86
    # overlap relaxation: the force sim guarantees cluster cohesion, not
    # non-overlap — dense cores leave spheres intersecting. Push every
    # overlapping pair apart along its axis (min center distance = 1.35x
    # the rendered radii sum) until clean, then spread + re-center.
    deg = np.zeros(n, dtype=np.float32)
    for l in links:
        s_, t_ = (l["s"], l["t"]) if isinstance(l, dict) else (l[0], l[1])
        w_ = (l.get("w", 1) if isinstance(l, dict) else (l[2] if len(l) > 2 else 1))
        deg[s_] += w_
        deg[t_] += w_
    # radius parity with the renderer: min(10, 3.5+sqrt(deg)) * churn boost
    # * 1.1 — the browser draws exactly this; the relax must too or hot
    # files (up to +35% radius) end up overlapping their neighbors
    churn = (np.asarray(hot, dtype=np.float32) if hot is not None
             else np.zeros(n, dtype=np.float32))
    rad = (np.minimum(12.0, 4.5 + np.sqrt(deg) * 1.0)
           * (1.0 + 0.35 * churn) * 1.1).astype(np.float32)
    min_d = (rad[:, None] + rad[None, :]) * 2.0
    # dilation fallback: pure pair-pushing oscillates in dense cores (a
    # correction that fixes one pair re-violates its neighbors). If a burst
    # of iterations doesn't converge, inflate the layout slightly and retry
    # — geometric relaxation plus dilation always terminates.
    dbg = os.environ.get("NEURONAV_DEBUG_RELAX")
    def depenetrate() -> int:
        """Separate near-concentric pairs deterministically (see comment)."""
        d0 = pos[:, None, :] - pos[None, :, :]
        dd0 = np.sqrt((d0 * d0).sum(-1))
        np.fill_diagonal(dd0, np.inf)
        fused = np.argwhere(dd0 < 5.0)
        moved = 0
        for a, b in fused:
            if a >= b:
                continue
            h = (int(a) * 2654435761 + int(b) * 40503) % 9973
            ang = h / 9973.0 * 6.2831853
            axis = np.array([np.cos(ang), 0.35 * np.sin(ang * 1.7), np.sin(ang)], dtype=np.float32)
            axis /= max(float(np.sqrt((axis * axis).sum())), 1e-3)
            sep = float(min_d[a, b]) * 1.2
            mid = (pos[a] + pos[b]) * 0.5
            pos[a] = mid - axis * (sep * 0.5)
            pos[b] = mid + axis * (sep * 0.5)
            moved += 1
        return moved
    # the force sim can leave two files at nearly identical positions (same
    # gravity basin), where every push direction cancels and they stay
    # concentric forever - which would poison the exact scale pass below
    # (s = min_d/dist explodes on a dist~0 pair). Depenetrate up front, and
    # again after the pushes (pushing can create new fusions).
    n0 = depenetrate()
    # pair pushes REJECTED: at 0.5 gain they created 112 new fusions on
    # target repos (nodes shoved into bystanders) while only marginally reducing
    # the exact-scale factor. Depenetration + exact scale alone is both
    # simpler and provably sufficient: scaling is linear in pos, so one
    # multiply clears every pair.
    n1 = depenetrate()
    if dbg and (n0 or n1):
        print(f"[depen] fixed {n0} before / {n1} after pushes", file=sys.stderr)
    dist = np.sqrt(((pos[:, None, :] - pos[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(dist, np.inf)
    s = float((min_d / dist).max())
    if s > 1.0:
        if dbg:
            print(f"[relax3d] exact scale pass: s={s:.3f}", file=sys.stderr)
        pos *= s * 1.01
    # strata: mermaid reading order - height = call depth from entry files.
    # strata: mermaid reading order — height = call depth from entry files.
    # The force sim and the 3D pass above settle XZ; Y is discarded and
    # re-layered monotonically from _strata_depths (entries on top, callees
    # below) over ~0.55x the old Y half-range, then an XZ-only relaxation
    # with Y frozen restores the non-overlap guarantee.
    if depths is None:   # direct/test callers; _build_data passes it in
        depths = _strata_depths(n, links)
    maxd = max(depths, default=0)
    if maxd > 0:
        # strata Y range proportional to the (relaxed) XZ extent: the old
        # fixed 0.55x-of-old-Y range went pancake-flat once the exact XZ
        # scale inflated the plane 4-5x - an edge-on disc reads as one
        # merged blob. ~0.2x of XZ span keeps terraces visible.
        span = float(max(pos[:, 0].max() - pos[:, 0].min(),
                         pos[:, 2].max() - pos[:, 2].min()))
        half = max(0.32 * span, 160.0)
        spacing = 2.0 * half / maxd
        pos[:, 1] = half - np.asarray(depths, dtype=np.float32) * spacing
        # deterministic jitter within a layer (<= 0.15 spacing): breaks exact
        # Y ties so same-layer pairs keep a stable separation axis below
        pos[:, 1] += (rng.uniform(-1.0, 1.0, n) * (0.30 * spacing)).astype(np.float32)
        # required XZ distance so the 3D distance still clears min_d given
        # the now-frozen Y gap (dy >= min_d pairs need nothing)
        dy = pos[:, None, 1] - pos[None, :, 1]
        req = np.sqrt(np.maximum(min_d * min_d - dy * dy, 0.0)).astype(np.float32)
        # XZ twin of the 3D relax, Y (strata axis) frozen; same exact-scale
        # finisher: dxz scales linearly, s = max(req/dxz) clears all pairs
        # while the frozen Y gaps keep their contribution to the 3D distance
        # strata Y reassignment stacks same-depth nodes into one layer:
        # pairs the OLD Y kept apart vertically can now sit at dxz~0 with
        # dy~0 (jitter) - depenetrate them in XZ (same trick as the 3D
        # pass) before the exact XZ scale, or s explodes on a dxz~0 pair
        for sweep in range(30):
            dxz0 = pos[:, None, [0, 2]] - pos[None, :, [0, 2]]
            dd0 = np.sqrt((dxz0 * dxz0).sum(-1))
            np.fill_diagonal(dd0, np.inf)
            fused = np.argwhere((dd0 < 8.0) & (req > 0.0))
            if not len(fused):
                break
            for a, b in fused:
                if a >= b:
                    continue
                h = (int(a) * 2654435761 + int(b) * 40503) % 9973
                ang = h / 9973.0 * 6.2831853
                axis = np.array([np.cos(ang), np.sin(ang)], dtype=np.float32)
                axis /= max(float(np.sqrt((axis * axis).sum())), 1e-3)
                sep = float(req[a, b]) * 1.2
                mid = (pos[a, [0, 2]] + pos[b, [0, 2]]) * 0.5
                pos[a, [0, 2]] = mid - axis * (sep * 0.5)
                pos[b, [0, 2]] = mid + axis * (sep * 0.5)
        # exact XZ scale: dxz scales linearly, s = max(req/dxz) clears all
        # pairs. Y scales by the SAME factor (around 0): anisotropic
        # XZ-only inflation turned the galaxy into a pancake - uniform
        # scaling keeps the sphere-to-scene ratio the eye was calibrated on.
        dxz = pos[:, None, [0, 2]] - pos[None, :, [0, 2]]
        dxzd = np.sqrt((dxz * dxz).sum(-1))
        np.fill_diagonal(dxzd, np.inf)
        s = float((req / dxzd).max())
        if s > 1.0:
            if dbg:
                print(f"[relaxXZ] exact scale pass: s={s:.3f}", file=sys.stderr)
            pos *= s * 1.01
        else:
            pos *= 1.01
    pos *= 1.45   # extra global breathing room — the frame adapts
    pos -= pos.mean(0)
    return [[round(float(x), 1) for x in p] for p in pos]


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>neuronav — code graph</title>
<style>
  :root { --pane-w: 800px; }   /* map pane width — divider drag rewrites it */
  html, body { margin:0; height:100%; background:#000; overflow:hidden;
    font: 13px/1.45 "Segoe UI", system-ui, sans-serif; color:#cfd8dc; }
  /* camera drags must never text-select the overlays (labels/panel swallowed
     pointer drags and froze the camera) */
  body, body * { user-select: none; -webkit-user-select: none; }
  input, textarea { user-select: text; -webkit-user-select: text; }
  #panel { position:fixed; top:12px; left:12px; z-index:10; width:230px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; backdrop-filter: blur(4px); }
  #panel h1 { font-size:14px; margin:0 0 8px; color:#1de9b6; letter-spacing:1px; }
  #stats { font-size:11px; color:#78909c; margin-bottom:8px; }
  #caption { font-size:10.5px; color:#546e7a; margin:-2px 0 8px; }
  #search { width:100%; box-sizing:border-box; background:#0b1116; color:#cfd8dc;
    border:1px solid #263238; border-radius:6px; padding:6px 8px; outline:none; }
  #search:focus { border-color:#1de9b688; }
  #depthRow { display:flex; align-items:center; gap:6px; margin-top:8px;
    font-size:11px; color:#78909c; }
  #depth { flex:1; accent-color:#1de9b6; }
  #depthRow .cb { display:flex; align-items:center; gap:4px; cursor:pointer; }
  #depthRow .cb input { accent-color:#1de9b6; }
  #legend { margin-top:10px; max-height:38vh; overflow-y:auto; }
  .chip { display:inline-block; margin:2px; padding:2px 8px; border-radius:10px;
    font-size:10.5px; cursor:pointer; border:1px solid transparent; }
  .chip:hover { border-color:#fff5; }
  .chip.on { outline:1px solid #fff; }
  #toggles { margin-top:10px; display:flex; flex-wrap:wrap; gap:6px 4px; }
  button { flex:1 1 21%; background:#0b1116; color:#b0bec5; border:1px solid #263238;
    border-radius:6px; padding:5px 2px; cursor:pointer; font-size:11px;
    text-align:center; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  button.on { color:#1de9b6; border-color:#1de9b688; }
  #info { position:fixed; top:12px; right:12px; z-index:10; width:290px;
    transition:right .25s ease; }
  #info.mapShift { right: calc(var(--pane-w) + 18px); }  /* clear of the map pane */
    background:rgba(10,14,18,.88); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; display:none; backdrop-filter: blur(4px); }
  #info h2 { font-size:13px; margin:0 0 4px; color:#fff; word-break:break-all; }
  #info .sub { font-size:11px; color:#78909c; margin-bottom:8px; }
  #info .tag { display:inline-block; font-size:10px; padding:1px 7px;
    border-radius:8px; margin:0 4px 6px 0; }
  #info ul { list-style:none; margin:6px 0 0; padding:0; max-height:24vh;
    overflow-y:auto; }
  #info li { padding:3px 6px; border-radius:5px; cursor:pointer; font-size:11.5px; }
  #info li:hover { background:#1de9b61a; color:#1de9b6; }
  .kind { color:#546e7a; font-size:10px; text-transform:uppercase;
    letter-spacing:1px; margin-top:8px; }
  #dirRow { display:flex; align-items:center; gap:5px; margin-top:6px;
    font-size:11px; color:#78909c; }
  #dirRow .seg { flex:1; padding:3px 0; font-size:10.5px; }
  #iCopy { position:absolute; top:9px; right:9px; width:26px; height:22px;
    padding:0; flex:none; background:#0b1116; color:#78909c;
    border:1px solid #263238; border-radius:5px; cursor:pointer; font-size:11px; }
  #iCopy:hover { color:#1de9b6; border-color:#1de9b688; }
  #info li.more { color:#78909c; cursor:default; font-size:10.5px; }
  #info li.more:hover { background:none; color:#78909c; }
  #tip { position:fixed; z-index:20; pointer-events:none; display:none;
    background:#000d; border:1px solid #1de9b644; color:#eee; font-size:11px;
    padding:4px 8px; border-radius:6px; white-space:pre-line; }
  #lg3d { position:fixed; left:10px; bottom:10px; z-index:21; width:22px; height:22px;
    display:flex; align-items:center; justify-content:center; cursor:pointer;
    background:rgba(10,14,18,.82); border:1px solid #1de9b644; border-radius:50%;
    color:#80cbc4; font-size:12px; font-weight:700; user-select:none; }
  #lg3dx { position:fixed; left:38px; bottom:8px; z-index:21; display:none;
    align-items:center; gap:14px; padding:4px 10px;
    background:rgba(10,14,18,.88); border:1px solid #1de9b633; border-radius:12px;
    font-size:10.5px; color:#b0bec5; white-space:nowrap; }
  #lg3dx span { display:flex; align-items:center; gap:5px; }
  #lg3dx i { display:inline-block; }
  .lgTrunk { width:16px; height:4px; border-radius:2px;
    background:linear-gradient(90deg,#e8996d,#e8b084); }
  .lgDot { width:9px; height:9px; border-radius:50%; background:#f2efe4; }
  .lgChev { width:0; height:0; border-left:5px solid transparent;
    border-right:5px solid transparent; border-bottom:9px solid #f5a623; }
  .lgDash { width:16px; border-top:2px dashed #546e7a; }
  #edgeLegend { display:flex; flex-wrap:wrap; gap:3px 10px; margin-top:6px;
    font-size:10px; color:#78909c; }
  .eKey { display:flex; align-items:center; gap:4px; }
  .eKey i { width:14px; border-top:2px solid #ffffff55; display:inline-block; }
  .eHint { color:#546e7a; }
  #crumb { position:fixed; top:12px; left:50%; transform:translateX(-50%);
    z-index:10; display:none; align-items:center; gap:6px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633;
    border-radius:14px; padding:4px 7px 4px 14px; font-size:11px; color:#b0bec5; }
  /* pane open: center over the 3D region, not the window - a wide map pane
     (default 800) would otherwise put the crumb ON TOP of the pane's vars
     chip, stealing its clicks */
  body.mapOpen #crumb { left: calc((100% - var(--pane-w)) / 2); }
  #crumb b { color:#1de9b6; font-weight:600; }
  #crumb .x { cursor:pointer; color:#78909c; padding:0 6px; border-radius:50%;
    line-height:1.3; }
  #crumb .x:hover { color:#fff; background:#ffffff14; }
  #crumb .back { cursor:pointer; color:#1de9b6; padding:0 6px; border-radius:50%;
    line-height:1.3; }
  #crumb .back:hover { color:#fff; background:#ffffff14; }
  #crumb .hint { color:#546e7a; }
  #crumb .f { cursor:pointer; color:#ffcc80; padding:0 7px; border-radius:9px;
    border:1px solid #ffcc8044; }
  #crumb .f:hover { background:#ffcc8022; }
  #hubs { position:fixed; inset:0; z-index:5; pointer-events:none;
    overflow:hidden; }
  .hub { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    max-width:230px; overflow:hidden; text-overflow:ellipsis;
    font-size:10.5px; color:#eceff1; background:rgba(8,12,16,.8);
    padding:1px 7px; border-radius:7px; border:1px solid #ffffff1f;
    pointer-events:auto; cursor:pointer; }
  .hub:hover { border-color:#fff7; background:rgba(18,26,32,.88); }
  #elabs { position:fixed; inset:0; z-index:4; pointer-events:none;
    overflow:hidden; }
  .elab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:11px; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); pointer-events:none; }
  #xtlabs { position:fixed; inset:0; z-index:4; pointer-events:none;
    overflow:hidden; }
  .xtlab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:10.5px; color:#b0bec5; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); pointer-events:none; }
  #clabs { position:fixed; inset:0; z-index:3; pointer-events:none;
    overflow:hidden; }
  .clab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:12px; font-weight:700; letter-spacing:1.5px; opacity:.95;
    text-shadow:0 0 6px #000, 0 1px 3px #000, 0 0 12px #000; }
  #flabs { position:fixed; inset:0; z-index:4; pointer-events:none;
    overflow:hidden; }
  .flab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:11px; padding:1px 6px; border-radius:5px; cursor:pointer;
    background:rgba(8,12,16,.78); color:#cfd8dc; pointer-events:auto;
    text-shadow:0 1px 2px #000; }
  .flab:hover { color:#fff; background:rgba(20,30,38,.92); }
  .flab.fn { font-size:10px; color:#8fa3ad; background:rgba(8,12,16,.6); }
  .flab.fn:hover { color:#d0f2ea; background:rgba(14,26,24,.9); }
  #stubLabs { position:fixed; inset:0; z-index:6; pointer-events:none;
    overflow:hidden; }
  .stublab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:10px; color:#8fa3ad; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); }
  /* 3D region: canvas pinned left of the map pane; renderer.setSize keeps
     its style box in sync on every divider/resize event */
  canvas#gl { position:fixed; top:0; left:0; display:block; }
  #mapPane { position:fixed; top:0; right:0; width:var(--pane-w); height:100%;
    display:block; background:#0b0f14; z-index:8;
    border-left:1px solid #1de9b633; }
  #mapPane.collapsed { display:none; }
  /* draggable vertical split between the 3D view and the map pane */
  #divider { position:fixed; top:0; right:var(--pane-w); width:7px; height:100%;
    z-index:12; cursor:col-resize; background:transparent;
    border-left:1px solid #1de9b633; touch-action:none; user-select:none; }
  #divider:hover, #divider.drag { background:#1de9b622; }
  #divider.collapsed { display:none; }
  /* map-local DOM overlays (tooltip / bundle list / fn picker) — one
     container spanning the pane area, children opt into pointer events */
  #mapOv { position:fixed; top:0; right:0; width:var(--pane-w); height:100%;
    z-index:9; pointer-events:none; overflow:hidden; display:none;
    font:10.5px ui-monospace, Menlo, Consolas, monospace; color:#cfd8dc; }
  body.mapOpen #mapOv { display:block; }
  /* label overlays are projected in CANVAS space — confine them to the 3D
     region whenever the map pane cedes width */
  body.mapOpen #hubs, body.mapOpen #elabs, body.mapOpen #xtlabs,
  body.mapOpen #clabs, body.mapOpen #flabs { right: var(--pane-w); }
  #mapTip { position:absolute; display:none; background:#000d;
    border:1px solid #1de9b644; color:#eee; padding:4px 8px; border-radius:6px;
    white-space:pre-line; max-width:280px; line-height:1.5; }
  #wireTip { position:fixed; display:none; z-index:30; background:#000e;
    border:1px solid #1de9b655; color:#eee; padding:6px 10px; border-radius:6px;
    font-size:12px; white-space:pre-line; max-width:340px; max-height:45vh;
    overflow-y:auto; line-height:1.5; pointer-events:none; }
  #mapList { position:absolute; display:none; pointer-events:auto;
    background:rgba(8,12,16,.95); border:1px solid #263238; border-radius:8px;
    padding:8px; min-width:240px; max-width:300px; max-height:50vh;
    overflow-y:auto; }
  #mapList h3 { margin:0 0 6px; font-size:11px; font-weight:600; color:#1de9b6;
    word-break:break-all; }
  #mapList .row, #mapPick .row, #fnPick .row { padding:2px 4px; border-radius:4px;
    cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #mapList .row:hover, #mapPick .row:hover, #fnPick .row:hover {
    background:#1de9b61a; color:#1de9b6; }
  #mapPick, #fnPick { display:none; pointer-events:auto;
    background:rgba(8,12,16,.95); border:1px solid #263238; border-radius:8px;
    padding:8px; width:250px; max-height:40vh; overflow-y:auto; }
  #mapPick { position:absolute; }
  /* fn picker rides the 3D view: body-level fixed, viewport coords */
  #fnPick { position:fixed; z-index:6; }
  #mapPick input, #fnPick input { width:100%; box-sizing:border-box; background:#0b1116;
    color:#cfd8dc; border:1px solid #263238; border-radius:5px; padding:4px 6px;
    outline:none; font:inherit; margin-bottom:6px; }
  #mapPick input:focus, #fnPick input:focus { border-color:#1de9b688; }
  </style>
</head>
<body>
<div id="panel">
  <h1>neuronav</h1>
  <div id="caption">color = subsystem · size = connectivity · click a node to explore</div>
  <div id="stats"></div>
  <div id="edgeLegend"></div>
  <input id="search" placeholder="search file / class…">
  <div id="depthRow">
    <span>depth</span>
<input id="depth" type="range" min="1" max="3" value="1">
<span id="depthVal">1</span>
<span style="margin-left:10px">spread</span>
<input id="spread" type="range" min="60" max="260" value="100" title="stretch the whole layout apart (scales from the centroid)">
<span id="spreadVal">1.0</span>
    <label class="cb"><input type="checkbox" id="cbFn" checked> functions</label>
    <label class="cb"><input type="checkbox" id="cbSpin"> spin</label>
  </div>
  <div id="dirRow">
    <span>dir</span>
    <button class="seg on" data-d="0">both</button>
    <button class="seg" data-d="1">out</button>
    <button class="seg" data-d="2">in</button>
  </div>
  <div class="kind">subsystems</div>
  <div id="legend"></div>
  <div class="kind">folders</div>
  <div id="dirs"></div>
  <div id="toggles">
  <button id="bCalls" class="on">calls</button>
  <button id="bSignals" class="on">signals</button>
<button id="bMut" title="fn layer: only functions that write member state (✎ badge)">mutators</button>
    <button id="bInst">contains</button>
  <button id="bVar" title="member-var references — dense, off by default">var</button>
  <button id="bGhost" title="focus mode: ghost wires outside the hub budget (quiet layer — off by default; hover a node or wire to reveal)">ghosts</button>
    <button id="bGround" title="fixed ground grid under the graph (orientation aid)">ground</button>
    <button id="bGroups" title="recolor by coarse supergroups (two-level navigation)">groups</button>
    <button id="bCollapse" title="collapse every cluster of 3+ visible files into one supernode; edges re-attach to the merged sphere">collapse</button>
    <button id="bDead" title="show only files flagged dead: at least 40% of their funcs are dead candidates">dead only</button>
    <button id="bMap" title="collapse / expand the named-wire map pane (right)">map</button>
    <button id="bCyc" title="show only files inside call cycles (strongly connected components); cycle members tint red like madge's cyclic marker">cycles</button>
    <button id="bReset">reset</button>
  </div>
</div>
<div id="crumb"></div>
<div id="info" style="display:none">
  <h2 id="iTitle"></h2>
  <div class="sub" id="iSub"></div>
  <button id="iCopy" title="copy res:// path">⧉</button>
  <div id="iTags"></div>
  <div class="kind" id="kUses">USES (0)</div>
  <ul id="iUses"></ul>
  <div class="kind" id="kUsedBy">USED BY (0)</div>
  <ul id="iUsedBy"></ul>
</div>
<div id="tip"></div>
<div id="lg3d" title="what am I looking at?">?</div>
<div id="lg3dx">
  <span><i class="lgTrunk"></i>trunk = bundled calls (one corridor)</span>
  <span><i class="lgDot"></i>ivory dot = junction (wires merge)</span>
  <span><i class="lgChev"></i>amber chevron = delivery direction</span>
  <span><i class="lgDash"></i>dashed = quiet (many thin calls)</span>
</div>
<div id="hubs"></div>
<div id="stubLabs"></div>
<div id="elabs"></div>
<div id="xtlabs"></div>
<div id="flabs"></div>
<div id="clabs"></div>
<canvas id="mapPane"></canvas>
<div id="divider" title="drag to resize the map pane"></div>

<script type="importmap">
__IMPORTMAP__
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";

const DATA = __DATA__;
const nodes = DATA.nodes, links = DATA.links, fedges = DATA.fedges || [], hw = DATA.hw || [];
const N = nodes.length;
// cycle lens (madge cyclicNodeColor steal): files inside call cycles
// (SCC size > 1), baked by _strata_analysis
const cycSet = new Set(DATA.meta.cycIds || []);
let cycOnly = false;
nodes.forEach((n, i) => { n.cyc = cycSet.has(i) ? 1 : 0; });
// undirected adjacency for focus BFS + directed halves for the in/out
// direction modes ("what breaks if I change X" needs OUT = who I affect
// downstream, IN = who feeds me)
const adj = Array.from({ length: N }, () => []);
const adjOut = Array.from({ length: N }, () => []);
const adjIn = Array.from({ length: N }, () => []);
links.forEach(l => {
  adj[l.s].push(l.t); adj[l.t].push(l.s);
  adjOut[l.s].push(l.t); adjIn[l.t].push(l.s);
});
const outDeg = adjOut.map(a => new Set(a).size);
const inDeg = adjIn.map(a => new Set(a).size);
// fn-name index for search seeding: fn name -> Set(owning/calling files)
const fnOf = {};
fedges.forEach(e => {
  (fnOf[e[1]] = fnOf[e[1]] || new Set()).add(e[0]);
  (fnOf[e[3]] = fnOf[e[3]] || new Set()).add(e[2]);
});
const fnNames = Object.keys(fnOf);
// per-file typed edge weight (for the hover summary line)
const typedCount = Array.from({ length: N }, () => ({}));
links.forEach(l => {
  typedCount[l.s][l.ty] = (typedCount[l.s][l.ty] || 0) + l.w;
  typedCount[l.t][l.ty] = (typedCount[l.t][l.ty] || 0) + l.w;
});
function edgeSummary(i) {
  const c = typedCount[i], parts = [];
  if (c.call) parts.push(Math.round(c.call) + " calls");
  if (c.signal) parts.push(Math.round(c.signal) + " signals");
  const cont = (c.inst || 0) + (c.attach || 0);
  if (cont) parts.push(Math.round(cont) + " contains");
  return parts.join(" · ");
}
// BFS path from a hovered node to the current focus seed(s) — the "where am I
// relative to what I focused" answer. Seeds = clicked seeds plus query matches
// (the same seed set computeLevels uses), so the path matches the lit graph.
function pathToSeed(i) {
  const seeds = new Set(focusSeeds);
  if (query) {
    const q = query.toLowerCase();
    nodes.forEach((n, j) => {
      if (n.path.toLowerCase().includes(q) || (n.cls || "").toLowerCase().includes(q)) seeds.add(j);
    });
  }
  if (!seeds.size || seeds.has(i)) return null;
  const prev = new Map([[i, -1]]);
  const queue = [i];
  for (let qi = 0; qi < queue.length; qi++) {
    const u = queue[qi];
    for (const v of (adj[u] || [])) {
      if (prev.has(v)) continue;
      prev.set(v, u);
      if (seeds.has(v)) {
        const hops = [];
        for (let x = v; x !== -1; x = prev.get(x)) hops.push(nodes[x].label);
        return hops.reverse();
      }
      queue.push(v);
    }
  }
  return null;   // no route: hovered node is outside the focus component
}

// ---- scene -----------------------------------------------------------------
// 3D/map split: the map pane is a permanent part of the layout (collapsible
// via #bMap). The 3D renderer owns everything left of it; both sizes derive
// from paneW so a divider drag reflows both at once.
const PANE_MIN = 180, PANE_DEFAULT = 800;
const paneMax = () => Math.max(PANE_MIN + 120, innerWidth - 320);   // 3D keeps >=320
const PANE_KEY = "neuronav.mapPaneW";
let mapVisible = true;   // pane ships open; #bMap collapses/expands it
let paneW = Math.max(PANE_MIN, Math.min(paneMax(),
  parseInt(localStorage.getItem(PANE_KEY) || "", 10) || PANE_DEFAULT));
const applyPaneW = () =>
  document.documentElement.style.setProperty("--pane-w", paneW + "px");
applyPaneW();
const glW = () => Math.max(320, innerWidth - (mapVisible ? paneW : 0));
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.domElement.id = "gl";
renderer.setSize(glW(), innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x000000, 0.00022); // heavier fog washed out cluster hues at overview distance
const camera = new THREE.PerspectiveCamera(55, glW()/innerHeight, 1, 20000);
camera.position.set(0, 0, 1400);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
// idle auto-spin (the cbSpin checkbox toggles it); tick() pauses it
// while the user drags or inspects a fn box
controls.autoRotate = false;
controls.autoRotateSpeed = 0.35;
let spinEnabled = false;

// cluster hue: golden angle spread; dead files tinted toward red.
// lightness bands break golden-angle hue collisions across long cid runs
const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
const lightOf = c => 0.52 + 0.09 * (Math.floor(c / 13) % 3);
const colorOf = n => {
  // cycles lens: tint overrides cluster hue while the toggle is on
  if (cycOnly && n.cyc) return new THREE.Color(0xff6c60);
  // groups mode colors by supergroup id (few ids, well-spread hues)
  const cc = (groupsMode && n.gid >= 0) ? n.gid : n.cluster;
  // unclustered: neutral desaturated gray (old hue 0.08 read orange-red)
  const col = cc < 0 ? new THREE.Color().setHSL(0.55, 0.08, 0.62)
                     : new THREE.Color().setHSL(hue(cc), 0.72, lightOf(cc));
  if (n.dead > 0) col.lerp(new THREE.Color(1.0, 0.3, 0.2), n.dead >= 1 ? 0.85 : 0.68);
  return col;
};

const pos = new Float32Array(N * 3);
const colArr = new Float32Array(N * 3);
const sizes = new Float32Array(N);
const degree = new Float32Array(N);
// git-churn channel (optional): DATA.hot[i] in 0..1 = how often node i's
// file was touched in the last 90 days (max-touched file = 1). Absent when
// the generator ran outside a git repo — every fallback below no-ops.
const hot = DATA.hot || null;
// two-level navigation (optional): coarse supergroups from clusters.py;
// the "groups" toggle recolors nodes, halos and the legend by supergroup.
// Absent (no scipy/embeddings) → button hidden, nothing else changes.
const groups = DATA.groups || null;
const gNames = {};
if (groups) groups.forEach(g => { gNames[g.id] = g.label; });
let groupsMode = false;
// frozen baked layout: positions were settled offline in Python (seeded,
// deterministic) — the browser only renders. A missing DATA.pos means the
// offline pass failed and the build aborted halfway: fail loudly here
// rather than silently fall back to a live sim.
const frozenPos = DATA.pos;
if (!Array.isArray(frozenPos)) throw new Error("DATA.pos missing — offline layout failed");
links.forEach(l => { degree[l.s] += l.w; degree[l.t] += l.w; });
nodes.forEach((n, i) => {
  pos[i*3] = frozenPos[i][0]; pos[i*3+1] = frozenPos[i][1]; pos[i*3+2] = frozenPos[i][2];
  const c = colorOf(n);
  colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
  // churn boost rides on top of the connectivity size (up to +35% radius
  // for the most-touched file) — subtle, never shrinks
  sizes[i] = Math.min(12, 4.5 + Math.sqrt(degree[i]) * 1.0) * (hot ? 1 + 0.35 * hot[i] : 1);
});
if (hot) document.getElementById("caption").textContent += " · size also encodes 90-day churn";

// spread control: uniformly re-scale the baked layout around its centroid.
// basePos holds the baked coordinates; pos is the live (scaled) copy that
// every renderer reads, so a slider change just re-derives pos + re-syncs.
const basePos = Float32Array.from(pos);
let spread = 1;
const supMem = new Uint8Array(N);
let fnMode = false;   // hoisted: the eval-time syncFileMesh() call below reads it via sphR
// satellite allowance: files with many fn boxes grow the sphere so the box
// ring keeps readable spacing (user call: bigger sphere, not smaller boxes).
// Threshold mirrors AGG_MAX (fn-layer local). Deterministic: pure fn of DATA.
const fnCount = nodes.map(n => (DATA.fns && DATA.fns[n.path] || []).length);
const satBoost = i =>
  fnCount[i] > 6 ? 1 + Math.min(0.8, 0.25 * Math.log2(fnCount[i] / 6)) : 1;
const sphR = i => sizes[i] * 1.1 * Math.sqrt(spread) *
  (fnMode && !supMem[i] ? satBoost(i) : 1);
// centroid of the baked layout — the affine center for spread transforms
let baseCx = 0, baseCy = 0, baseCz = 0;
for (let i = 0; i < N; i++) { baseCx += basePos[i*3]; baseCy += basePos[i*3+1]; baseCz += basePos[i*3+2]; }
baseCx /= N; baseCy /= N; baseCz /= N;
// absolute fog density must track the layout size (frameGraph recomputes
// it on reset/isolates): a constant tuned for one graph size fogs out far
// nodes once the relax inflates the layout
scene.fog.density = 0.17 / Math.max(420, graphBounds().radius * 1.55);
function applySpread(s) {
  if (s === spread) return;   // no-op: never fight camera tweens / mid-flight syncs
  const spreadPrev = spread;
  spread = s;
  // centroid of basePos is constant since boot (baseCx/Cy/Cz) - recomputing
  // per call was redundant with syncEdgePos's arc anchor
  const cx = baseCx, cy = baseCy, cz = baseCz;
  for (let i = 0; i < N; i++) {
    pos[i*3]   = cx + (basePos[i*3]   - cx) * s;
    pos[i*3+1] = cy + (basePos[i*3+1] - cy) * s;
    pos[i*3+2] = cz + (basePos[i*3+2] - cz) * s;
  }
  // fn satellites re-derive from their wires and edges re-attach — all via
  // applyVisibility (owns the fn layer + edge geometry + labels).
  // NO frameGraph here: spread must keep the user's zoom (reframing halved
  // apparent node size and made the graph unrecognizable); spheres grow by
  // sqrt(spread) in syncFileMesh so they stay readable as gaps open.
  // fog must weaken as the galaxy expands or far clusters sink into black;
  // frameGraph owns the absolute part (layout-size-derived), spread scales
  // it relatively
  scene.fog.density *= spreadPrev / s;
  syncFileMesh();
  applyVisibility();
  buildContainment();
  // the raycaster caches ONE bounding sphere for the whole instanced mesh on
  // its first hit-test; after a spread rescale that sphere is stale and every
  // node outside it silently fails to pick (frustumCulled=false does NOT
  // affect raycasting). Recompute so picking tracks the real layout.
  if (fileMesh) fileMesh.computeBoundingSphere();
}

const alphaArr = new Float32Array(N).fill(1);
// per-node alpha TARGETS: alphaArr eases toward these each tick (fade);
// visibility checks (raycast, edge kill, labels) read the targets
const alphaTgt = new Float32Array(N).fill(1);
// in-scene hover feedback: per-node scale eases toward 1.8 while hovered
// (lerped in tick's instance-matrix sync; tooltip text is unchanged)
const hoverScale = new Float32Array(N).fill(1);

// supernode collapse: when ON, each cluster with >= 3 visible members
// merges into one sphere at the member centroid. pos NEVER moves (the
// layout is frozen); dpos is the edge-ATTACHMENT copy — identical to pos
// normally, but a collapsed member's slot points at its cluster centroid
// so every edge re-targets the supernode.
let collapsed = false, fnWasOn = false;   // fnWasOn: fn-layer state across a collapse round-trip
const dpos = new Float32Array(N * 3);
dpos.set(pos);
// cluster -> { cx, cy, cz, n, first } for currently-collapsed clusters,
// plus per-node membership (alphaTgt is zeroed for members, so the edge
// dim pass needs this to keep supernode-carried edges bright)
const supCollapsed = new Map();
// satellite allowance block moved above the eval-time syncFileMesh() call
// PERCEPTUAL ANCHOR law (user sightings 3+4): ink arriving at a node implies
// that node's anchor READS. anchorBoost[i] = minimum DIAMETER in REF-px
// (normalized to 900px canvas height; the user's ~840h window shows 8 REF-px
// as ~7.5 real px) the sprite must draw while ink terminates on it; 6 was
// still speck-class to the eye, 8 REF-px diameter is the ruled bar.
// syncFileMesh lifts scale to meet it (capped so hierarchy survives).
const ANCHOR_PX = 8;   // endpoint anchor bar, DIAMETER ref-px
const anchorBoost = new Float32Array(N);

// true 3D node geometry (billboard sprites read flat on screen): files =
// shaded spheres, functions = boxes orbiting their owner file sphere,
// variables later = tetrahedra. Per-instance color carries the cluster hue;
// hidden nodes collapse to scale 0 (zero rasterized fragments); dimmed nodes
// darken toward black instead of fading, so shading stays readable.
scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dirLight = new THREE.DirectionalLight(0xffffff, 1.4);
dirLight.position.set(0.4, 0.8, 0.65);
scene.add(dirLight);

// fixed-up cue: subtle polar grid below the galaxy — when orbiting deep,
// it is the only thing that keeps "down" readable (off by default; the
// ground button toggles it and its state survives resetAll)
let showGround = false;
let _minY = 1e9, _maxR = 0;
for (let i = 0; i < N; i++) {
  _minY = Math.min(_minY, pos[i*3+1]);
  _maxR = Math.max(_maxR, Math.hypot(pos[i*3], pos[i*3+2]));
}
const groundGrid = new THREE.PolarGridHelper(
  Math.max(600, _maxR * 1.05), 12, 5, 64, 0x27455c, 0x1a2f42);
groundGrid.position.y = _minY - 110;
groundGrid.material.transparent = true;
groundGrid.material.opacity = 0.35;
groundGrid.visible = showGround;
scene.add(groundGrid);

const fileMesh = new THREE.InstancedMesh(
  new THREE.SphereGeometry(1, 14, 10),
  new THREE.MeshLambertMaterial(),
  N
);
fileMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
// the union bounding sphere is computed once from boot positions; after a
// spread rescale it is stale and frustum-culls periphery instances from
// BOTH rendering and raycasting (outside nodes became unpickable)
fileMesh.frustumCulled = false;
scene.add(fileMesh);
const _dummy = new THREE.Object3D();
const _col = new THREE.Color();
function syncFileMesh() {
  // fn-ownership: while a fn box is hovered its owning file lifts hard
  // stale pick guard: fnMeta is rebuilt/cleared by rebuildFnLayer (focus
  // cleared, collapse, fn toggle) — a hoveredFn pointing past it would
  // crash this per-frame read and kill the tick loop
  if (hoveredFn >= 0 && !fnMeta[hoveredFn]) hoveredFn = -1;
  const fnOwner = hoveredFn >= 0 ? fnMeta[hoveredFn].file : -1;
  for (let i = 0; i < N; i++) {
    const a = alphaArr[i];
    if (a < 0.01) {
      _dummy.position.set(0, 0, 0);
      _dummy.scale.setScalar(0);
    } else {
      _dummy.position.set(pos[i*3], pos[i*3+1], pos[i*3+2]);
      // dead-only mode boosts the survivors so the red set reads at overview distance
      // sqrt(spread) size compensation: gaps scale ~spread, nodes scale
      // ~sqrt(spread) so pulling apart leaves them readable without a
      let sc = sphR(i) * (deadOnly && nodes[i].dead > 0 ? 1.7 : 1) * hoverScale[i] * (0.45 + 0.55 * a);
      // perceptual anchor: if ink terminates on this node, hold the sprite
      // at ANCHOR_PX screen diameter — but floor the REST size and let
      // hoverScale ease from the FLOORED rest. Flooring the post-hover size
      // instead made the lift vanish the moment hover grew the sprite past
      // the floor, so the eased 1.8x read as 1.1x of the visible rest
      // (harness pin + user expectation: hover grows what the eye sees).
      if (anchorBoost[i] > 0) {
        const dist = camera.position.distanceTo(_dummy.position);
        const hs = hoverScale[i] || 1;
        const base = sc / hs;   // rest size (alpha included), hover lifted out
        const rpxBase = base * (renderer.domElement.clientHeight / 2) /
                        (Math.tan(camera.fov * Math.PI / 360) * dist);
        if (rpxBase > 0.001 && rpxBase < anchorBoost[i])
          sc = base * Math.min(4.0, anchorBoost[i] / rpxBase) * hs;
      }
      _dummy.scale.setScalar(sc);
    }
    _dummy.updateMatrix();
    fileMesh.setMatrixAt(i, _dummy.matrix);
    // hovered nodes also lift slightly in brightness alongside the scale ease
    const lift = a * (1 + 0.35 * (hoverScale[i] - 1)) * (i === fnOwner ? 1.9 : 1);
    _col.setRGB(colArr[i*3] * lift, colArr[i*3+1] * lift, colArr[i*3+2] * lift);
    fileMesh.setColorAt(i, _col);
  }
  fileMesh.instanceMatrix.needsUpdate = true;
  if (fileMesh.instanceColor) fileMesh.instanceColor.needsUpdate = true;
}

// supernode spheres: second instanced mesh, capacity = cluster count.
// count + matrices/colors are rewritten by refreshCollapse whenever the
// collapse set changes; hidden entirely while the toggle is off.
let CLMAX = -1;
for (let i = 0; i < N; i++) if (nodes[i].cluster > CLMAX) CLMAX = nodes[i].cluster;
const supMesh = new THREE.InstancedMesh(
  new THREE.SphereGeometry(1, 14, 10),
  new THREE.MeshLambertMaterial(),
  Math.max(1, CLMAX + 1)
);
supMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
supMesh.frustumCulled = false;
supMesh.count = 0;
supMesh.visible = false;
scene.add(supMesh);
// rebuild dpos + the collapse set from the CURRENT alphaTgt (called from
// applyVisibility right after per-node targets are set). Members of a
// collapsed cluster get alphaTgt 0 — the existing hide path fades their
// spheres out and drops their hub labels — while their dpos slots point
// at the centroid so syncEdgePos re-targets every edge they carry.
function refreshCollapse() {
  dpos.set(pos);
  supCollapsed.clear(); supMem.fill(0);
  if (!collapsed) {
    supMesh.count = 0; supMesh.visible = false;
    return;
  }
  const byC = {};
  for (let i = 0; i < N; i++) {
    if (alphaTgt[i] <= 0.05) continue;   // "visible" = would render under current filters/focus
    const c = nodes[i].cluster;
    if (c < 0) continue;
    (byC[c] = byC[c] || []).push(i);
  }
  // ascending cluster ids, ascending member indices — deterministic
  Object.keys(byC).map(Number).sort((a, b) => a - b).forEach(c => {
    const members = byC[c];
    if (members.length < 3) return;
    let cx = 0, cy = 0, cz = 0;
    members.forEach(i => { cx += pos[i*3]; cy += pos[i*3+1]; cz += pos[i*3+2]; });
    cx /= members.length; cy /= members.length; cz /= members.length;
    supCollapsed.set(c, { cx, cy, cz, n: members.length, first: members[0] });
    members.forEach(i => {
      dpos[i*3] = cx; dpos[i*3+1] = cy; dpos[i*3+2] = cz;
      alphaTgt[i] = 0;
      supMem[i] = 1;
    });
  });
  let k = 0;
  for (const c of [...supCollapsed.keys()].sort((a, b) => a - b)) {
    const s = supCollapsed.get(c);
    _dummy.position.set(s.cx, s.cy, s.cz);
    // radius encodes the merged mass: 4 + sqrt(memberCount)
    _dummy.scale.setScalar(4 + Math.sqrt(s.n));
    _dummy.updateMatrix();
    supMesh.setMatrixAt(k, _dummy.matrix);
    // cluster hue via a member's frozen color slot
    _col.setRGB(colArr[s.first*3], colArr[s.first*3+1], colArr[s.first*3+2]);
    supMesh.setColorAt(k, _col);
    k++;
  }
  supMesh.count = k;
  supMesh.visible = k > 0;
  supMesh.instanceMatrix.needsUpdate = true;
  if (supMesh.instanceColor) supMesh.instanceColor.needsUpdate = true;
  if (k > 0) supMesh.computeBoundingSphere();
}

// edges: LineMaterial renders true pixel-width lines (WebGL caps
// LineBasicMaterial linewidth at 1px); one linewidth per material, so links
// are split into three weight buckets, each its own LineSegments2 over an
// instanced geometry whose buffers mutate in place (no per-frame rebuild)
const MAXL = links.length;
// full-saturation per-type hue: calls neutral-white, signals amber,
// contains (inst/attach) cyan, anything else green
const TYPE_COLORS = {
  call:   new THREE.Color(0.85, 0.88, 0.92),
  signal: new THREE.Color(1.00, 0.70, 0.22),
  inst:   new THREE.Color(0.24, 0.80, 0.95),
  attach: new THREE.Color(0.24, 0.80, 0.95),
  var:    new THREE.Color(0.45, 0.90, 0.55),
};
// overview opacity rises gently with bucket weight: width stays the main
// weight cue — and heavy edges must never read dimmer than trivial ones
const BUCKETS = [
  { max: 1, width: 2, op: 0.28 },            // w <= 1
  { max: 4, width: 2.2, op: 0.32 },          // 2..4
  { max: Infinity, width: 3.5, op: 0.36 },   // >= 5
];
const bucketOf = new Int8Array(MAXL);
const slotOf = new Int32Array(MAXL);
const hwSlot = new Int32Array(MAXL).fill(-1);
const bucketPosIB = [], bucketColIB = [], bucketMat = [], bucketMesh = [];
{
  const counts = [0, 0, 0];
  // arcs get no straight slot (their slots would stay zero-filled at the
  // galaxy origin — NaN streak quads); they live entirely in the hw span
  const hwSet = new Set(hw.map(e => e[0]));
  links.forEach((l, i) => {
    const b = BUCKETS.findIndex(x => l.w <= x.max);
    bucketOf[i] = b;
    if (!hwSet.has(i)) slotOf[i] = counts[b]++;
  });
  // highways: each bezier arc is 16 static segments (32 vertices, 96 floats)
  // appended after the straight links inside its bucket. hwSlot[i] is the
  // vertex-float base of link i's arc, or -1 for straight links.
  const hwCounts = [0, 0, 0];
  hw.forEach(([li, pts]) => {
    const b = bucketOf[li];
    // hwCounts counts ARCS: the slot math multiplies by 32 vertices/arc.
    // (A previous `+= 32` here double-multiplied, spacing arcs 1024
    // vertices apart and leaving ~500 zero-filled segments at the galaxy
    // origin per arc — NaN quads in LineMaterial's normalize(0) rendered
    // as the close-zoom streak artifact.)
    hwSlot[li] = (counts[b] * 2 + hwCounts[b] * 32) * 3;
    hwCounts[b] += 1;
  });
  BUCKETS.forEach(b => {
    const bi = BUCKETS.indexOf(b);
    const geo = new LineSegmentsGeometry();
    geo.setPositions(new Float32Array((counts[bi] * 2 + hwCounts[bi] * 32) * 3));
    geo.setColors(new Float32Array((counts[bi] * 2 + hwCounts[bi] * 32) * 3));
    const mat = new LineMaterial({
      vertexColors: true, linewidth: b.width, worldUnits: false,
      // normal blending: additive stacking blew out hub fans into white glare
      // (hundreds of strands converge on 200+-degree hubs); overview opacity
      // capped per bucket so edges stay a quiet layer under the cluster hues
      transparent: true, opacity: b.op, alphaToCoverage: false,
      blending: THREE.NormalBlending, depthWrite: false,
    });
    mat.resolution.set(glW(), innerHeight);
    const mesh = new LineSegments2(geo, mat);
    mesh.frustumCulled = false;   // instance positions mutate per frame
    scene.add(mesh);
    bucketPosIB.push(geo.attributes.instanceStart.data);
    bucketColIB.push(geo.attributes.instanceColorStart.data);
    bucketMat.push(mat);
    bucketMesh.push(mesh);   // dash distances are computed on the LineSegments2
  });
}
// per-link ink state, written by the k-pass in applyVisibility and read by
// the picker: pick exactly what renders. <0.02 = no pickable ink (0 = no ink
// at all — filtered/budget-under-arc/fn-wire-replaced; 0.012 dead-end dim
// reads as nothing; GHOST_K 0.08 ghosts stay pickable)
const edgeK = new Float32Array(MAXL);
// shared 2D point-to-segment distance (screen space). Also records the hit
// param in segT so callers can interpolate depth at the hit point.
let segT = 0;
function segHit(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const L2 = dx * dx + dy * dy;
  let t = L2 > 0 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  segT = t;
  return Math.hypot(ax + t * dx - px, ay + t * dy - py);
}
let pickWireZ = 1;   // NDC depth of the last hit — node-front comparisons
// ONE picker for hover AND click: the meta of the wire under the pointer
// across every layer that actually RENDERS INK —
//   fnLines/fnQuiet arcs (fn wire mode), focusArcs (hub budget arcs) and
//   straight bucket chords (overview + signal/inst strands).
// Chords that render NO ink are skipped: fn-mode call chords between lit
// files (blacked — fn wires replace them; the old picker tested those and
// read as a "pre-curved collider"), budget chords under their arcs, and
// dead-end dim (0.012) ink that reads as nothing. Distances score to the
// INK EDGE (4px trunk beats 2px wire at ties; the quiet tier needs margin).
function pickWireMeta(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  const v = new THREE.Vector3(), w = new THREE.Vector3();
  let best = null, bestD = 8;
  for (const mesh of [fnLines, fnQuiet, focusArcs && focusArcs.lines]) {
    if (!mesh || !mesh.visible) continue;
    const a = mesh.geometry.attributes.instanceStart.array;
    const meta = mesh.userData.meta || [];
    const per = mesh.userData.seg || 8;
    const n = Math.min(a.length / 6, meta.length * per);
    const quiet = mesh === fnQuiet;
    for (let i = 0; i < n; i++) {
      const o = i * 6;
      v.set(a[o], a[o+1], a[o+2]).project(camera);
      if (v.z > 1) continue;
      w.set(a[o+3], a[o+4], a[o+5]).project(camera);
      if (w.z > 1) continue;
      const d = segHit(px, py,
        (v.x + 1) / 2 * rect.width, (1 - v.y) / 2 * rect.height,
        (w.x + 1) / 2 * rect.width, (1 - w.y) / 2 * rect.height);
      const m = meta[Math.floor(i / per)];
      // pick parity (sighting #11): a trunk meta on the wire tier must not
      // answer the picker when its conduit is LOD-culled — the card would
      // describe ink that isn't served (explanation without presence)
      if (m && m.kind === "trunk" && fnBus && busPts && !inkKeys.has(String(m.k))) continue;
      const dd = d - (m.kind === "trunk" ? 2 : 1) + (quiet ? 3 : 0);
      if (dd < bestD) { bestD = dd; best = m; pickWireZ = v.z + segT * (w.z - v.z); }
    }
  }
  for (let i = 0; i < links.length; i++) {
    // ink truth from the k-pass — pick exactly what renders (see edgeK decl)
    if (edgeK[i] < 0.02) continue;
    const arr = bucketPosIB[bucketOf[i]].array;
    const fo = hwSlot[i] >= 0 ? hwSlot[i] : slotOf[i] * 6;   // float offset
    const nseg = hwSlot[i] >= 0 ? 16 : 1;
    for (let s = 0; s < nseg; s++) {
      const o = fo + s * 6;
      v.set(arr[o], arr[o+1], arr[o+2]).project(camera);
      if (v.z > 1) break;
      w.set(arr[o+3], arr[o+4], arr[o+5]).project(camera);
      if (w.z > 1) break;
      const d = segHit(px, py,
        (v.x + 1) / 2 * rect.width, (1 - v.y) / 2 * rect.height,
        (w.x + 1) / 2 * rect.width, (1 - w.y) / 2 * rect.height);
      const dd = d - 1;
      if (dd < bestD) { bestD = dd; best = { kind: "link", li: i }; pickWireZ = v.z + segT * (w.z - v.z); }
    }
  }
  // click-parity pass (user r5): the WHITE trunk conduits + ivory junction
  // dots pick exactly like the colored bus wires — same metas, same tip.
  // Conduits get a wider forgiveness (tubes are 2-3x wire width); dots match
  // by projected center. Only while the tier actually renders (serve gate).
  if (fnBus && fnBus.visible && _lodServe) {
    if (busPts && busPtsMeta) {
      for (let i = 0; i < busPts.length; i++) {
        const m = busPtsMeta[i];
        if (!m) continue;
        const s = busPts[i];
        v.set(s.a[0], s.a[1], s.a[2]).project(camera);
        if (v.z > 1) continue;
        w.set(s.b[0], s.b[1], s.b[2]).project(camera);
        if (w.z > 1) continue;
        const d = segHit(px, py,
          (v.x + 1) / 2 * rect.width, (1 - v.y) / 2 * rect.height,
          (w.x + 1) / 2 * rect.width, (1 - w.y) / 2 * rect.height);
        const dd = d - 3;   // thick target: generous forgiveness
        // pick-vs-render parity (skeptic A8b): a culled chain (instance
        // scale parked at 0.0001) must not answer the picker — hovering
        // invisible ink is the tooltip-over-nothing class. Tapered EXPLAINED
        // EXIT stubs keep scale > 0 and stay pickable.
        const _sc = Math.hypot(fnBus.instanceMatrix.array[i*16],
                               fnBus.instanceMatrix.array[i*16+1],
                               fnBus.instanceMatrix.array[i*16+2]);
        if (_sc <= 0.001) continue;
        if (dd < bestD) { bestD = dd; best = m; pickWireZ = v.z + segT * (w.z - v.z); }
      }
    }
    if (juncPickInfo) {
      for (let i = 0; i < juncPickInfo.length; i++) {
        const m = juncPickInfo[i];
        const p = fnJDotPos;
        if (!m || !p) continue;
        v.set(p[i*3], p[i*3+1], p[i*3+2]).project(camera);
        if (v.z > 1) continue;
        // pick-vs-render parity (sighting #11 class 2): a culled bollard
        // (radius parked at 0.0001) must not answer the picker — a card on
        // invisible ink is explanation without presence
        if ((fnJDotR[i] || 0) <= 0.001) continue;
        m.__ji = i;   // presence check keys the card to this dot's live radius
        const dx = (v.x + 1) / 2 * rect.width - px, dy = (1 - v.y) / 2 * rect.height - py;
        const dd = Math.hypot(dx, dy) - 8;   // dot radius + forgiveness
        if (dd < bestD) { bestD = dd; best = m; pickWireZ = v.z; }
      }
    }
  }
  return best;
}
// focus-mode dash-flow (direction cue): world-units/s of dashOffset travel,
// re-read each tick
const FLOW_SPEED = 2.5;
let edgeFlowOn = false;   // set by applyVisibility, read by tick's dash pass

// typed strands between the same file pair run parallel instead of
// overlapping: each gets a slot offset perpendicular to the strand
// filter state shared by applyVisibility (colors) and syncEdgePos
// (geometry) — declared here because syncEdgePos runs at module init
let showTests = false;
const activeDirs = new Set();   // multi-select dir filter
const isTestNode = n => n.path.startsWith("tests/") || n.path.startsWith("tools/") ||
  n.path.slice(n.path.lastIndexOf("/") + 1).startsWith("test_");
// single predicate for "is this node filtered out right now" - used by
// nodeVisible (node alpha) and linkFiltered (edge collapse). applyVisibility's
// per-endpoint copy of this logic once drifted and painted black full-length
// wires over content.
const nodeFiltered = n => (!showTests && isTestNode(n)) ||
  (activeDirs.size && !activeDirs.has(n.dir));
const linkFiltered = l => nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t]);
const pairKey = (s, t) => s < t ? s + "_" + t : t + "_" + s;
const pairLinks = new Map();
links.forEach((l, i) => {
  const k = pairKey(l.s, l.t);
  if (!pairLinks.has(k)) pairLinks.set(k, []);
  pairLinks.get(k).push(i);
});
// baked highway arc points by link index — syncEdgePos is the SINGLE
// geometry owner for arcs (collapse + restore + spread attachment)
const hwPts = new Map(hw);
// hub-budget state lives here (above syncEdgePos, whose boot call reads
// budgetLit for the pin-topology attachment — a later let would be TDZ)
let budgetLit = null;   // link indices allowed to render lit this focus
let hoverEdgeLi = -1;   // wire-hover budget bypass (hovered ghost wire)
let hoverVisNode = -1;  // node-hover budget bypass (reveals the node's fan)
// focus-neighborhood compaction state (lit set = focus + 1-hop)
let posSaved = null;      // frozen-layout snapshot taken at focus entry
let compactTgt = null;    // compacted targets (Float32Array 3N) or null
let compactIdx = [];      // lit-set node indices the ease animates
let compactAnim = null;   // { t0, dur } while the ease runs, else null
let compactScale = 1;     // exact-scale finisher factor (via __dbg)
let compactBallR = 0;     // focus ball radius — LOD gate for the fn-wire layer
let focusFileIdx = -1;   // the level-0 seed of the active focus (compaction)
let compactOverlaps = 0;  // residual violations after the pass (must be 0)
// attachment trim at node i's CURRENT body: its own sphere (world radius
// sizes*1.1*sqrt(spread) + 2 margin), or the supernode (4 + sqrt(members)
// + 2) when i's cluster is collapsed
const trimAt = i => supMem[i]
  ? 4 + Math.sqrt(supCollapsed.get(nodes[i].cluster).n) + 2
  : sphR(i) + 2;
function syncEdgePos() {
  links.forEach((l, i) => {
    if (hwSlot[i] >= 0) {
      // ATTACHMENT: highway arcs are geometry like any other edge — rescale
      // the baked arc shape affinely around the layout centroid so endpoints
      // track their (moved) nodes exactly. Arc data shape: 17 [x,y,z] points
      // (nested arrays — flat indexing here once produced NaN, killing every
      // line in the affected buckets). Filtered/ghost arcs collapse exactly
      // like straight edges — black color alone is NOT hidden under normal
      // blending.
      const arr = bucketPosIB[bucketOf[i]].array, b = hwSlot[i];
      const pts = hwPts.get(i);
      if (!pts || !pts.length) return;
      const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
      const hidden = linkFiltered(l) || ghost;
      for (let v = 0; v < 16; v++) {
        const o = b + v * 6;
        if (hidden) {
          const sx = dpos[l.s*3], sy = dpos[l.s*3+1] + 0.05, sz = dpos[l.s*3+2];
          arr[o] = sx; arr[o+1] = sy; arr[o+2] = sz;
          arr[o+3] = sx; arr[o+4] = sy; arr[o+5] = sz;
          continue;
        }
        const A = pts[v], B = pts[v + 1];
        if (!A || !B) return;
        // per-endpoint re-rooted affine: the baked arc is mapped into live
        // space anchored at each endpoint's CURRENT attachment point
        // (dpos — supernode centroid when collapsed, the node itself
        // otherwise, which reproduces the plain centroid affine exactly),
        // then blended along the arc: v=0 maps purely through the s-anchor,
        // v=16 purely through the t-anchor. Retargets arcs onto supernodes
        // without disturbing uncollapsed ones.
        const p0 = pts[0], p16 = pts[16];
        const sax = dpos[l.s*3] + (A[0] - p0[0]) * spread,
              say = dpos[l.s*3+1] + (A[1] - p0[1]) * spread,
              saz = dpos[l.s*3+2] + (A[2] - p0[2]) * spread;
        const tax = dpos[l.t*3] + (A[0] - p16[0]) * spread,
              tay = dpos[l.t*3+1] + (A[1] - p16[1]) * spread,
              taz = dpos[l.t*3+2] + (A[2] - p16[2]) * spread;
        const sbx = dpos[l.s*3] + (B[0] - p0[0]) * spread,
              sby = dpos[l.s*3+1] + (B[1] - p0[1]) * spread,
              sbz = dpos[l.s*3+2] + (B[2] - p0[2]) * spread;
        const tbx = dpos[l.t*3] + (B[0] - p16[0]) * spread,
              tby = dpos[l.t*3+1] + (B[1] - p16[1]) * spread,
              tbz = dpos[l.t*3+2] + (B[2] - p16[2]) * spread;
        const u1 = v / 16, u2 = (v + 1) / 16;
        let ax = sax + (tax - sax) * u1, ay = say + (tay - say) * u1, az = saz + (taz - saz) * u1;
        let bx = sbx + (tbx - sbx) * u2, by = sby + (tby - sby) * u2, bz = sbz + (tbz - sbz) * u2;
        // surface trim at the two node-attached ends (baked arc endpoints
        // sit on the node centers): pull the terminal vertex along its own
        // segment by the node's world radius + margin, same rule as
        // straight edges. Interior segments untouched; a degenerate
        // segment (tiny spread) keeps its points rather than feed
        // normalize(0).
        if (v === 0 || v === 15) {
          const vx = bx - ax, vy = by - ay, vz = bz - az;
          const vl = Math.sqrt(vx*vx + vy*vy + vz*vz);
          const tr = trimAt(v === 0 ? l.s : l.t);
          if (vl > tr + 0.05) {
            const k = tr / vl;
            if (v === 0) { ax += vx * k; ay += vy * k; az += vz * k; }
            else { bx -= vx * k; by -= vy * k; bz -= vz * k; }
          }
        }
        arr[o]   = ax; arr[o+1] = ay; arr[o+2] = az;
        arr[o+3] = bx; arr[o+4] = by; arr[o+5] = bz;
      }
      bucketPosIB[bucketOf[i]].needsUpdate = true;
      return;
    }
    const s = l.s * 3, t = l.t * 3;
    // single owner of edge geometry: applyVisibility re-runs this after
    // every filter change, so filtered links collapse here and unfiltered
    // links always restore full positions.
    // ghost = both endpoints outside the current focus (alpha ~ 0): a line
    // between two invisible nodes is pure noise — collapse it too.
    const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
    if (linkFiltered(l) || ghost) {
      const a0 = bucketPosIB[bucketOf[i]].array, o0 = slotOf[i] * 6;
      a0[o0] = dpos[s]; a0[o0+1] = dpos[s+1]; a0[o0+2] = dpos[s+2];
      // tiny offset: an exactly-zero-length segment gives LineMaterial's
      // normalize(0) NaN screen quads (driver-dependent streaks)
      a0[o0+3] = dpos[s]; a0[o0+4] = dpos[s+1] + 0.05; a0[o0+5] = dpos[s+2];
      bucketPosIB[bucketOf[i]].needsUpdate = true;
      return;
    }
    let ox = 0, oy = 0;
    const g = pairLinks.get(pairKey(l.s, l.t));
    if (g.length > 1) {
      const slot = g.indexOf(i) - (g.length - 1) / 2;
      // perpendicular to the strand in the xy-plane; +X fallback when the
      // strand is nearly parallel to Z (cross with Z degenerates)
      const dx = dpos[t] - dpos[s], dy = dpos[t+1] - dpos[s+1];
      let px = dy, py = -dx;
      const pl = Math.sqrt(px*px + py*py);
      if (pl < 0.001) { px = 1; py = 0; } else { px /= pl; py /= pl; }
      ox = px * slot * 9; oy = py * slot * 9;
    }
    const a = bucketPosIB[bucketOf[i]].array, o = slotOf[i] * 6;
    // surface trim: pull each endpoint out of its attachment body (own
    // sphere or supernode — trimAt) so strands meet the surface, not the
    // center. A strand shorter than both trims would invert and feed
    // normalize(0) — fall back to the same stub the ghost path uses.
    const ex = dpos[t] - dpos[s], ey = dpos[t+1] - dpos[s+1], ez = dpos[t+2] - dpos[s+2];
    const el = Math.sqrt(ex*ex + ey*ey + ez*ez);
    const trimS = trimAt(l.s), trimT = trimAt(l.t);
    if (el - trimS - trimT <= 0.05) {
      a[o] = dpos[s]; a[o+1] = dpos[s+1]; a[o+2] = dpos[s+2];
      a[o+3] = dpos[s]; a[o+4] = dpos[s+1] + 0.05; a[o+5] = dpos[s+2];
      return;
    }
    const ndx = ex / el, ndy = ey / el, ndz = ez / el;
    // pin topology (focus): budgeted lit wires attach at distinct points
    // around the sphere rim — rotate the attachment bearing by a small
    // deterministic per-link angle (Rodrigues around an axis perpendicular
    // to the strand) so wires leaving a hub land at visibly separate pins
    // instead of stacking on one bearing line. Rotation slides the trim
    // point ALONG the surface, so the endpoint always sits on-rim.
    // Overview and ghost edges keep the exact center-to-center bearing.
    // budgetLit !== null only while a focus is active.
    let rx = ndx, ry = ndy, rz = ndz;
    if (budgetLit && budgetLit.has(i)) {
      const spin = ((i % 9) - 4) * 0.055;   // ±0.22 rad deterministic fan
      if (spin !== 0) {
        let ax = ndy, ay = -ndx, az = 0;    // n × Z
        if (ndx * ndx + ndy * ndy < 1e-6) { ax = 0; ay = ndz; az = -ndy; }  // n × X
        const al = Math.sqrt(ax * ax + ay * ay + az * az) || 1;
        ax /= al; ay /= al; az /= al;
        const cs = Math.cos(spin), sn = Math.sin(spin);
        const wx = ay * ndz - az * ndy, wy = az * ndx - ax * ndz, wz = ax * ndy - ay * ndx;
        rx = ndx * cs + wx * sn; ry = ndy * cs + wy * sn; rz = ndz * cs + wz * sn;
      }
    }
    a[o]   = dpos[s] + rx * trimS + ox; a[o+1] = dpos[s+1] + ry * trimS + oy; a[o+2] = dpos[s+2] + rz * trimS;
    a[o+3] = dpos[t] - rx * trimT + ox; a[o+4] = dpos[t+1] - ry * trimT + oy; a[o+5] = dpos[t+2] - rz * trimT;
  });
   bucketPosIB.forEach(ib => { ib.needsUpdate = true; });
   // dash support: lineDistance attributes must track every geometry
   // rewrite, but only USE_DASH reads them - and every edgeFlowOn false->true
   // transition flows through the applyVisibility() call that just ran this
  // syncEdgePos, so distances are fresh exactly when dashes can appear.
  if (edgeFlowOn) bucketMesh.forEach(ms => ms.computeLineDistances());
}
syncEdgePos();
// boot arc pre-fill deleted: syncEdgePos above already wrote the arcs
// (with surface trim), and boot applyVisibility re-runs it before the
// first render - the old raw re-write was dead weight + a stale comment.
// base colors: pure type hue scaled by weight-as-brightness; the TARGET-end
// vertex is tinted 55% toward the target node's cluster color so edges
// show direction (start vertex keeps the pure type hue)
const eColBase = new Float32Array(MAXL * 6);
links.forEach((l, i) => {
  const tc = TYPE_COLORS[l.ty] || TYPE_COLORS.var;
  const wb = 0.45 + Math.min(1, l.w / 6) * 0.55;
  const o = i * 6, t3 = l.t * 3;
  eColBase[o]   = tc.r * wb; eColBase[o+1] = tc.g * wb; eColBase[o+2] = tc.b * wb;
  eColBase[o+3] = tc.r * wb + (colArr[t3]   - tc.r * wb) * 0.55;
  eColBase[o+4] = tc.g * wb + (colArr[t3+1] - tc.g * wb) * 0.55;
  eColBase[o+5] = tc.b * wb + (colArr[t3+2] - tc.b * wb) * 0.55;
});
// boot color pre-fill deleted: nothing renders before boot applyVisibility()
// rewrites every link's color (bucket buffers start zeroed; first tick is
// after that pass).
bucketColIB.forEach(ib => { ib.needsUpdate = true; });

// ---- camera tween + focus back-stack -----------------------------------------
// 400ms ease-out camera transitions replace teleporting focus() jumps
let camTween = null;
function tweenCamTo(toTarget, toCam) {
  camTween = { t0: performance.now(), dur: 400,
    fromT: controls.target.clone(), toT: toTarget.clone(),
    fromC: camera.position.clone(), toC: toCam.clone() };
}
// focus back-stack: interactions that re-root the focus push the previous
// seeds + camera pose; Backspace / the ‹ chip pops back to them
let focusStack = [];
function pushFocusState() {
  if (!focusSeeds.size) return;
  focusStack.push({ seeds: [...focusSeeds],
    target: controls.target.clone(), cam: camera.position.clone() });
  if (focusStack.length > 20) focusStack.shift();
}
function popFocus() {
  const f = focusStack.pop();
  if (!f) return;
  focusSeeds.clear();
  f.seeds.forEach(s => focusSeeds.add(s));
  tweenCamTo(f.target, f.cam);
  applyVisibility();
}
function tick() {
  const nowT = performance.now();
  // map selection pulse: keep the pane repainting while the amber ring
  // breathes; drop it at end of life (pane closed -> pulse frozen, not lost)
  if (mapPulse && mapVisible) {
    if (performance.now() - mapPulse.t0 > MAP_PULSE_MS) mapPulse = null;
    drawMapPane();
  }
  // dash-flow while focusing: offsets walk each edge along its s→t vertex
  // order (caller→callee), with a per-bucket phase hashed from the bucket
  // index so the flow reads per-edge instead of one global march. Overview
  // keeps dashed fully off — USE_DASH leaves the shader, so nothing
  // shimmers at rest.
  const flowT = nowT / 1000;
  bucketMat.forEach((em, i) => {
    if (edgeFlowOn) {
      if (!em.dashed) { em.dashed = true; em.dashSize = 8; em.gapSize = 5; em.needsUpdate = true; }
      const period = em.dashSize + em.gapSize;
      const hashI = (i * 2654435761) % 997 / 997 * period;
      em.dashOffset = -((flowT * FLOW_SPEED + hashI * 13) % period);
    } else if (em.dashed) {
      em.dashed = false; em.dashOffset = 0; em.needsUpdate = true;
    }
  });
  // focus arcs share the dash-flow direction cue (single material — the
  // per-link phase lives in the lineDistance attribute)
  if (focusArcs && focusArcs.lines.visible && edgeFlowOn) {
    const fp = focusArcs.mat.dashSize + focusArcs.mat.gapSize;
    focusArcs.mat.dashOffset = -((flowT * FLOW_SPEED) % fp);
  }
  // fn wires: dash-flow direction cue + LOD fade — the layer dissolves as
  // the camera pulls away from the ball (1px salad reads as noise from afar)
  if (fnLines && fnLines.visible && edgeFlowOn) {
    const fp = 7 + 4;   // dashSize + gapSize of the fn-wire dashed material
    fnLines.material.dashOffset = -((flowT * FLOW_SPEED) % fp);
  }
  if (fnLines && compactBallR > 0 && hubRing) {
    const cd = camera.position.distanceTo(hubRing.position);
    let lod = Math.max(0, Math.min(1, (2.8 * compactBallR - cd) / (0.9 * compactBallR)));
    // the fade is a FAR-ZOOM noise control; when the focus-state gate
    // serves the tier the paint must be FULL — dim dots at the default
    // focus camera (0.60/0.40) made a geometrically-served state read
    // as spheres-only (skeptic r5-final objection)
    if (_lodServe) lod = 1;
    fnLines.material.opacity = 0.75 * lod;
    if (fnQuiet) fnQuiet.material.opacity = 0.16 * lod;
    if (fnBus) fnBus.material.opacity = 0.35 + 0.65 * lod;
    if (fnJDot) fnJDot.material.opacity = 0.35 + 0.55 * lod;
    if (fnArrows) fnArrows.material.opacity = 0.9 * lod;
    _oDot = fnJDot ? 0.35 + 0.55 * lod : 0;
    _oArrow = fnArrows ? 0.9 * lod : 0;
  } else { _oDot = 0; _oArrow = 0; }
  // conduit width is SCREEN-CONSTANT per segment: radius ∝ each segment's own
  // distance to the camera (~2.4px on screen at every depth, next to 1px wires)
// round-5 LOD gate (user-acceptance): a bus element is visible only when
// the boxes it serves RESOLVE on screen — at far zoom whole trunks read
// as "nowhere to nowhere" and sub-junction dots as droplets on wires.
// Pure function of camera pose + build data; no layout change.
function busLodInit() {
  const hpx = renderer.domElement.clientHeight || 900;
  const wuPerPx = 2 * Math.tan(camera.fov * Math.PI / 360) / hpx;
  const memo = new Map();
  // REF-normalized px (user directive, round 5): all screen laws live in
  // REFERENCE pixels — px a size would have on a nominal 900px-tall canvas.
  // Thresholds become resolution-independent; a probe at any viewport reads
  // the same numbers.
  const REFH = 900, refK = REFH / hpx;
  const pxOf = fi => {
    let v = memo.get("px" + fi);
    if (v === undefined) {
      const sc = (fnBoxScale && fnBoxScale.get(fi)) || 4;
      const d = Math.hypot(pos[fi*3] - camera.position.x,
                           pos[fi*3+1] - camera.position.y,
                           pos[fi*3+2] - camera.position.z) || 1;
      v = (sc / (wuPerPx * d)) * refK;
      memo.set("px" + fi, v);
    }
    return v;
  };
  // FOCUS-STATE master gate (skeptic r5 objection): when the user asks for
  // the fn layer (focus active) and the FOCUS file's own box is readable,
  // serve the whole bus tier — camera distance alone gated the busiest
  // hub's d2 state (the top .tscn hub: 21/21 bollards at radius 0 because
  // its neighborhood shells sit farther out than the entry scene's). Zoomed-out
  // overview (user's droplet state) still gates: focus box < floor there.
  // Rotation-invariant master gate: autoRotate orbits the camera at constant
  // camDist, so a sphere-relative px floor oscillates with spin phase and
  // the tier flickers gate<->serve (measured: servePx 2.34 vs 1.80 across
  // reloads at the same nominal state). camDist-to-target is orbit-stable:
  // serve iff the camera is inside 2.2 compact-ball radii of the focus
  // (default focus camera = 1.69R serves; the user's droplet state = 7.9R
  // stays gated; ~1.5x default zoom-out is the cutoff)
  const serveAll = focusFileIdx >= 0 && compactBallR > 0 &&
    camera.position.distanceTo(controls.target) <= 2.2 * compactBallR;
  _lodServe = serveAll;
  if (fnLodV) { fnLodV.serveFi = focusFileIdx; fnLodV.servePx = focusFileIdx >= 0 ? pxOf(focusFileIdx) : -1; }
  // TIER/NODE UNIFICATION (sighting #9 + slider regression): res() is the
  // ONE focus-set oracle — a file serves the wire tier iff it is LIT
  // (alphaTgt > 0.5 encodes depth + filters + the 1-hop ghost law) AND its
  // box resolves. Before this, a depth-3 ghost (alpha 0.04) with a big box
  // still served corridors = wires to invisible files at any zoom.
  const res = fi => (serveAll || pxOf(fi) >= 2.5) && alphaTgt[fi] > 0.5;
  // the USER's 735h window (round-5b boot-straggler fix: pair census found
  // 3-4 dots over 0.8-1.2px boxes; 2.2 ref-px = 1.8 CSS px there)
  // chevron floor is LOWER: a delivery mark may ride a 1.5-2.5px box —
  // visible at probe-d2 zooms, where the 2.5 floor thinned on-screen
  // chevrons 18->3 (too sparse for route tracing). Below 1.5 the box is
  // a speck and the mark reads as noise on nothing
  const resA = fi => pxOf(fi) >= 1.5 && alphaTgt[fi] > 0.5;
  const tkPx = tk => {
    const p = String(tk).split(">");
    return p.length === 2 ? Math.min(pxOf(+p[0]), pxOf(+p[1])) : pxOf(+String(tk).split("|")[1]);
  };
  const trOK = tk => {
    const p = String(tk).split(">");
    return p.length === 2 ? res(+p[0]) && res(+p[1]) : res(+String(tk).split("|")[1]);
  };
  const stOK = fi => {
    if (!res(fi)) return false;
    const tks = stationTks && stationTks.get(fi);
    // no bare station heads: a station tree with NO trunk leaving it renders
    // dots + legs connected to nothing ("node heads in the void", 4th user
    // sighting) — the chain (legs + bollards) only serves when >=1 trunk
    // beyond the station also serves
    if (!tks || !tks.length) return false;
    for (const tk of tks) if (trOK(tk)) return true;
    return false;
  };
  // CHAIN-INTEGRITY law (user defect: orphan junction legs): a leg ("L|fi|…")
  // is connector ink between a file box and its station — it may serve ONLY
  // when its OWN file's box resolves on screen (strict px floor, never
  // serveAll-blind: the waiver exists for the FOCUS file's tier, and keying
  // it on the leg's own file keeps that accommodation intact) AND its
  // station context serves (stOK: station bollard + >=1 serving trunk).
  // Both ends resolved or the whole chain culls (all FS segments share k).
  const legOK = tk => {
    const fi = +String(tk).split("|")[1];
    // PERCEPTUAL_ANCHOR: 2.5px resolves geometrically but reads as a
    // speck — a leg chained to a sub-8px box is floating ink (3rd user
    // sighting, VLM-confirmed). Anchor READS when the box naturally
    // measures ANCHOR_PX OR a serving corridor has boosted the file's
    // SPRITE to the floor (corridor-complete law: size, not brightness —
    // dim files keep their dim color). Plus station context (stOK):
    // no leg without its trunk, no trunk without both ends.
    return (pxOf(fi) >= ANCHOR_PX || anchorBoost[fi] > 0) && stOK(fi);
  };
  return { res, resA, trOK, stOK, legOK, pxOf, tkPx };
}
  fnLodV = { bollardsShown: 0, bollardsGated: 0, conduitsShown: 0, conduitsGated: 0,
             chevShown: 0, minChevPx: Infinity, minServedBoxPx: Infinity, minBollardPx: Infinity,
             serveFi: -1, servePx: -1, oDot: _oDot, oArrow: _oArrow };
  _lod = (fnBus || fnJDot) ? busLodInit() : null;   // after fnLodV: it stamps serveFi/servePx
  // endpoint anchor demands are per-frame (camera-pose dependent): reset,
  // then arcs, serving corridors and serving legs add theirs
  anchorBoost.fill(0);
  if (focusArcs && focusArcs.lines.visible && focusArcs.lines.userData.meta)
    for (const m of focusArcs.lines.userData.meta) {
      anchorBoost[links[m.li].s] = ANCHOR_PX; anchorBoost[links[m.li].t] = ANCHOR_PX;
    }
  // leg termini must LAND: a served leg whose BOTH ends project outside the
  // viewport renders as floating mid-view debris ("starting nowhere and
  // ending nowhere", user sighting #8 — orbit sweep found up to 11 such legs
  // at close zooms az 120-180). Build the per-key on-screen map before the
  // serve loop; legs failing it cull whole-chain (all FS segments share k).
  // Trunks stay exempt (edge-exiting highways read as leaving; user ruling).
  legTermOn.clear();   // module-scope Map (probe hook): chain-key -> terminus on-screen
  if (fnBus && busPts) {
    const v3 = new THREE.Vector3();
    const spans = new Map();
    for (const s of busPts) {
      let sp = spans.get(s.k);
      if (!sp) spans.set(s.k, { a: s.a, b: s.b });   // first segment's a = chain start
      else sp.b = s.b;                               // last segment's b = chain terminus
    }
    for (const [k, sp] of spans) {
      // SIGHTING #10: both rejection clauses here were PROJECTION-based —
      // a terminus sliding off-frame (|ndc|>1.05) or a projected span
      // crossing 35% of the diagonal flipped the WHOLE chain off in one
      // wheel click (en-bloc corridor+junction vanish: the cull also drops
      // its stAttKey, so the bollard goes too). Zoom law is now monotonic:
      // (a) terminus on-screen-ness is NOT a cull reason — ink is
      // world-anchored (draws-to-anchor), a wire exiting the frame is a
      // highway leaving view, not float; (b) the 35%-diagonal sprawl cap
      // (ruling b) is evaluated at the NOMINAL focus framing
      // (2.2·compactBallR, the serveAll reference distance), so it culls
      // corridors that would sprawl where they are MEANT to be read, and
      // zooming in can never cross it (px at nominal dist is
      // zoom-invariant; pan-invariant by construction).
      let ok = true;
      const wu = Math.hypot(sp.b[0] - sp.a[0], sp.b[1] - sp.a[1], sp.b[2] - sp.a[2]);
      if (isFinite(wu) && compactBallR > 0) {
        const cw = renderer.domElement.clientWidth || 1600;
        const ch2 = renderer.domElement.clientHeight || 900;
        const nominalD = 2.2 * compactBallR;
        const pxN = wu * (ch2 / 2) / (Math.tan(camera.fov * Math.PI / 360) * nominalD);
        if (pxN > 0.35 * Math.hypot(cw, ch2)) ok = false;
      }
      legTermOn.set(k, ok);
    }
  }
  // highways). Trunk keys are "sf>tf".
  if (_lod && fnBus && busPts)
    for (const s of busPts) {
      if (typeof s.k !== "string" || s.k.charCodeAt(0) === 76) continue;
      if (!_lod.trOK(s.k)) continue;
      const p = s.k.split(">");
      if (p.length === 2) { anchorBoost[+p[0]] = ANCHOR_PX; anchorBoost[+p[1]] = ANCHOR_PX; }
    }
  if (fnBus && busPts) {
    stAttKey.clear(); chainGate.clear(); inkKeys.clear();
    let dirty = false;
    const lod = _lod;
    const a = fnBus.instanceMatrix.array;
    const stubPts = [];
    let curK = null, curJ = 0;
    const _sv = new THREE.Vector3();
    // per-key segment totals (bridge taper runs from the focus-side end)
    const segN = new Map();
    for (const s of busPts)
      if (typeof s.k === "string") segN.set(s.k, (segN.get(s.k) || 0) + 1);
    for (let i = 0; i < busPts.length; i++) {
      const s = busPts[i];
      const d = Math.hypot((s.a[0]+s.b[0])/2 - camera.position.x,
                           (s.a[1]+s.b[1])/2 - camera.position.y,
                           (s.a[2]+s.b[2])/2 - camera.position.z);
      const isLeg = typeof s.k === "string" && s.k.charCodeAt(0) === 76;
      const termOk = !isLeg || legTermOn.get(s.k) !== false;
      let gateOk = lod ? (isLeg ? (lod.legOK(s.k) && termOk) : lod.trOK(s.k)) : true;
      if (!isLeg && gateOk && lod) {
        // sighting #9: bridge trunks (station↔station arcs whose files
        // attach directly, no fan) read as wire-in-the-void once either
        // anchor box drops below the 8px perceptual floor. The corridor
        // law now points BOTH ways: a trunk renders only when BOTH its
        // stations' file anchors read (box >= ANCHOR_PX or boosted).
        const pp = String(s.k).split(">");
        if (pp.length === 2) {
          const anch = fi => lod.pxOf(fi) >= ANCHOR_PX || anchorBoost[fi] > 0;
          if (!(anch(+pp[0]) && anch(+pp[1]))) gateOk = false;
        }
      }
      // sighting #10 forensic: first failing gate per chain (extend-only
      // probe surface — 'served' | 'legOK' | 'termOn' | 'trOK' | 'bridgeAnch')
      if (typeof s.k === "string" && !chainGate.has(s.k))
        chainGate.set(s.k, gateOk ? "served" :
          (isLeg ? (lod && !lod.legOK(s.k) ? "legOK" : "termOn")
                 : (lod && !lod.trOK(s.k) ? "trOK" : "bridgeAnch")));
      if (gateOk && typeof s.k === "string") {
        // empty-station law: bollards render only against attachments that
        // ACTUALLY served this frame — heads key on trunk endpoints "T|fi",
        // sub dots on their leg key (all FS segments of a chain share k)
        if (isLeg) stAttKey.add(s.k);
        else { const tp = String(s.k).split(">");
          if (tp.length === 2) { stAttKey.add("T|" + (+tp[0])); stAttKey.add("T|" + (+tp[1])); } }
      }
      if (fnLodV) {
        if (gateOk) { fnLodV.conduitsShown++;
          fnLodV.minServedBoxPx = Math.min(fnLodV.minServedBoxPx, lod ? lod.tkPx(s.k) : Infinity); }
        else fnLodV.conduitsGated++;
      }
      // EXPLAINED EXIT (user amendment): a chain culled while the user has
      // interaction context on it must not vanish silently — taper a short
      // stub from its attached end (radius ramping to 0 over 3 segments)
      // and label the dissolve point. Context = the chain's rider file is
      // LIT (focus file itself, or within the focus..depth visible set —
      // a fan's riders ARE the focus callees). Three legal states only:
      // ATTACHED / ABSENT / EXPLAINED EXIT. Overview stays quiet.
      if (s.k !== curK) { curK = s.k; curJ = 0; } else curJ++;
      const base = Math.max(0.05, Math.min(12, d * 0.0037 * (s.rf || 1)));
      let taperF = 0, destFi = -1, jx = curJ;
      if (!gateOk && focusFileIdx >= 0 && lod) {
        if (isLeg && lod.legOK(s.k)) {
          const pp = String(s.k).split("|");
          if (alphaTgt[+pp[1]] > 0.5) {
            taperF = Math.max(0, 1 - curJ / 3); destFi = +pp[1];
          }
        } else if (!isLeg && lod.trOK(s.k)) {
          const pp = String(s.k).split(">");
          if (pp.length === 2 && (+pp[0] === focusFileIdx || +pp[1] === focusFileIdx)) {
            // taper from the FOCUS-side end of the bridge
            const n = segN.get(s.k) || 1;
            jx = +pp[1] === focusFileIdx ? (n - 1 - curJ) : curJ;
            taperF = Math.max(0, 1 - jx / 3);
            destFi = +pp[0] === focusFileIdx ? +pp[1] : +pp[0];
          }
        }
      }
      const rT = gateOk ? base : (taperF > 0 ? base * taperF : 0.0001);
      if (taperF > 0 && jx === 2) {
        _sv.set((s.a[0]+s.b[0])/2, (s.a[1]+s.b[1])/2, (s.a[2]+s.b[2])/2).project(camera);
        if (isFinite(_sv.x) && _sv.z < 1)
          stubPts.push({ nx: _sv.x, ny: _sv.y, fi: destFi, dest: String(s.k) });
      }
      const f = rT / fnBusRi[i];
      // NO EXPLANATION WITHOUT PRESENCE (sighting #11): record which chains
      // carry ink this frame — the pinned wireTip's lifetime checks against
      // this set every tick and hides the moment its anchor loses ink
      if (typeof s.k === "string" && rT > 0.002) inkKeys.add(s.k);
      if (Math.abs(f - 1) > 0.06) {
        const o = i * 16;
        a[o] *= f; a[o+1] *= f; a[o+2] *= f;
        a[o+8] *= f; a[o+9] *= f; a[o+10] *= f;
        fnBusRi[i] = rT;
        dirty = true;
      }
    }
    if (dirty) fnBus.instanceMatrix.needsUpdate = true;
    stubExits = stubPts;
  }
  updateStubLabs();
  // bollards read at ANY camera distance: a 1-2px dot in a dark knot is not
  // a reroute node you can see — radius ∝ camera distance (~2.5-4px on screen)
  if (fnJDot && fnJDotPos) {
    let dirty = false;
    const a = fnJDot.instanceMatrix.array;
    for (let i = 0; i < fnJDotR.length; i++) {
      const d = Math.hypot(fnJDotPos[i*3] - camera.position.x,
                           fnJDotPos[i*3+1] - camera.position.y,
                           fnJDotPos[i*3+2] - camera.position.z);
      // SCREEN-CONSTANT bollard radii, the same law class as conduit width
      // (skeptic B3: a world-radius floor shrinks with zoom while trunks
      // hold ~screen px — hierarchy inverted at hubzoom). Station ≈11px,
      // sub-junction ≈5.7px, Jof ≈5.4px (px-factor ~1073), scaled by the
      // dot's size factor — one dominant merge dot per station, tree dots
      // clearly subordinate (round-3 crop verdict: "cluster of mid-sized
      // balls" with 9px/6px was still too flat).
      const kf = fnJDotK ? fnJDotK[i] : 1;
      const lod = _lod;
      const hpxr = renderer.domElement.clientHeight || 900;
      let gateOk = lod ? (fnJDotSt[i] ? lod.stOK(fnJDotOf[i]) : lod.res(fnJDotOf[i])) : true;
      // sighting #9 (2) + empty-station law (user report): a bollard renders
      // only when its attachment ACTUALLY SERVED this frame — station heads
      // need a serving trunk endpoint ("T|fi" in stAttKey), sub dots their
      // own leg chain. Inventory legs/trunks do NOT earn ink (the user saw
      // fully-empty station bollards floating at the landing pose).
      if (gateOk && fnJDotKey && fnJDotKey[i] && !stAttKey.has(fnJDotKey[i]))
        gateOk = false;
      // viewport-FRACTION law (user directive r5): the world-slope law
      // r = kf*0.0102*d keeps every element a constant FRACTION of the
      // frame height — px-at-nominal-900 (ref-px) is invariant; the
      // skeptic's 1600x900 numbers are exactly the round-4 values
      const rRef = d * 0.0102 * kf;
      if (fnLodV) {
        if (gateOk) { fnLodV.bollardsShown++;
          fnLodV.minServedBoxPx = Math.min(fnLodV.minServedBoxPx, lod ? lod.pxOf(fnJDotOf[i]) : Infinity);
          const wpp = 2 * Math.tan(camera.fov * Math.PI / 360) / hpxr;
          fnLodV.minBollardPx = Math.min(fnLodV.minBollardPx, ((2 * rRef) / (wpp * d)) * (900 / hpxr)); }
        else fnLodV.bollardsGated++;
      }
      const rT = gateOk ? rRef : 0.0001;
      const f = rT / fnJDotR[i];
      if (Math.abs(f - 1) > 0.06) {
        const o = i * 16;
        a[o] *= f; a[o+5] *= f; a[o+10] *= f;   // uniform sphere scale
        fnJDotR[i] = rT;
        dirty = true;
      }
    }
    if (dirty) fnJDot.instanceMatrix.needsUpdate = true;
  }
  // NO EXPLANATION WITHOUT PRESENCE (sighting #11): the pinned card's
  // lifetime is frame-synced to its anchor's rendered state — chain keys
  // must carry ink (inkKeys), bollards must render (fnJDotR), plain wires
  // keep at least one lit endpoint. Anchor gone -> card hides, no exceptions.
  if (wireTipAnchor && wireTipEl.style.display !== "none") {
    const a = wireTipAnchor;
    let present = true;
    if ((a.kind === "trunk" || a.kind === "jleg") && a.k !== undefined)
      present = inkKeys.has(String(a.k));
    else if (a.__ji !== undefined)
      present = (fnJDotR[a.__ji] || 0) > 0.001;
    else if (a.kind === "wire" && a.a !== undefined && a.b !== undefined)
      present = (alphaTgt[a.a] || 0) > 0.5 || (alphaTgt[a.b] || 0) > 0.5;
    if (!present) hideWireTip();
  }
  // chevron aim is camera-dependent: recompute every frame
  aimArrows();
  // camera tween (focus / back-stack); a user drag cancels it
  if (camTween) {
    const u = Math.min(1, (performance.now() - camTween.t0) / camTween.dur);
    const e = 1 - Math.pow(1 - u, 3);
    controls.target.lerpVectors(camTween.fromT, camTween.toT, e);
    camera.position.lerpVectors(camTween.fromC, camTween.toC, e);
    if (u >= 1) camTween = null;
  }
  // focus-neighborhood compaction ease: pos walks from the saved layout to
  // the compacted targets (cubic ease-out); syncEdgePos re-derives the fan
  // per frame so wires stay attached while the set pulls together
  if (compactAnim) {
    const u = Math.min(1, (performance.now() - compactAnim.t0) / compactAnim.dur);
    const e = 1 - Math.pow(1 - u, 3);
    for (const i of compactIdx) {
      pos[i*3]   = posSaved[i*3]   + (compactTgt[i*3]   - posSaved[i*3])   * e;
      pos[i*3+1] = posSaved[i*3+1] + (compactTgt[i*3+1] - posSaved[i*3+1]) * e;
      pos[i*3+2] = posSaved[i*3+2] + (compactTgt[i*3+2] - posSaved[i*3+2]) * e;
    }
    syncEdgePos();
    if (focusArcs && focusArcs.lines.visible) rebuildFocusWires();
    // fn boxes orbit owner spheres — park the layer while the spheres
    if (fnMesh) fnMesh.visible = false;
    if (fnStalks) fnStalks.visible = false;
    if (fnLines) fnLines.visible = false;
    if (u >= 1) { compactAnim = null; applyVisibility(); }
  }
  // node alpha eases toward its target so filter/focus changes fade in
  // (visibility decisions read alphaTgt, so the fade is purely visual)
  for (let i = 0; i < N; i++) {
    const d = alphaTgt[i] - alphaArr[i];
    alphaArr[i] = Math.abs(d) < 0.003 ? alphaTgt[i] : alphaArr[i] + d * 0.15;
    const hsT = i === hovered ? 1.8 : 1;
    const dh = hsT - hoverScale[i];
    hoverScale[i] = Math.abs(dh) < 0.004 ? hsT : hoverScale[i] + dh * 0.18;
  }
  syncFileMesh();
  // idle spin pauses while the pointer is down over the canvas or a fn box
  // is hovered; the cbSpin checkbox turns it off entirely
  controls.autoRotate = !(pointerDown && overCanvas) && hoveredFn < 0 && spinEnabled;
  controls.update();
  updateHubs();
  updateClusterLabs();
  updateEdgeLabels();
  updateXtLabels();
  updateFocusLabels();
  // hub ring: marks the focused node so the budgeted wire fan reads as
  // radiating from ONE subject; hidden in overview. Search focus fills
  // query, not focusSeeds — fall back to the strongest level-0 node.
  let ringHi = -1;
  if (focusActive) {
    if (focusSeeds.size) ringHi = focusSeeds.values().next().value;
    else
      for (let i = 0; i < N; i++)
        if (level[i] === 0 && !supMem[i] &&
            (ringHi < 0 || degree[i] > degree[ringHi])) ringHi = i;
  }
  if (ringHi >= 0) {
    if (!hubRing) {
      hubRing = new THREE.Mesh(
        new THREE.RingGeometry(1, 1.12, 48),
        new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true,
          opacity: 0.28, side: THREE.DoubleSide, depthWrite: false,
          blending: THREE.AdditiveBlending }));
      hubRing.renderOrder = 2;
      scene.add(hubRing);
    }
    hubRing.scale.setScalar(sphR(ringHi) + 4);
    hubRing.position.set(pos[ringHi*3], pos[ringHi*3+1], pos[ringHi*3+2]);
    hubRing.visible = true;
  } else if (hubRing) hubRing.visible = false;
  // hub ring billboards toward the camera every frame
  if (hubRing && hubRing.visible) hubRing.quaternion.copy(camera.quaternion);
  // DYNAMIC NEAR PLANE (user report: junction legs/trunks pop out mid-view
  // when zooming in — the static near=1 swallowed corridor geometry passing
  // close to the camera). Tie near to the orbit distance with hysteresis so
  // the projection matrix isn't rebuilt every frame; far stays 20000 (depth
  // precision is fine: near tracks dist*0.01, ratio bounded per pose).
  const camD = camera.position.distanceTo(controls.target);
  const wantNear = Math.max(0.01, camD * 0.01);
  if (Math.abs(camera.near - wantNear) > wantNear * 0.25) {
    camera.near = wantNear; camera.updateProjectionMatrix();
  }
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

// frame camera on the graph centroid + bounding radius
function graphBounds() {
  const c = new THREE.Vector3(), lo = new THREE.Vector3(1e9,1e9,1e9), hi = new THREE.Vector3(-1e9,-1e9,-1e9);
  for (let i = 0; i < N; i++) {
    const p = new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]);
    c.add(p); lo.min(p); hi.max(p);
  }
  c.divideScalar(N || 1);
  return { center: c, radius: lo.distanceTo(hi) * 0.5 };
}
function frameGraph() {
  const b = graphBounds();
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y,
    camera.position.z - controls.target.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76).normalize(); // elevated 3/4 view
  dir.normalize();
  camera.position.copy(b.center).addScaledVector(dir, Math.max(420, b.radius * 1.55));
  controls.target.copy(b.center);
  // perceptual recenter: the elevated 3/4 view + left info panel bias the
  // mass low-left on screen; aim slightly below the bbox center so the
  // galaxy lands mid-canvas
  const lift = b.radius * 0.06;
  camera.position.y -= lift;
  controls.target.y -= lift;
  // LOD threshold tracks the framing distance so overview stays overview
  // regardless of graph size
  lodDist = Math.max(420, b.radius * 1.1);
  // fog scales with the layout: an absolute density tuned for one graph
  // size fogs out every far node once the relax inflates the layout (the
  // "spheres render dim" regression). Visibility ~ 6 framing distances.
  scene.fog.density = 0.17 / Math.max(420, b.radius * 1.55);
}

// frame only the nodes the filters still show (cluster/dir isolates) — the
// selected island(s) deserve the camera, not a whole-galaxy view with a
// bright corner. Tweened: this is a navigation act, not a boot-time snap.
function frameVisible() {
  const p = new THREE.Vector3(1e9, 1e9, 1e9), q = new THREE.Vector3(-1e9, -1e9, -1e9);
  let n = 0;
  for (let i = 0; i < N; i++) {
    if (alphaTgt[i] < 0.5) continue;   // targets, not the eased display values
    p.set(Math.min(p.x, pos[i*3]), Math.min(p.y, pos[i*3+1]), Math.min(p.z, pos[i*3+2]));
    q.set(Math.max(q.x, pos[i*3]), Math.max(q.y, pos[i*3+1]), Math.max(q.z, pos[i*3+2]));
    n++;
  }
  if (!n) return;
  const c = p.clone().add(q).multiplyScalar(0.5);
  const r = Math.max(120, p.distanceTo(q) * 0.5);
  const dir = new THREE.Vector3().subVectors(camera.position, controls.target);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  tweenCamTo(c, c.clone().addScaledVector(dir, Math.max(320, r * 1.8)));
}

// ---- UI ---------------------------------------------------------------------
const stats = document.getElementById("stats");
const m = DATA.meta;
// dead counts are function-level candidates; the map flags a file only when
// >= 40% of its funcs are candidates — surface both so "28+62" vs 9 lit
// files in dead-only mode doesn't read as missing data
const flagged = nodes.reduce((a, n) => a + (n.dead > 0 ? 1 : 0), 0);
stats.innerHTML = `${m.files} files · ${m.edges} links · ${m.clusters} clusters · <span title="${m.deadLikely} likely + ${m.deadReview} review-tier dead-FUNCTION candidates across the repo; a file is flagged (and shown in dead-only mode) when at least 40% of its funcs are candidates — currently ${flagged} files">dead ${m.deadLikely}+${m.deadReview} → ${flagged} files</span> · drag orbit · wheel zoom` +
  (m.strata ? " · height = call depth from entry" : "") +
  (m.generated_at ? `<br>gen ${m.generated_at}${m.git ? " · " + m.git : ""}` : "");
const edgeLegend = document.getElementById("edgeLegend");
// the legend is honest about what is on screen: the overview renders edges
// as weight-tinted gray, so the boot legend says so; typed colors return
// with a focus, one key per type still toggled on (var never shows — off)
function updateEdgeLegend(focusing) {
  edgeLegend.innerHTML = "";
  const addKey = (name, c) => {
    const k = document.createElement("span");
    k.className = "eKey";
    const sw = document.createElement("i");
    sw.style.borderTopColor = "#" + c.getHexString();
    k.appendChild(sw);
    k.appendChild(document.createTextNode(name));
    edgeLegend.appendChild(k);
  };
  if (!focusing) {
    addKey("edges", new THREE.Color(0.55, 0.60, 0.66));
    const hint = document.createElement("span");
    hint.className = "eHint";
    hint.textContent = "colors appear when you focus a node";
    edgeLegend.appendChild(hint);
  } else {
    if (showCalls) addKey("call", TYPE_COLORS.call);
    if (showSignals) addKey("signal", TYPE_COLORS.signal);
    if (showInst) addKey("contains", TYPE_COLORS.inst);
    if (showVar) addKey("var", TYPE_COLORS.var);
  }
}
// boot updateEdgeLegend(false) deleted - boot applyVisibility() re-runs it
// before the first render

// ---- containment: per-cluster halo ring + name at centroid (overview) -----
// names come from DATA.meta.clusterNames (extractor's cluster labeler fills
// the slot later); until then labels fall back to the cN chip id
const cNames = (m.clusterNames || {});
const clabsEl = document.getElementById("clabs");
let cLabs = [], cRings = [];
// rebuilt when the tests/dir filters change: hidden files leave their
// cluster, so centroids + halo extents must be recomputed (positions frozen)
function buildContainment() {
  cRings.forEach(r => { scene.remove(r); r.geometry.dispose(); r.material.dispose(); });
  cRings = []; cLabs.forEach(c => c.el.remove()); cLabs = [];
  const byC = {};
  // groups mode draws the halo ring per SUPERGROUP (matching node colors)
  const keyOf = n => groupsMode ? n.gid : n.cluster;
  nodes.forEach((n, i) => {
    const k = keyOf(n);
    if (k >= 0 && nodeVisible(n)) (byC[k] = byC[k] || []).push(i);
  });
  Object.entries(byC).sort((a, b) => b[1].length - a[1].length)
    .forEach(([cid, members]) => {
      let cx = 0, cy = 0, cz = 0;
      members.forEach(i => { cx += pos[i*3]; cy += pos[i*3+1]; cz += pos[i*3+2]; });
      cx /= members.length; cy /= members.length; cz /= members.length;
      let r = 60;
      members.forEach(i => {
        r = Math.max(r, Math.hypot(pos[i*3]-cx, pos[i*3+1]-cy, pos[i*3+2]-cz));
      });
      r *= 1.12;
      const col = new THREE.Color().setHSL(hue(+cid), 0.72, lightOf(+cid));
      // strata layout: halo rings assume planar cluster blobs — depth-
      // stretched clusters would ring mid-air. Keep the name labels only.
      if (!m.strata) {
        const segs = 72, pts = [];
        for (let s = 0; s <= segs; s++) {
          const a = s / segs * Math.PI * 2;
          pts.push(new THREE.Vector3(cx + Math.cos(a)*r, cy, cz + Math.sin(a)*r));
        }
        const ring = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
          new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.14,
            blending: THREE.AdditiveBlending, depthWrite: false }));
        scene.add(ring); cRings.push(ring);
      }
      const el = document.createElement("div");
      el.className = "clab";
      el.style.color = "#" + col.getHexString();
      const nm = groupsMode ? (gNames[+cid] || "g" + cid) : (cNames[cid] || "c" + cid);
      // collapsed clusters announce the merged mass on the label
      const sup = collapsed ? supCollapsed.get(+cid) : null;
      el.textContent = sup ? nm + " · " + sup.n + " files" : nm + " · " + members.length;
      clabsEl.appendChild(el);
      cLabs.push({ cx, cy, cz, el });
    });
}
// same hysteresis as hub labels: a cluster name keeps its placement (radial
// slot or row) while it stays collision-free at the freshly projected
// centroid; the candidate search reruns only on collision or after a hidden
// frame. Radial-outward wins first: 40px from the centroid along the
// screen-space direction away from the galaxy center of mass.
const clabOff = new Map();
function updateClusterLabs() {
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  // hub pills win collisions; cluster names try placements around the centroid
  const hubRects = [...document.querySelectorAll("#hubs .hub")]
    .filter(el => el.style.display !== "none")
    .map(el => el.getBoundingClientRect());
  const sep = (a, b) =>
    a.right < b.left - 4 || b.right < a.left - 4 ||
    a.bottom < b.top - 4 || b.bottom < a.top - 4;
  // project every centroid once; the mean of the on-screen projections is
  // the galaxy center of mass the radial candidates point away from
  const proj = [];
  let gx = 0, gy = 0, gn = 0;
  for (const c of cLabs) {
    if (clabsEl.style.display === "none") { proj.push(null); continue; }
    hubV.set(c.cx, c.cy, c.cz).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.05 || Math.abs(hubV.y) > 1.05) {
      proj.push(null); continue;
    }
    const px = (hubV.x*0.5+0.5)*w, py = (-hubV.y*0.5+0.5)*h;
    proj.push({ px, py });
    gx += px; gy += py; gn++;
  }
  gx /= (gn || 1); gy /= (gn || 1);
  const taken = [];
  const clabObst = juncArrowObstacles(w, h);   // skeptic r4 #3
  cLabs.forEach((c, k) => {
    const pj = proj[k];
    if (!pj) { c.el.style.display = "none"; clabOff.delete(c.el.textContent); return; }
    c.el.style.display = "block";
    const px = pj.px, py = pj.py;
    const key = c.el.textContent;
    const dx = px - gx, dy2 = py - gy;
    const dl = Math.hypot(dx, dy2) || 1;
    const rx = px + dx / dl * 40, ry = py + dy2 / dl * 40;
    const tryAt = (x, y) => {
      c.el.style.transform = "translate(" + x.toFixed(1) + "px," + y.toFixed(1) + "px) translate(-50%,-50%)";
      const r = c.el.getBoundingClientRect();
      return (hubRects.every(hr => sep(r, hr)) && taken.every(t => sep(r, t)) &&
              clabObst.every(q => q[0] < r.left - 6 || q[0] > r.right + 6 ||
                                  q[1] < r.top - 6 || q[1] > r.bottom + 6)) ? r : null;
    };
    const prev = clabOff.get(key);
    if (prev !== undefined) {
      const r = prev.rad ? tryAt(rx, ry) : tryAt(px, py + prev.dy);
      if (r) { taken.push(r); return; }
    }
    const rRad = tryAt(rx, ry);
    if (rRad) { clabOff.set(key, { rad: true }); taken.push(rRad); return; }
    for (const dy of [0, -34, 34, -64, 64]) {
      const r = tryAt(px, py + dy);
      if (r) { clabOff.set(key, { rad: false, dy }); taken.push(r); return; }
    }
    c.el.style.display = "none"; clabOff.delete(key);
  });
}

const tip = document.getElementById("tip");
const crumb = document.getElementById("crumb");
const esc = s => String(s).replace(/[&<>"]/g,
  ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" })[ch]);
  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
const _pickV = new THREE.Vector3();   // scratch for screen-space pick accuracy
  let hovered = -1, hoveredFn = -1;
  // hover greyout (Cosmograph pattern): hovering a node greys everything
  // outside its 1-hop neighborhood to 0.12 alpha — zero-click orientation.
  // hoverGreyIdx = the node currently greyed (-1 = clean overview);
  // focusActive mirrors applyVisibility's focusing so focus mode owns the
  // scene and hover must not fight its BFS dimming.
  let hoverGreyIdx = -1, focusActive = false;
let hubRing = null;   // additive halo marking the focused hub (budget fan origin)
let deadOnly = false, query = "";
let mutOnly = false;   // fn layer: show only functions that write state
const activeClusters = new Set();   // multi-select cluster filter (legend chips)
let showInst = false, showCalls = true, showSignals = true, showVar = false, showGhost = false, depth = 1;
// focus roots: single click replaces, shift-click stacks (BFS is multi-seed)
const focusSeeds = new Set();
// direction mode: 0 = both, 1 = out (downstream impact), 2 = in (upstream deps)
let dirMode = 0;
// tests/tools hidden by default (chip toggles them in); dir filter row works
// like the cluster chips — both only ever filter, never re-layout
// (declarations live above syncEdgePos: the boot-time geometry writer
// reads them through linkFiltered)
// overview LOD: intra-cluster edges stay hidden until the camera closes in
// (zoom threshold maintained by the controls 'change' listener below)
let lodClose = false, lodDist = 1e9;
const level = new Int16Array(N).fill(-1);
// hub edge budget: a focus used to light EVERY link in the lit subgraph —
// a 150-wire hub rendered as a radial starburst ('wire salad', unreadable).
// The budget keeps only the top HUB_EDGE_BUDGET links by weight (call
// count) fully lit; the rest drop to ghost ink: GHOST_K × the focus bucket
// opacity (0.75) ≈ alpha 0.06. Hovering a budgeted wire re-lights it
// (hoverEdgeLi) — detail on demand. The fn layer is UNCAPPED between lit
// files (amendment: the focus neighborhood is small + compacted, so every
// fn interconnection renders; HUB_FN_BUDGET is retired).
const HUB_EDGE_BUDGET = 12;
const GHOST_K = 0.08;

// BFS from search seeds (path/class/fn-name matches + clicked seeds) up to
// `depth`; the direction mode picks which adjacency half the walk follows
function computeLevels() {
  level.fill(-1);
  const seeds = [];
  if (query) nodes.forEach((n, i) => {
    if (n.path.toLowerCase().includes(query) || n.cls.toLowerCase().includes(query)) {
      if (level[i] < 0) { level[i] = 0; seeds.push(i); }
    }
  });
  // fn-name seeds: a function search lights the files that own or call it
  if (query && query.length >= 2) fnNames.forEach(nm => {
    if (nm.toLowerCase().includes(query)) {
      for (const f of fnOf[nm]) if (level[f] < 0) { level[f] = 0; seeds.push(f); }
    }
  });
  for (const s of focusSeeds) if (level[s] < 0) { level[s] = 0; seeds.push(s); }
  for (let qi = 0; qi < seeds.length; qi++) {
    const u = seeds[qi];
    if (level[u] >= depth) continue;
    const nbrs = dirMode === 1 ? adjOut[u] : dirMode === 2 ? adjIn[u] : adj[u];
    for (const v of nbrs) if (level[v] < 0) { level[v] = level[u] + 1; seeds.push(v); }
  }
  return seeds.length > 0;
}

function nodeVisible(n) {
  if (deadOnly && n.dead <= 0) return false;
  if (cycOnly && !n.cyc) return false;
  if (activeClusters.size && !activeClusters.has(n.cluster)) return false;
  if (!showTests && isTestNode(n)) return false;
  if (activeDirs.size && !activeDirs.has(n.dir)) return false;
  return true;
}
function typeVisible(ty) {
  if (ty === "call") return showCalls;
  if (ty === "signal") return showSignals;
  if (ty === "inst" || ty === "attach") return showInst;
  if (ty === "var") return showVar;   // member-var refs: dense, opt-in via the var toggle
  return true;
}
// focus-neighborhood compaction targets (see applyVisibility's hook): the
// lit set lerps 0.6 toward its centroid, then the Python layout's
// non-overlap guarantees are re-established deterministically
function compactLitSet() {
  const lit = [];
  for (let i = 0; i < N; i++)
    if (level[i] >= 0 && level[i] <= 1 && nodeVisible(nodes[i]) && !supMem[i]) lit.push(i);
  compactIdx = lit;
  compactScale = 1; compactOverlaps = 0;
  if (!lit.length) { compactTgt = null; return; }
  // Always compute from the pristine saved base (not live pos): the
  // anim-end applyVisibility re-run and mid-focus filter changes must
  // land on the SAME settled layout, not compound the centroid lerp.
  const work = new Float32Array(N * 3);
  work.set(posSaved || pos);
  const sp = Math.sqrt(spread);
  const rad = i => sphR(i);
  const CLR = 8;   // wire clearance (fn arcs ride at +14)
  const minD = (a, b) => rad(a) + rad(b) + CLR;
  // COMPACT BALL: center = focus seed; neighbors on a golden-spiral shell
  // sized so adjacent points start above min spacing - the depenetration +
  // exact-scale passes below guarantee the final no-overlap state.
  const fi = lit.find(i => level[i] === 0) ?? lit[0];
  focusFileIdx = fi;
  const cx = work[fi*3], cy = work[fi*3+1], cz = work[fi*3+2];
  let maxRad = 0;
  for (const i of lit) maxRad = Math.max(maxRad, rad(i));
  const others = lit.filter(i => i !== fi);
  const SP = 2 * maxRad + CLR + 6;
  const R = Math.max(rad(fi) + maxRad + CLR + SP * 0.5,
                     Math.sqrt(others.length || 1) * SP * 0.5);
  const GA = Math.PI * (3 - Math.sqrt(5));
  compactBallR = R;
  others.forEach((i, k) => {
    const y = 1 - (2 * (k + 0.5)) / others.length;
    const rr = Math.sqrt(Math.max(0, 1 - y * y));
    const th = GA * k;
    work[i*3]   = cx + Math.cos(th) * rr * R;
    work[i*3+1] = cy + y * R * 0.55;
    work[i*3+2] = cz + Math.sin(th) * rr * R;
  });
  work[fi*3] = cx; work[fi*3+1] = cy; work[fi*3+2] = cz;
  // depenetrate fused pairs on the deterministic hash axis (_layout parity)
  for (let sweep = 0; sweep < 8; sweep++) {
    let fused = 0;
    for (let x = 0; x < lit.length; x++) for (let y = x + 1; y < lit.length; y++) {
      const a = lit[x], b = lit[y];
      const dx = work[a*3] - work[b*3], dy = work[a*3+1] - work[b*3+1],
            dz = work[a*3+2] - work[b*3+2];
      if (Math.sqrt(dx*dx + dy*dy + dz*dz) >= minD(a, b) * 0.4) continue;
      const h = (a * 2654435761 + b * 40503) % 9973;
      const ang = h / 9973.0 * 6.2831853;
      let ax = Math.cos(ang), ay = 0.35 * Math.sin(ang * 1.7), az = Math.sin(ang);
      const al = Math.sqrt(ax*ax + ay*ay + az*az) || 1;
      ax /= al; ay /= al; az /= al;
      const sep = minD(a, b) * 1.2;
      const mx = (work[a*3] + work[b*3]) / 2, my = (work[a*3+1] + work[b*3+1]) / 2,
            mz = (work[a*3+2] + work[b*3+2]) / 2;
      work[a*3] = mx - ax * sep * 0.5; work[a*3+1] = my - ay * sep * 0.5;
      work[a*3+2] = mz - az * sep * 0.5;
      work[b*3] = mx + ax * sep * 0.5; work[b*3+1] = my + ay * sep * 0.5;
      work[b*3+2] = mz + az * sep * 0.5;
      fused++;
    }
    if (!fused) break;
  }
  // exact-scale finisher about the centroid: scaling is linear in the
  // offsets, so one multiply clears every lit pair. Alternating with the
  // wire clamp: a clamp push can shrink a pair below minD and a scale can
  // re-pierce a wire, so the two passes converge together (bounded rounds).
  let s = 1;   // cumulative exact-scale factor (reported via __dbg)
  const scalePass = () => {
    let s2 = 1;
    for (let x = 0; x < lit.length; x++) for (let y = x + 1; y < lit.length; y++) {
      const a = lit[x], b = lit[y];
      const dx = work[a*3] - work[b*3], dy = work[a*3+1] - work[b*3+1],
            dz = work[a*3+2] - work[b*3+2];
      const d = Math.sqrt(dx*dx + dy*dy + dz*dz);
      if (d > 1e-3) s2 = Math.max(s2, minD(a, b) / d);
    }
    if (s2 > 1) {
      for (const i of lit) {
        work[i*3]   = cx + (work[i*3]   - cx) * s2 * 1.01;
        work[i*3+1] = cy + (work[i*3+1] - cy) * s2 * 1.01;
        work[i*3+2] = cz + (work[i*3+2] - cz) * s2 * 1.01;
      }
      s *= s2 * 1.01;
    }
  };
  scalePass();
  // node-vs-wire clamp: push a lit sphere off any lit wire it pierces
  const seg = [];
  links.forEach(l => {
    if (level[l.s] < 0 || level[l.s] > 1 || level[l.t] < 0 || level[l.t] > 1) return;
    if (supMem[l.s] || supMem[l.t]) return;
    if (!typeVisible(l.ty) || nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t])) return;
    seg.push([l.s, l.t]);
  });
  const segD = (px, py, pz, a, b) => {
    const abx = work[b*3] - work[a*3], aby = work[b*3+1] - work[a*3+1],
          abz = work[b*3+2] - work[a*3+2];
    const apx = px - work[a*3], apy = py - work[a*3+1], apz = pz - work[a*3+2];
    const L = abx*abx + aby*aby + abz*abz;
    const t = L > 1e-6 ? Math.min(1, Math.max(0, (apx*abx + apy*aby + apz*abz) / L)) : 0;
    const qx = work[a*3] + abx*t, qy = work[a*3+1] + aby*t, qz = work[a*3+2] + abz*t;
    const dx = px - qx, dy = py - qy, dz = pz - qz;
    return { d: Math.sqrt(dx*dx + dy*dy + dz*dz), qx, qy, qz,
             ex: dx, ey: dy, ez: dz };
  };
  const clampPass = () => {
    let pushed = 0;
    for (const i of lit) for (const [a, b] of seg) {
      if (i === a || i === b) continue;
      const hit = segD(work[i*3], work[i*3+1], work[i*3+2], a, b);
      const need = (rad(i) + CLR) * 1.05;
      if (hit.d >= need) continue;
      // push along the TRUE perpendicular (node minus closest point) — a
      // midpoint-direction push is near-parallel to the wire for grazing
      // nodes and slides them along it without clearing
      let ex = hit.ex, ey = hit.ey, ez = hit.ez;
      const el = Math.sqrt(ex*ex + ey*ey + ez*ez);
      if (el < 1e-3) {
        const h = (i * 2654435761 + a * 40503) % 9973;
        const ang = h / 9973.0 * 6.2831853;
        ex = Math.cos(ang); ey = 0.35 * Math.sin(ang * 1.7); ez = Math.sin(ang);
      } else { ex /= el; ey /= el; ez /= el; }
      const push = need - hit.d;
      work[i*3] += ex * push; work[i*3+1] += ey * push; work[i*3+2] += ez * push;
      pushed++;
    }
    return pushed;
  };
  for (let round = 0; round < 8; round++) {
    scalePass();
    if (!clampPass()) break;
  }
  // residual violations (target 0): pairwise + sphere-vs-wire
  let bad = 0;
  for (let x = 0; x < lit.length; x++) for (let y = x + 1; y < lit.length; y++) {
    const a = lit[x], b = lit[y];
    const dx = work[a*3] - work[b*3], dy = work[a*3+1] - work[b*3+1],
          dz = work[a*3+2] - work[b*3+2];
    if (Math.sqrt(dx*dx + dy*dy + dz*dz) < minD(a, b)) bad++;
  }
  for (const i of lit) for (const [a, b] of seg) {
    if (i === a || i === b) continue;
    if (segD(work[i*3], work[i*3+1], work[i*3+2], a, b).d < rad(i) + CLR) bad++;
  }
  compactOverlaps = bad;
  compactScale = +s.toFixed(4);   // honest cumulative factor (margin already inside s)
  compactTgt = work;
}
// focus wire arcs: budget wires render as gentle bezier arcs (lift = 14% of
// span on +Y) in the map pane's wire-type palette. Straight chords crossing
// at one gray value were the confusion driver (Purchase/Huang: crossings
// dominate legibility); arcs separate crossing wires in height and the type
// color carries identity — same law as the 2D pane. One LineSegments,
// rebuilt from budgetLit (budget-capped 12 + hover reveals), so rebuilds
// stay cheap even per-frame during the compaction ease.
const TYPE_C3D = { call: 0xd9e2eb, signal: 0xffb347, var: 0x73e68c,
  attach: 0x3dccf2, inst: 0x3dccf2 };
let focusArcs = null;   // { lines, geo, mat }
const ARC_SEG = 14;
function rebuildFocusWires() {
  const list = [];
  // zero-wire .tscn affordance (C2.2 ruling): a deg-57+ hub whose whole
  // neighborhood sits in the ghost tier renders NOTHING about its
  // connectedness. Reveal the focused .tscn hub's top-8 budget links as
  // dim-but-traceable arcs — countable ink, not a full wire re-add.
  const revealed = new Set();
  const fi = focusFileIdx;
  const tscn = fi >= 0 && /\.tscn$/i.test(nodes[fi].path || "");
  if (tscn) for (let i = 0; i < links.length; i++) {
    const l = links[i];
    if (l.s !== fi && l.t !== fi) continue;
    if (budgetLit && budgetLit.size && budgetLit.has(i)) continue;  // budget ink
    revealed.add(i);
  }
  if (focusActive && budgetLit) budgetLit.forEach(i => {
    const l = links[i];
    if (alphaTgt[l.s] <= 0.05 && alphaTgt[l.t] <= 0.05) return;   // ghost pair
    list.push(i);
  });
  if (revealed.size) {
    const top = [...revealed].sort((a, b) => links[b].w - links[a].w || a - b).slice(0, 8);
    top.forEach(i => { revealed.delete(i); list.push(i); });
    top.forEach(i => revealed.add(i));
    list.sort((a, b) => a - b);   // deterministic vertex order
  } else if (!list.length) {
    if (focusArcs) {
      scene.remove(focusArcs.lines); focusArcs.lines.geometry.dispose();
      focusArcs = null;
    }
    return;
  }
  if (focusArcs) {
    scene.remove(focusArcs.lines); focusArcs.lines.geometry.dispose();
    focusArcs = null;
  }
  const n = list.length;
  const vCount = n * ARC_SEG * 2;
  const P = new Float32Array(vCount * 3);
  const C = new Float32Array(vCount * 3);
  const D = new Float32Array(vCount);
  const col = new THREE.Color();
  list.forEach((li, k) => {
    const l = links[li];
    const ax = pos[l.s*3], ay = pos[l.s*3+1], az = pos[l.s*3+2];
    const bx = pos[l.t*3], by = pos[l.t*3+1], bz = pos[l.t*3+2];
    const dist = Math.hypot(bx-ax, by-ay, bz-az) || 1;
    // control point: midpoint lifted 14% of the span — every arc rises, so
    // two crossing wires separate in height instead of sharing a pixel
    const mx = (ax + bx) / 2, my = (ay + by) / 2 + dist * 0.14, mz = (az + bz) / 2;
    const vis = Math.min(alphaTgt[l.s], alphaTgt[l.t]);
    col.setHex(TYPE_C3D[l.ty] || 0xd9e2eb);
    if (revealed.has(li)) col.multiplyScalar(0.42);   // C2.2 dim-but-traceable
    else if (vis < 0.5) col.multiplyScalar(0.12);   // dead-end / ghost endpoint
    const phase = ((li * 2654435761) % 997) / 997 * 13;   // per-link dash phase
    let px = 0, py = 0, pz = 0, pd = 0;
    for (let s = 0; s <= ARC_SEG; s++) {
      const t = s / ARC_SEG, u = 1 - t;
      const x = u*u*ax + 2*u*t*mx + t*t*bx;
      const y = u*u*ay + 2*u*t*my + t*t*by;
      const z = u*u*az + 2*u*t*mz + t*t*bz;
      const dd = s ? pd + Math.hypot(x-px, y-py, z-pz) : 0;
      if (s) {
        const vi = (k * ARC_SEG + s - 1) * 2;
        const p3 = vi * 3;
        P[p3] = px; P[p3+1] = py; P[p3+2] = pz;
        P[p3+3] = x; P[p3+4] = y; P[p3+5] = z;
        C[p3] = col.r; C[p3+1] = col.g; C[p3+2] = col.b;
        C[p3+3] = col.r; C[p3+4] = col.g; C[p3+5] = col.b;
        D[vi] = pd + phase; D[vi+1] = dd + phase;
      }
      px = x; py = y; pz = z; pd = dd;
    }
  });
  // fat-line rebuild per call: the budget is tiny (≤ hub budget arcs) and
  // Line2 gives the hub fan real 2px ink like every other wire
  const fgeo = new LineSegmentsGeometry();
  fgeo.setPositions(P);
  fgeo.setColors(C);
  const fmat = new LineMaterial({ vertexColors: true,
    transparent: true, opacity: 0.95, linewidth: 2, worldUnits: false,
    dashed: true, dashSize: 8, gapSize: 5, depthWrite: false,
    blending: THREE.NormalBlending, alphaToCoverage: false });
  fmat.resolution.set(glW(), innerHeight);
  const flines = new LineSegments2(fgeo, fmat);
  flines.computeLineDistances();
  flines.frustumCulled = false; flines.renderOrder = 3;
  scene.add(flines);
  focusArcs = { lines: flines, geo: fgeo, mat: fmat };
  // the arcs REPLACE the budget links' straight bucket chords (k-pass blacks
  // those) — hover/click must test THESE chords, or the collider stays on
  // the invisible pre-curve straight line
  flines.userData.meta = list.map(li => ({ kind: "link", li }));
  flines.userData.seg = ARC_SEG;
  focusArcs.lines.visible = true;
}
function applyVisibility() {
  const focusing = computeLevels();
  focusActive = focusing;   // hover greyout defers to focus mode
  edgeFlowOn = focusing;   // tick's dash-flow pass reads this
  // edges are a quiet layer at overview (per-bucket caps) and open up when
  // a focus set is lit
  bucketMat.forEach((mat, bi) => { mat.opacity = focusing ? 0.75 : BUCKETS[bi].op; });
  updateEdgeLegend(focusing);
  // fn layer only makes sense inside a focus — say so instead of ignoring clicks
  cbFnEl.disabled = !focusing;
  cbFnEl.parentElement.title = focusing ? "" : "function layer needs a focus (search or click a node)";
  for (let i = 0; i < N; i++) {
    let a;
    if (!nodeVisible(nodes[i])) a = 0.0;   // size-0 gate = true disable
    else if (focusing) a = level[i] < 0 ? 0.0 : (level[i] <= depth ? 1 : 0.04);   // lit set = focus..`depth` hops (slider-owned radius); strata beyond ghost near-zero (0.04 < the 0.05 ghost kill, so their deep-deep wires collapse outright)
    else a = 1;
    alphaTgt[i] = a;
    if (a > 0.5) {
      const c = colorOf(nodes[i]);
      colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
    }
  }
  // focus-neighborhood compaction: pull the lit set toward its centroid,
  // then mirror the Python layout's guarantees — deterministic hash-axis
  // depenetration for fused pairs, an exact-scale finisher about the
  // centroid (linear in the offsets: one multiply clears every pair), and
  // a node-vs-wire clamp so no lit wire pierces a lit sphere. pos is
  // mutated in place (picks, labels, fn arcs and the hub ring all read
  // pos); posSaved restores the frozen layout on unfocus.
  if (focusing) {
    if (!posSaved) {
      posSaved = pos.slice();
      compactLitSet();
      if (compactTgt) compactAnim = { t0: performance.now(), dur: 450 };
    } else if (!compactAnim) {
      // filters changed mid-focus: recompute deterministically from the
      // saved base (the running ease keeps ownership until it lands)
      compactLitSet();
      if (compactTgt) for (const i of compactIdx) {
        pos[i*3] = compactTgt[i*3]; pos[i*3+1] = compactTgt[i*3+1]; pos[i*3+2] = compactTgt[i*3+2];
      }
    }
  } else if (posSaved) {
    pos.set(posSaved); posSaved = null; compactTgt = null;
    compactIdx = []; compactAnim = null; compactScale = 1; compactOverlaps = 0;
    compactBallR = 0; focusFileIdx = -1;
  }
  // collapse pass: re-derives dpos + the supernode set from the targets
  // just computed; zeroes member alphaTgt (existing hide path fades the
  // spheres and drops hub labels)
  refreshCollapse();
  syncFileMesh();
  // dim edges: hidden endpoints, filtered types, or focus distance
  // (dimmed eColBase written straight into each bucket's instanced colors).
  // Overview palette: edge-type hues are demoted to weight-tinted gray so
  // cluster colors carry the overview; full type colors return on focus.
    const grayMix = focusing ? 0 : 0.6;
  // hub edge budget: rank the focus-lit links by weight desc; only the top
  // HUB_EDGE_BUDGET render lit, the rest ghost down. Candidates mirror the
  // lit branch below exactly — edges that would render k=0 (fn wire mode),
  // k=0.012 (dead-end dim) or k=0 (ghost/filtered) must not consume budget
  // slots. A hovered wire (hoverEdgeLi) is force-admitted: hover = reveal.
  if (focusing) {
    const deadEnd = j => alphaTgt[j] <= 0.5 && !supMem[j];
    const cand = [];
    links.forEach((l, i) => {
      if (!typeVisible(l.ty)) return;
      if (nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t])) return;
      if (alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05) return;
      if (deadEnd(l.s) || deadEnd(l.t)) return;
      if (fnMode && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) return;
      cand.push([i, l.w]);
    });
    cand.sort((a, b) => b[1] - a[1] || a[0] - b[0]);
    budgetLit = new Set(cand.slice(0, HUB_EDGE_BUDGET).map(c => c[0]));
    if (hoverEdgeLi >= 0) budgetLit.add(hoverEdgeLi);
    // endpoint-hover reveal: hovering a node admits every surviving link
    // incident to it (the ghost layer hides by default; hover = show the
    // fan). Filters mirror the candidate loop INCLUDING the dead-end bar:
    // under hover greyout the far end can sit at alphaTgt 0.12, and a lit
    // arc into a near-invisible speck is the "signal wire with no visible
    // terminus" class (user sighting #6, upper-left) — focus-lit plain
    // links render only onto anchors that read (alpha above the lit
    // threshold; the arc endpoint's sprite gets the ANCHOR_PX floor).
    const fanLit = j => alphaTgt[j] > 0.5 || supMem[j];
    if (hovered >= 0) links.forEach((l, i) => {
      if (l.s !== hovered && l.t !== hovered) return;
      if (!typeVisible(l.ty) || nodeFiltered(nodes[l.s]) || nodeFiltered(nodes[l.t])) return;
      if (!fanLit(l.s) || !fanLit(l.t)) return;
      if (fnMode && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) return;
      budgetLit.add(i);
    });
  } else {
    budgetLit = null;
    hoverEdgeLi = -1;
  }
  const touched = [false, false, false];
  links.forEach((l, i) => {
    let k;
    // dir-filtered endpoints (tests/tools hidden, active dir isolation):
    // edges are killed outright, not dimmed — additive blending makes even
    // 1% gray visible when dozens of test edges converge on a hub
    const sFiltered = nodeFiltered(nodes[l.s]);
    const tFiltered = nodeFiltered(nodes[l.t]);
    // ghost: both endpoints outside the focus — kill outright (the arc
    // collapse in the k===0 branch below removes its baked geometry too)
    const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
    if (sFiltered || tFiltered || ghost) k = 0.0;
    // collapsed members carry alphaTgt 0 but their edges LIVE (re-targeted
    // to the supernode) — only non-member dim endpoints take the 0.012 dim
    else if (!typeVisible(l.ty) ||
      ((alphaTgt[l.s] <= 0.5 && !supMem[l.s]) || (alphaTgt[l.t] <= 0.5 && !supMem[l.t]))) k = 0.012;
    else if (fnMode && focusing && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) k = 0; // wire mode: fn wires replace the aggregate call line; geometry collapse (k===0 branch) handles invisibility — a ghost 0.04 double-draws under the additive fn wires
    else if (focusing) k = budgetLit && budgetLit.has(i)
      // budget wires render as curved type-colored arcs (rebuildFocusWires)
      // — the straight bucket segment goes black underneath them
      ? 0
      // quiet layer: non-budget wires hide by default (a ball full of dim
      // gray diagonals reads as noise); the ghosts toggle or a hover on an
      // endpoint brings them back
      : (showGhost ? GHOST_K : 0);
    else {
      // overview edge budget: single-ref wires are noise at full extent
      // (1530 lines summing to white over the core under additive blending)
      // - only multi-ref links earn a line until a focus opens the scene.
      // Corridors (hw arcs) are the intended carriers - they keep more ink
      // than same-band straight chords, which fade to near-nothing.
      k = l.w >= 2 ? 1 : 0.0;
      if (k > 0.0) k = hwSlot[i] >= 0 ? Math.min(1, k * 1.5) : k * 0.5;
    }
    if (!focusing && !lodClose && k > 0.04) {
      const dx = pos[l.s*3] - pos[l.t*3], dy = pos[l.s*3+1] - pos[l.t*3+1],
            dz = pos[l.s*3+2] - pos[l.t*3+2];
      const el3 = Math.sqrt(dx*dx + dy*dy + dz*dz);
      const sameC = nodes[l.s].cluster >= 0 && nodes[l.s].cluster === nodes[l.t].cluster;
      // overview edge-cut: intra-cluster edges hide entirely and long
      // ring-diameter chords dim out — otherwise they cross the whole
      // galaxy and re-form the hairball the layout just removed.
      // highway arcs are exempt from the chord cut: bundled beziers ARE
      // the intended inter-cluster carriers
      if (sameC) { if (k > 0.05) k = 0.05; }
      else if (el3 > 200 && hwSlot[i] < 0) k = 0.04;
    }
    edgeK[i] = k;   // ink truth for the picker — see edgeK decl
    const b = bucketOf[i], o6 = i * 6;
    const tgt = bucketColIB[b].array;
    if (k === 0) {
      // filtered-out edge (tests/tools hidden, dir filter, focus ghost):
      // colors go black here; GEOMETRY is syncEdgePos's job (it collapses
      // filtered/ghost arcs + straight edges — this pass used to write
      // arc positions too, racing the re-derivation and letting black
      // full-length wires paint over content)
      if (hwSlot[i] >= 0) {
        tgt.fill(0, hwSlot[i], hwSlot[i] + 96);
      } else {
        tgt.fill(0, slotOf[i] * 6, slotOf[i] * 6 + 6);
      }
      touched[b] = true;
      return;
    }
    if (hwSlot[i] >= 0) {
      // highway: replicate the (possibly grayed/dimmed) color across all
      // 16 segments. Geometry is NOT touched here — syncEdgePos() right
      // below re-derives arc positions in the CURRENT coordinate space
      // (this block used to restore baked arcs, detaching them from
      // spread-moved nodes)
      const base = hwSlot[i];
      if (grayMix > 0) {
        const g = (0.10 + 0.18 * Math.min(1, l.w / 8)) * k;
        for (let v = 0; v < 16; v++) {
          const s6 = base + v * 6;
          for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * (1 - grayMix) + g * grayMix;
        }
      } else {
        for (let v = 0; v < 16; v++) {
          const s6 = base + v * 6;
          for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * k;
        }
      }
      touched[b] = true;
      return;
    }
    const s6 = slotOf[i] * 6;
    if (grayMix > 0) {
      // dim weight-tinted gray: edges stay legible structure hints without
      // outshining the cluster-colored nodes under additive blending
      const g = (0.10 + 0.18 * Math.min(1, l.w / 8)) * k;
      for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * (1 - grayMix) + g * grayMix;
    } else {
      for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * k;
    }
    touched[b] = true;
  });
  touched.forEach((t, b) => { if (t) bucketColIB[b].needsUpdate = true; });
  syncEdgePos();   // geometry follows the new filter state (collapse/restore)
  hoverGreyIdx = -1;   // baseline rebuilt — next hover re-greys from here
  rebuildFocusWires();   // budget arcs track budgetLit + live pos
  if (focusing) {
    let lit = 0;
    for (let i = 0; i < N; i++) if (level[i] >= 0 && level[i] <= 1 && nodeVisible(nodes[i])) lit++;
    const first = focusSeeds.values().next().value;
    const label = !focusSeeds.size ? "“" + esc(query) + "”"
      : focusSeeds.size === 1 ? esc(nodes[first].label)
      : focusSeeds.size + " files";
    crumb.innerHTML = "focus: <b>" + label + "</b> · depth " + depth +
      (dirMode ? " · dir " + (dirMode === 1 ? "out" : "in") : "") + " · " + lit +
      " files lit" +
      (focusStack.length ? "<span class='back' title='back (Backspace)'>‹</span>" : "") +
      "<span class='x' title='clear focus (Esc)'>✕</span>" +
      "<span class='hint'>Esc/right-click clears · ⌫ back</span>";
    crumb.style.display = "flex";
    crumb.querySelector(".x").onclick = clearFocus;
    const back = crumb.querySelector(".back");
    if (back) back.onclick = popFocus;
  } else {
    // empty state: nothing passes the filters — say so and offer one-chip undo
    let vis = 0;
    for (let i = 0; i < N; i++) if (nodeVisible(nodes[i])) vis++;
    if (vis === 0) {
      const clears = [];
      if (deadOnly) clears.push(["dead only", () => {
        deadOnly = false; bDeadEl.classList.remove("on");
      }]);
      activeClusters.forEach(c => clears.push([cNames[c] || "c" + c, () => {
        activeClusters.delete(c);
        const ch = legendChips.get(c); if (ch) ch.classList.remove("on");
      }]));
      activeDirs.forEach(d => clears.push([d, () => {
        activeDirs.delete(d);
        const ch = dirChips.get(d); if (ch) ch.classList.remove("on");
      }]));
      crumb.innerHTML = "0 files visible — " + clears.map((c, k) =>
        "<span class='f' data-k='" + k + "'>" + esc(c[0]) + " ✕</span>").join("");
      crumb.style.display = "flex";
      crumb.querySelectorAll(".f").forEach((el, k) =>
        el.onclick = () => { clears[k][1](); buildContainment(); applyVisibility(); });
    } else crumb.style.display = "none";
  }
  // cluster identity is an overview cue — hide the name labels on focus
  clabsEl.style.display = focusing ? "none" : "block";
  rebuildFnLayer(focusing);
  rebuildHubs();
  rebuildEdgeLabels(focusing);
  rebuildFocusLabels(focusing);
  drawMapPane();   // mermaid pane mirrors the new focus state
}

// ---- edge labels: focus detail mode (small lit set) ---------------------------
// when the focus set is small, label each in-set link's midpoint with its
// type and weight; large sets skip labels entirely to avoid clutter
// threshold is relative to graph size so depth-1 neighborhoods stay labeled
// across data drift while depth 2-3 sets stay clean
const DETAIL_MAX = Math.max(120, nodes.length * 0.35);
const elabsEl = document.getElementById("elabs");
let eLabs = [], detailMode = false;
const TY_NAME = { call: "call", signal: "signal", inst: "contains",
  attach: "contains", var: "var" };
const tyCss = c => "rgb(" + Math.round(c.r * 255) + "," +
  Math.round(c.g * 255) + "," + Math.round(c.b * 255) + ")";
function rebuildEdgeLabels(focusing) {
  eLabs = [];
  elabsEl.innerHTML = "";
  detailMode = false;
  if (!focusing) return;
  let lit = 0;
  for (let i = 0; i < N; i++) if (level[i] >= 0 && nodeVisible(nodes[i])) lit++;
  if (lit === 0 || lit > DETAIL_MAX) return;
  detailMode = true;
  // strongest edges claim labels first (weight desc); collision-skip in
  // updateEdgeLabels hides the rest
  const cand = [];
  links.forEach((l, i) => {
    if (!typeVisible(l.ty) || level[l.s] < 0 || level[l.t] < 0) return;
    if (budgetLit && !budgetLit.has(i)) return;   // ghost wires carry no label
    cand.push({ i, w: l.w });
  });
  cand.sort((a, b) => b.w - a.w);
  cand.slice(0, 120).forEach(({ i }) => {
    const l = links[i];
    const el = document.createElement("div");
    el.className = "elab";
    el.textContent = TY_NAME[l.ty] + " ×" + l.w;
    el.style.color = tyCss(TYPE_COLORS[l.ty] || TYPE_COLORS.var);
    elabsEl.appendChild(el);
    eLabs.push({ i, el });
  });
}
function updateEdgeLabels() {
  if (!detailMode) return;
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  const placed = [];
  for (const { i, el } of eLabs) {
    const l = links[i];
    if (alphaTgt[l.s] < 0.5 || alphaTgt[l.t] < 0.5) { el.style.display = "none"; continue; }
    hubV.set((pos[l.s*3] + pos[l.t*3]) / 2, (pos[l.s*3+1] + pos[l.t*3+1]) / 2,
      (pos[l.s*3+2] + pos[l.t*3+2]) / 2).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.02 || Math.abs(hubV.y) > 1.02) {
      el.style.display = "none"; continue;
    }
    const x = (hubV.x * 0.5 + 0.5) * w, y = (-hubV.y * 0.5 + 0.5) * h;
    el.style.display = "block";
    // try a small vertical nudge before letting collision-skip hide it
    el.style.transform = "translate(" + x.toFixed(1) + "px," + y.toFixed(1) +
      "px) translate(-50%,-50%)";
    placed.push({ el, x, y, r: el.getBoundingClientRect() });
  }
  // collision-skip: hub labels win, then stronger (earlier) edge labels win;
  // losing labels try a small nudge before hiding
  const hubRects = [...document.querySelectorAll(".hub")]
    .filter(e => e.style.display === "block")
    .map(e => e.getBoundingClientRect());
  const free = (a, b) => a.right < b.left - 2 || b.right < a.left - 2 ||
    a.bottom < b.top - 2 || b.bottom < a.top - 2;
  const taken = [];
  for (const p of placed) {
    if (taken.length >= 24) { p.el.style.display = "none"; continue; }
    let r = p.r;
    if (hubRects.some(hr => !free(r, hr)) ||
        taken.some(t => !free(r, t))) {
      let ok = false;
      for (const dy of [-13, 11, -26]) {
        p.el.style.transform = "translate(" + p.x.toFixed(1) + "px," +
          (p.y + dy).toFixed(1) + "px) translate(-50%,-50%)";
        const r2 = p.el.getBoundingClientRect();
        if (hubRects.every(hr => free(r2, hr)) &&
            taken.every(t => free(r2, t))) { r = r2; ok = true; break; }
      }
      if (!ok) { p.el.style.display = "none"; continue; }
    }
    taken.push(r);
  }
}

// ---- crosstalk corridors: top inter-cluster pairs, labeled at the arc ----
// baked in DATA.meta.crosstalk (count desc); each pair claims the heaviest
// highway arc between its two clusters and labels that arc's baked midpoint
// (pts[8]). Overview-only: hidden while focusing or once the camera closes
// inside the LOD threshold (same gate family as the overview edge state).
const xtEl = document.getElementById("xtlabs");
let xtLabs = [];
(function buildXtLabs() {
  const xt = m.crosstalk || [];
  xt.forEach(p => {
    let best = null;
    for (const [li, pts] of hw) {
      const l = links[li];
      const ca = nodes[l.s].cluster, cb = nodes[l.t].cluster;
      if ((ca === p.a && cb === p.b) || (ca === p.b && cb === p.a)) {
        if (!best || l.w > best.w) best = { w: l.w, mid: pts[8] };
      }
    }
    if (!best) return;
    const el = document.createElement("div");
    el.className = "xtlab";
    el.textContent = (cNames[p.a] || "c" + p.a) + " - " +
      (cNames[p.b] || "c" + p.b) + " ×" + p.n;
    xtEl.appendChild(el);
    xtLabs.push({ mid: best.mid, el });
  });
})();
function updateXtLabels() {
  // collapse hides crosstalk labels too: the arcs they annotate re-target to
  // supernode centroids, so the "A - B ×n" captions would float over merged
  // piles pointing at nothing
  const overview = !collapsed && !focusSeeds.size && !query &&
    camera.position.distanceTo(controls.target) >= lodDist;
  if (!overview) {
    xtLabs.forEach(k => { k.el.style.display = "none"; });
    return;
  }
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  // greedy collision skip: two corridor labels stacked on the same screen
  // region read as garbage; the later one yields (first come = highest
  // crosstalk count, since meta.crosstalk is baked count-desc)
  const taken = [];
  const free = (a, b) => a.right < b.left - 4 || b.right < a.left - 4 ||
    a.bottom < b.top - 4 || b.bottom < a.top - 4;
  for (const k of xtLabs) {
    hubV.set(k.mid[0], k.mid[1], k.mid[2]).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.05 || Math.abs(hubV.y) > 1.05) {
      k.el.style.display = "none"; continue;
    }
    k.el.style.display = "block";
    k.el.style.transform = "translate(" + ((hubV.x*0.5+0.5)*w).toFixed(1) + "px," +
      ((-hubV.y*0.5+0.5)*h).toFixed(1) + "px) translate(-50%,-50%)";
    const r = k.el.getBoundingClientRect();
    if (taken.every(t => free(r, t))) taken.push(r);
    else k.el.style.display = "none";
  }
}

// ---- hub labels: top-degree visible files, projected to screen each frame ---
// label LOD (Gource --dir-name-depth / Obsidian text-fade steal): overview
// keeps the classic top-12 landmarks; zooming in raises the cap so context
// names appear exactly when the user is close enough to read them
const HUB_N = 12;
const HUB_MAX = 40;
let hubCapNow = HUB_N;   // live zoom-driven cap, exposed via __dbg.hubCap
const hubsEl = document.getElementById("hubs");
let hubs = [];
const hubV = new THREE.Vector3();
function rebuildHubs() {
  const vis = [];
  // degree-0 files render near-invisible spheres - their labels would
  // float as orphans, so they never earn one
  for (let i = 0; i < N; i++) if (degree[i] > 0 && nodeVisible(nodes[i])) vis.push(i);
  vis.sort((a, b) => degree[b] - degree[a]);
  hubsEl.innerHTML = "";
  hubs = vis.slice(0, HUB_MAX).map(i => {
    const el = document.createElement("div");
    el.className = "hub";
    el.textContent = nodes[i].label + " · " + Math.round(degree[i]);
    el.title = nodes[i].path;
    el.onpointerenter = () => { tip.style.display = "none"; };
    el.onclick = () => { pushFocusState(); showInfo(i); focusSeeds.clear(); focusSeeds.add(i); applyVisibility(); focus(i); mapCenterOn(i); };
    hubsEl.appendChild(el);
    return { i, el };
  });
}
// label hysteresis: each hub remembers the offset that last placed cleanly
// (keyed by node index). While that offset stays collision-free at the new
// projected position it is reused verbatim — no candidate re-search, so
// labels stop hopping between rows while the camera orbits. The search
// reruns only on a real collision or after a hidden frame.
const hubOff = new Map();
// junction bollards + delivery chevrons as screen-space obstacle points —
// shared by ALL label placers (skeptic r4 #3: hub/cluster labels sat on
// dots and trunks; only fn labels avoided them before)
const _obstV = new THREE.Vector3();
function juncArrowObstacles(w, h) {
  const pts = [];
  for (const mesh of [fnJDot, fnArrows]) {
    if (!mesh) continue;
    const am = mesh.instanceMatrix.array;
    for (let i = 0; i < am.length / 16; i++) {
      _obstV.set(am[i*16+12], am[i*16+13], am[i*16+14]).project(camera);
      if (_obstV.z <= 1 && Math.abs(_obstV.x) <= 1.05 && Math.abs(_obstV.y) <= 1.05)
        pts.push([(_obstV.x*0.5+0.5)*w, (-_obstV.y*0.5+0.5)*h]);
    }
  }
  return pts;
}

function updateHubs() {
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  // zoom-driven cap: squared falloff so labels bloom in as you approach
  const camDist = camera.position.distanceTo(controls.target);
  const cap = camDist >= lodDist * 1.2 ? HUB_N
    : Math.min(HUB_MAX, Math.max(HUB_N, Math.round(HUB_N / Math.pow(camDist / (lodDist * 1.2), 2))));
  hubCapNow = cap;
  // junction bollards + delivery chevrons are obstacles (skeptic r4 #3)
  const obstPts = juncArrowObstacles(w, h);
  const clearOfDots = r => obstPts.every(q =>
    q[0] < r.left - 8 || q[0] > r.right + 8 || q[1] < r.top - 8 || q[1] > r.bottom + 8);
  // greedy placement against real measured boxes; transforms only touch
  // these few absolutely-positioned nodes, so rect reads stay cheap.
  // vertical rows first (keeps label near its node), then sideways nudges
  const fixed = [];
  const free = (a, b) => a.right < b.left - 4 || b.right < a.left - 4 ||
    a.bottom < b.top - 4 || b.bottom < a.top - 4;
  for (let hi = 0; hi < hubs.length; hi++) {
    const { i, el } = hubs[hi];
    if (hi >= cap || alphaTgt[i] < 0.5) { el.style.display = "none"; hubOff.delete(i); continue; }
    hubV.set(pos[i*3], pos[i*3+1], pos[i*3+2]).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.02 || Math.abs(hubV.y) > 1.02) {
      el.style.display = "none"; hubOff.delete(i); continue;
    }
    const rawX = (hubV.x * 0.5 + 0.5) * w;
    // panel-avoidance: a label shoved >80px sideways from its node to
    // clear the info panel reads as an orphan — hide it instead
    if (310 - rawX > 80) { el.style.display = "none"; hubOff.delete(i); continue; }
    // clamp inside the viewport but clear of the left info panel
    const x = Math.max(310, Math.min(w - 30, rawX)), y = Math.max(16, Math.min(h - 26, (-hubV.y * 0.5 + 0.5) * h));
    el.style.display = "block";
    let r = null;
    const prev = hubOff.get(i);
    if (prev) {
      el.style.transform = "translate(" + (x + prev.dx).toFixed(1) + "px," +
        (y + prev.dy).toFixed(1) + "px) translate(-50%,0)";
      r = el.getBoundingClientRect();
      if (fixed.every(f => free(r, f)) && clearOfDots(r)) { fixed.push(r); continue; }
    }
    outer:
    for (const dy of [-19, 17, -42, 41, -65, 65, -88, 88]) {
      for (const dx of [0, 100, -100]) {
        el.style.transform = "translate(" + (x + dx).toFixed(1) + "px," +
          (y + dy).toFixed(1) + "px) translate(-50%,0)";
        r = el.getBoundingClientRect();
        if (fixed.every(f => free(r, f)) && clearOfDots(r)) { hubOff.set(i, { dx, dy }); break outer; }
      }
    }
    fixed.push(r);
  }
}
let stubExits = [];     // EXPLAINED EXIT dissolve points this frame (focus-file
                        // chains culled by the termini law; labeled in tick)
const legTermOn = new Map();  // per-leg chain-key -> terminus projects on-screen (tick fills)
const chainGate = new Map();  // per-chain first failing gate this frame (sighting #10 probe)
const inkKeys = new Set();    // chains carrying ink this frame (serve loop fills; sighting #11)
let wireTipAnchor = null;     // pinned card's anchor meta — lifetime-tracked per frame
function updateStubLabs() {
  // EXPLAINED EXIT labels: compact "→ station · file" at each dissolve
  // point (cap 4). On-viewport stubs first (busPts order — deterministic);
  // off-screen dissolve points get no label — nothing to explain where
  // there is no ink. Hidden when no exits.
  const host = document.getElementById("stubLabs");
  if (!host) return;
  const vw = renderer.domElement.clientWidth || 1600, vh = renderer.domElement.clientHeight || 900;
  // NDC -> px at LABEL time (canvas may resize between capture and draw;
  // map pane shifts #gl width — glW() — so live conversion stays true)
  const px = stubExits.map(e => ({ x: (e.nx + 1) / 2 * vw, y: (1 - e.ny) / 2 * vh, fi: e.fi, dest: e.dest }));
  // #panel (z10) and the map pane occlude canvas ink — a dissolve point
  // hidden under them has no visible terminus, so no label either (the
  // law binds labels to VISIBLE dissolve ink, not to geometry)
  const panel = document.getElementById("panel");
  const pr = panel ? panel.getBoundingClientRect() : null;
  const mapP = document.getElementById("mapPane");
  const mr = mapP && document.body.classList.contains("mapOpen") ? mapP.getBoundingClientRect() : null;
  const occl = (x, y) => (pr && x > pr.left - 8 && x < pr.right + 8 && y > pr.top - 8 && y < pr.bottom + 8) ||
                         (mr && x > mr.left - 8 && x < mr.right + 8);
  const on = px.filter(e => e.x > 8 && e.x < vw - 8 && e.y > 8 && e.y < vh - 8 && !occl(e.x, e.y));
  const want = (on.length ? on : []).slice(0, 4);
  while (host.children.length < want.length) {
    const el = document.createElement("div");
    el.className = "stublab"; host.appendChild(el);
  }
  for (let i = 0; i < host.children.length; i++) {
    const el = host.children[i];
    if (i < want.length) {
      const nm = nodes[want[i].fi] && nodes[want[i].fi].label || "?";
      el.style.display = "block";
      el.style.transform = "translate(" + Math.round(want[i].x + 8) + "px," +
                           Math.round(want[i].y - 8) + "px)";
      el.textContent = "→ station · " + nm;
    } else el.style.display = "none";
  }
}
// boot rebuildHubs() deleted - boot applyVisibility() re-runs it before the
// first render

// ---- function-level layer (files inside the current focus) -------------------
let fnMesh = null, fnLines = null, fnStalks = null, fnMeta = [], fnArrows = null, fnQuiet = null;
  let fnTrunkN = 0;   // file-pair bus trunks in the current fn layer (via __dbg)
  // conduit lane law: ALWAYS +Y — a -Y lift drops the conduit down INTO
  // the fn-box swarm it is supposed to overfly. Consecutive shared-
  // corridor trunks (trunkGeom order = deterministic visEdges order) get
  // tiered control lifts, so quadratic apexes sit 0.11/0.135/0.16*dist
  // above the midpoint and never stack on each other.
  const CONDUIT_LIFT_BASE = 0.14;  // band 0.14-0.29 over 6 tiers, step 0.03
  // below (apex law >= 0.11*dist holds); 0.22 was overflight — the hills
  // interleaved on screen and read as braid even where chords never cross
  // quiet-tier consolidation: file pairs with >= QUIET_TRUNK_MIN quiet
  // wires collapse into ONE background trunk (QUIET_LIFT_FRAC apex lift)
  const QUIET_TRUNK_MIN = 3, QUIET_LIFT_FRAC = 0.22;
let fnTrunkW = 0;   // wires riding trunks (each emits entry+exit ramps)
let fnJstubN = 0;   // shared junction legs: Jof delivery stubs + station tree legs
let fnStationsArr = [];  // per-file bus stations of the current fn layer (via __dbg)
let fnJclearV = -1; // min world junction->box-center distance (via __dbg)
let fnLegN = 0;     // station tree legs (thin conduits, via __dbg)
let fnJDotK = null;  // per-junction bollard size factor (screen-constant law)
let fnJDotOf = null;   // owner FILE index per bollard (round-5 LOD gate)
let fnJDotSt = null;   // 1 = station-class gate, 0 = own-file resolved gate
let fnJDotLegs = null;  // legs per STATION (0 = bridge class; sighting #9 gate)
let fnJDotKey = null;   // per-bollard attachment key: heads "T|fi" (serving trunk
                        // endpoint), subs their leg key — a bollard renders only
                        // when its attachment SERVED this frame (empty-station law)
const stAttKey = new Set();  // served attachment keys (serve loop fills, jDot gate reads)
let fnBoxScale = null; // fi -> largest rendered fn-box world size
let stationTks = null; // fi -> trunk keys leaving that file's stations
let arrowFile = null;  // owner FILE index per delivery chevron
let _lod = null;       // per-frame LOD closures (res / trOK / stOK)
let _lodServe = false; // focus-state master gate (busLodInit) — clamps the
                       // distance fade below: served layer renders at full
                       // opacity (skeptic r5-final: 0.60/0.40 dims made the
                       // serving d2 state illegible — geometry served, paint
                       // faded)
let _oDot = 0, _oArrow = 0;   // effective opacities for fnLod reporting
// click-parity (user r5): the white trunk conduits + ivory junction dots
// must pick EXACTLY like the colored bus wires — same metas, same tip card
let trunkMetaMap = null;   // busPts key "a>b" -> {kind:"trunk", sf, tf, mates}
let busPtsMeta = null;     // parallel to busPts: per-conduit pick meta
let juncPickInfo = null;   // parallel to busJunc: per-dot pick meta
let legendOpen = false;    // 3D legend chip state (harness-pinned)
let fnLodV = null;     // per-frame LOD report (via __dbg.fnLod, round-5)
let fnJclip = 0;    // conduits whose obstacle lift hit the cap (via __dbg)
let fnQuietTrunkN = 0;  // quiet-tier trunk arcs (via __dbg)
let fnQuietTrunkW = 0;  // quiet wires absorbed into trunks (via __dbg)
let fnBus = null;   // trunk conduit bodies (InstancedMesh cylinders)
let busPts = null;  // segment endpoints for per-frame screen-constant rescale
let fnBusRi = null; // current per-segment radius (world units)
let fnJDot = null;  // reroute junction bollards (InstancedMesh spheres)
  let fnJDotPos = null, fnJDotR = null;  // world positions + current radii (screen-constant)
  let fnArrowPos = null, fnArrowR = null;   // arrowhead positions + current radii (screen-constant)
  let fnArrowTang = null;  // world wire tangent at each delivery (chevron aim)
  let fnArrowBox = null;  // delivery box center per arrow (screen-hug clamp)

// aimArrows(): every delivery chevron billboard-faces the camera with
// local +Y along the wire's projected tangent, sized by the EXACT
// screen-px law (half-height 5px -> ~10px tall chevron at any zoom —
// skeptic r4 bar >=8px). All inputs (camera, fov, viewport, arrow world
// data) are deterministic per view, so R10 determinism holds.
let _arrowOccl = new Float32Array(0);   // 1 = delivery chevron occluded
let _arrowOcclCam = null;              // camera pos of the last occlusion pass
function aimArrows() {
  if (!fnArrows || !fnArrowPos || !fnArrowTang || !fnArrowR) return;
  const hpx = renderer.domElement.clientHeight || 900;
  const wuPerPx = 2 * Math.tan(camera.fov * Math.PI / 360) / hpx;
  const M = new THREE.Matrix4(), X = new THREE.Vector3(), Y = new THREE.Vector3(),
        Z = new THREE.Vector3(), P = new THREE.Vector3(), T = new THREE.Vector3();
  const a = fnArrows.instanceMatrix.array;
  const lod = _lod || busLodInit();   // round-5: no chevrons on sub-pixel boxes
  // VISIBLE-OR-GONE (skeptic r4): a chevron buried behind a sphere swarm
  // reads as noise — depthTest:false paints it over everything anyway, so
  // gate on a real ray. Recomputed only when the camera moves (closed-form
  // inputs; determinism per view holds).
  if (_arrowOccl.length !== fnArrowR.length) {
    _arrowOccl = new Float32Array(fnArrowR.length);
    _arrowOcclCam = null;
  }
  // recompute EVERY frame: the settle-moment state must be a pure function
  // of the final camera pose (determinism — chevShown differed 16 vs 5 across
  // reloads when the last pass ran mid-damping). Cost ~1ms for 45 rays.
  if (true) {
    _arrowOccl.fill(0);
    const rc = new THREE.Raycaster();
    rc.far = Infinity;
    const dir = new THREE.Vector3(), org = new THREE.Vector3();
    for (let i = 0; i < fnArrowR.length; i++) {
      org.copy(camera.position);
      dir.set(fnArrowPos[i*3], fnArrowPos[i*3+1], fnArrowPos[i*3+2]).sub(org);
      const L = dir.length() || 1;
      dir.divideScalar(L);
      rc.set(org, dir);
      rc.far = L - 1;   // anything solid closer than the chevron blocks it
      const occ = fileMesh.visible ? [fileMesh, fnMesh, fnBus] : [fnMesh, fnBus];
      const hits = rc.intersectObjects(occ.filter(Boolean), false);
      // the delivery chevron rides 3.5wu off its TARGET box center — when
      // the wire arrives from the far side, the box's near face legitimately
      // sits between camera and chevron. That is the DELIVERY, not an
      // occluder: ignore hits on the own target box (within 4wu of it)
      let blocked = false;
      const bo = fnArrowBox ? [fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]] : null;
      for (const h of hits) {
        if (bo && h.point.distanceTo(new THREE.Vector3(bo[0], bo[1], bo[2])) < 4) continue;
        blocked = true; break;
      }
      if (blocked) _arrowOccl[i] = 1;
    }
    _arrowOcclCam = camera.position.clone();
  }
  const w = renderer.domElement.clientWidth || 1600, h = hpx;
  for (let i = 0; i < fnArrowR.length; i++) {
    P.set(fnArrowPos[i*3], fnArrowPos[i*3+1], fnArrowPos[i*3+2]);
    const bx0 = fnArrowBox ? fnArrowBox[i*3] : 0, by0 = fnArrowBox ? fnArrowBox[i*3+1] : 0,
          bz0 = fnArrowBox ? fnArrowBox[i*3+2] : 0;
    T.set(fnArrowTang[i*3], fnArrowTang[i*3+1], fnArrowTang[i*3+2]);
    Z.subVectors(camera.position, P).normalize();
    // project the wire tangent into the billboard plane; degenerate
    // (tangent along the view axis) falls back to world-up
    Y.copy(T).addScaledVector(Z, -T.dot(Z));
    if (Y.lengthSq() < 1e-6) Y.set(0, 1, 0).addScaledVector(Z, -Z.y);
    Y.normalize();
    X.crossVectors(Y, Z);
    // screen-hug clamp: at grazing angles a 3.5wu world offset projects
    // 30-60px from the box (skeptic aFar >25px bar) — pull the anchor
    // toward the box until it sits <=14px from it ON SCREEN
    if (fnArrowBox) {
      const q = _obstV.set(fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]).project(camera);
      const qx = (q.x*0.5+0.5)*w, qy = (-q.y*0.5+0.5)*h;
      for (let t = 1; t > 0.02; t -= 0.08) {
        _obstV.set(bx0 + (fnArrowPos[i*3]-bx0)*t, by0 + (fnArrowPos[i*3+1]-by0)*t,
                   bz0 + (fnArrowPos[i*3+2]-bz0)*t).project(camera);
        const ax2 = (_obstV.x*0.5+0.5)*w, ay2 = (-_obstV.y*0.5+0.5)*h;
        if (Math.hypot(ax2-qx, ay2-qy) <= 14) { P.set(
          bx0 + (fnArrowPos[i*3]-bx0)*t, by0 + (fnArrowPos[i*3+1]-by0)*t,
          bz0 + (fnArrowPos[i*3+2]-bz0)*t); break; }
        if (t <= 0.12) P.set(bx0, by0, bz0);   // extreme grazing: park ON the box
      }
    }
    // occluded deliveries collapse INTO their box (scale 0) — tested a 0.55x/
    // 0.45x hint V to lift chevObs, but each un-hidden arrow adds chevCrowd
    // pairs at delivery sites (hub6 zoomin 16->19, tol +2): hidden stays.
    if (_arrowOccl[i] && fnArrowBox) P.set(fnArrowBox[i*3], fnArrowBox[i*3+1], fnArrowBox[i*3+2]);
    const d = P.distanceTo(camera.position) || 1;
    const lodOk = !arrowFile || !arrowFile.length || arrowFile[i] < 0 || lod.resA(arrowFile[i]);
    // viewport-fraction law (user directive): 12px on the nominal 900px
    // canvas at ANY window size — same fraction of frame, ref-px invariant
    // viewport-FRACTION law: half-height 8px on the nominal 900 canvas at
    // ANY window — same fraction of frame height, ref-px invariant. 8 not 6:
    // at the USER's 735px window the chunky V's saturated mass read ~6 CSS px
    // (pair-engineer zoom-crop: 'direction not reliably readable at native');
    // 16px@900 keeps the V-mass >= 8px at the smallest window we test
    const s = 8 * (2 * Math.tan(camera.fov * Math.PI / 360) / 900) * d
             * ((lodOk && !_arrowOccl[i]) ? 1 : 0);
    if (fnLodV && s > 0.01) {
      fnLodV.chevShown++;
      fnLodV.minChevPx = Math.min(fnLodV.minChevPx, ((2 * s) / (wuPerPx * d)) * (900 / hpx));
    }
    // keep the probe-visible record in sync (fnArrowPos = CURRENT anchor)
    fnArrowPos[i*3] = P.x; fnArrowPos[i*3+1] = P.y; fnArrowPos[i*3+2] = P.z;
    M.makeBasis(X.multiplyScalar(s), Y.multiplyScalar(s), Z);
    M.setPosition(P);
    M.toArray(a, i * 16);
  }
  fnArrows.instanceMatrix.needsUpdate = true;
}

// ---- focus labels: name neighboring files + function satellites on focus ----
const flabsEl = document.getElementById("flabs");
let fLabs = [];
function rebuildFocusLabels(focusing) {
  fLabs = [];
  flabsEl.innerHTML = "";
  if (!focusing) return;
  // every directly-connected file node gets its name back (hubs own theirs)
  const hubIdx = new Set(hubs.map(h => h.i));
  for (let i = 0; i < N; i++) {
    if (level[i] !== 1 || hubIdx.has(i) || alphaTgt[i] <= 0.5) continue;
    const el = document.createElement("div");
    el.className = "flab";
    el.textContent = nodes[i].label;
    el.title = nodes[i].path;
    el.onclick = () => { pushFocusState(); focusSeeds.clear(); focusSeeds.add(i); showInfo(i); buildContainment(); applyVisibility(); focus(i); };
    flabsEl.appendChild(el);
    fLabs.push({ kind: 0, i, ix: -1, el });
  }
  // function satellites: focused file's own fns first, then one hop out —
  // capped so the layer stays readable
  if (fnMeta.length) {
    const own = [], near = [];
    fnMeta.forEach((m, ix) => {
      if (level[m.file] === 0) own.push(ix);
      else if (level[m.file] === 1 && alphaTgt[m.file] > 0.5) near.push(ix);
    });
    // cap + prefer state writers: labels are the densest channel, so when
    // the neighborhood is busy the pure functions yield their labels first
    const cands = own.concat(near);
    const writerFirst = (a, b) => {
      const ioa = DATA.fio && DATA.fio[nodes[fnMeta[a].file].path + "::" + fnMeta[a].name];
      const iob = DATA.fio && DATA.fio[nodes[fnMeta[b].file].path + "::" + fnMeta[b].name];
      return ((iob && iob.w.length) ? 1 : 0) - ((ioa && ioa.w.length) ? 1 : 0);
    };
    cands.sort(writerFirst).slice(0, 32).forEach(ix => {
      const m = fnMeta[ix];
      if (m.agg && !m.count) return;   // box collapsed into a wire aggregate
      const el = document.createElement("div");
      el.className = "flab fn";
      if (m.count) {   // per-file aggregate: 'n×' badge; tooltip lists the names
        const names = fnMeta.filter(x => x.agg && !x.count && x.file === m.file)
                            .map(x => x.name);
        const listed = names.slice(0, 12).join(", ") +
                       (names.length > 12 ? " +" + (names.length - 12) + " more" : "");
        el.textContent = m.count + "×";
        el.title = nodes[m.file].path + " :: " + m.count + " fns" + (listed ? "\n" + listed : "");
        flabsEl.appendChild(el);
        fLabs.push({ kind: 1, i: m.file, ix, el });
        return;
      }
      const io = DATA.fio && DATA.fio[nodes[m.file].path + "::" + m.name];
      // writes-state badge (tier-2 metadata per the LOD ladder; single
      // glyph channel — color stays cluster-owned)
      el.textContent = "ƒ " + m.name + (io && io.w.length ? " ✎" + io.w.length : "");
      if (mutOnly && (!io || !io.w.length)) return;   // mutators-only filter
      el.title = nodes[m.file].path + " :: " + m.name;
      el.onclick = () => showFnInfo(ix);
      flabsEl.appendChild(el);
      fLabs.push({ kind: 1, i: m.file, ix, el });
    });
  }
}
const _flabV = new THREE.Vector3();
function updateFocusLabels() {
  if (!fLabs.length) return;
  const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
  const clearOf = (a, b) => a.right < b.left - 2 || b.right < a.left - 2 ||
    a.bottom < b.top - 2 || b.bottom < a.top - 2;
  const hubRects = [...document.querySelectorAll(".hub")]
    .filter(e => e.style.display === "block").map(e => e.getBoundingClientRect());
  const taken = [];
  // station dots are label obstacles (skeptic R8): project them once per
  // frame — no flab may cover a junction bollard
  const stPts = [];
  for (const S of fnStationsArr) {
    for (const q of [S.p, ...S.subJ]) {
      _flabV.set(q[0], q[1], q[2]).project(camera);
      if (_flabV.z <= 1 && Math.abs(_flabV.x) <= 1.02 && Math.abs(_flabV.y) <= 1.02)
        stPts.push([(_flabV.x * 0.5 + 0.5) * w, (-_flabV.y * 0.5 + 0.5) * h]);
    }
  }
  // 14px margin: at 0.72 kf the marginal label straddled the old 10px
  // line and flickered 0<->1 overlap across settle frames
  const clearDots = r => stPts.every(q =>
    q[0] < r.left - 14 || q[0] > r.right + 14 || q[1] < r.top - 14 || q[1] > r.bottom + 14);
  // delivery arrowheads are obstacles too (a label chip sat ON an arrow
  // in the r3 crop — direction markers must never be covered)
  if (fnArrows) {
    const am = fnArrows.instanceMatrix.array;
    for (let i = 0; i < am.length / 16; i++) {
      _flabV.set(am[i*16+12], am[i*16+13], am[i*16+14]).project(camera);
      if (_flabV.z <= 1 && Math.abs(_flabV.x) <= 1.02 && Math.abs(_flabV.y) <= 1.02)
        stPts.push([(_flabV.x * 0.5 + 0.5) * w, (-_flabV.y * 0.5 + 0.5) * h]);
    }
  }
  for (const f of fLabs) {
    const p = f.kind === 1 ? fnMeta[f.ix].p : null;
    _flabV.set(
      f.kind === 1 ? p[0] : pos[f.i*3],
      f.kind === 1 ? p[1] : pos[f.i*3+1],
      f.kind === 1 ? p[2] : pos[f.i*3+2]).project(camera);
    if (_flabV.z > 1 || Math.abs(_flabV.x) > 1.02 || Math.abs(_flabV.y) > 1.02) {
      f.el.style.display = "none"; continue;
    }
    const x = (_flabV.x * 0.5 + 0.5) * w, y = (-_flabV.y * 0.5 + 0.5) * h;
    const base = "translate(" + x.toFixed(1) + "px," + y.toFixed(1) + "px) translate(-50%,-100%)";
    f.el.style.display = "block";
    f.el.style.transform = base;
    // fn satellites now mutually collide-avoid (ink#4): 18px screen gap to
    // every placed rect — the dense-swarm label shagpile; a blocked label
    // hides (element stays; next frame re-tries as the camera moves)
    if (f.kind === 1) {
      const r1 = f.el.getBoundingClientRect();
      const clear18 = t => r1.right < t.left - 18 || t.right < r1.left - 18 ||
        r1.bottom < t.top - 18 || t.bottom < r1.top - 18;
      if (!taken.every(clear18) || !clearDots(r1)) { f.el.style.display = "none"; continue; }
      taken.push(r1);
      continue;
    }
    let r = f.el.getBoundingClientRect();
    if (hubRects.some(hr => !clearOf(r, hr)) || taken.some(t => !clearOf(r, t)) || !clearDots(r)) {
      let ok = false;
      for (const dy of [16, -14, 32, -30]) {
        f.el.style.transform = "translate(" + x.toFixed(1) + "px," + (y + dy).toFixed(1) + "px) translate(-50%,-100%)";
        r = f.el.getBoundingClientRect();
        if (hubRects.every(hr => clearOf(r, hr)) && taken.every(t => clearOf(r, t)) && clearDots(r)) { ok = true; break; }
      }
      if (!ok) { f.el.style.display = "none"; continue; }
    }
    taken.push(r);
  }
}
// boot rebuildFocusLabels(false) deleted - boot applyVisibility() re-runs it
function rebuildFnLayer(focusing) {
  if (fnMesh) { scene.remove(fnMesh); fnMesh.geometry.dispose(); fnMesh.dispose(); fnMesh = null; }
  if (fnLines) { scene.remove(fnLines); fnLines.geometry.dispose(); fnLines = null; }
  if (fnQuiet) { scene.remove(fnQuiet); fnQuiet.geometry.dispose(); fnQuiet = null; }
  if (fnBus) { scene.remove(fnBus); fnBus.geometry.dispose(); fnBus = null; busPts = null; fnBusRi = null; busPtsMeta = null; trunkMetaMap = null; juncPickInfo = null; }
    if (fnJDot) { scene.remove(fnJDot); fnJDot.geometry.dispose(); fnJDot = null; fnJDotPos = null; fnJDotR = null; }
  if (fnArrows) { scene.remove(fnArrows); fnArrows.geometry.dispose(); fnArrows = null; fnArrowPos = null; fnArrowR = null; fnArrowTang = null; fnArrowBox = null; arrowFile = null; }
  if (fnStalks) { scene.remove(fnStalks); fnStalks.geometry.dispose(); fnStalks = null; }
  fnMeta = [];
  if (!fnMode || !focusing) return;
  // pass 1: visible cross-file fn edges
  const visEdges = [];
  fedges.forEach(e => {
    const sf = e[0], df = e[2];
    if (level[sf] < 0 || level[df] < 0) return;
    // hidden files (tests/tools filter, dir filter): their function nodes
    // and edges must not render in the fn layer either
    if (alphaTgt[sf] <= 0.5 || alphaTgt[df] <= 0.5) return;
    visEdges.push(e);
  });
  // fn-layer scope: rank wires deterministically (file-pair link weight,
  // then name/line) so eidx order is stable across reloads. No cap — every
  // fn interconnection between lit files renders (amendment).
  const lw = new Map();
  links.forEach(l => lw.set(l.s + ":" + l.t, l.w));
  const eW = e => lw.get(e[0] + ":" + e[2]) || 0;
  visEdges.sort((a, b) => (eW(b) - eW(a)) || (a[0] - b[0]) ||
    (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || (a[2] - b[2]) ||
    (a[3] < b[3] ? -1 : a[3] > b[3] ? 1 : 0) || (a[4] - b[4]));
  // amendment: every fn interconnection between lit files renders — no
  // budget cap at fn grain (the lit set is 1-hop + compacted; AGG_MAX
  // still declutters per-file box rings, but wires stay complete)
  if (!visEdges.length) return;
  const fIdx = new Map(), fpos = [], fcol = [], eidx = [], wireRows = [];
  // mutators-only filter: when on, fn satellites for functions with no
  // member writes are not created at all (their wires collapse with them)
  const ioOf = (fi, name) => (DATA.fio || {})[nodes[fi].path + "::" + name];
  const isMutator = (fi, name) => {
    const io = ioOf(fi, name);
    return !!(io && io.w.length);
  };
  const nodeOf = (e, isSrc) => {
    const fi = isSrc ? e[0] : e[2], name = isSrc ? e[1] : e[3];
    if (mutOnly && !isMutator(fi, name)) return -1;
    const k = fi + "::" + name;
    let ix = fIdx.get(k);
    if (ix === undefined) {
      ix = fnMeta.length; fIdx.set(k, ix);
      // position filled by the owner-arc pass below (m.p is the hover and
      // click anchor, so it must end up at the rendered box)
      fnMeta.push({ file: fi, name, p: [0, 0, 0] });
      fpos.push(0, 0, 0);
      fcol.push(colArr[fi*3], colArr[fi*3+1], colArr[fi*3+2]);
    }
    return ix;
  };
  visEdges.forEach(e => {
    const a = nodeOf(e, true), b = nodeOf(e, false);
    if (a < 0 || b < 0) return;   // filtered out by mutators-only
    eidx.push(a, b);
    wireRows.push(e);   // parallel row for wire descriptions (line number)
  });
  if (!fnMeta.length) return;
  // pass 2: fn boxes orbit their OWNER file sphere. Each lit file's fns
  // sit on a 120-degree arc around the sphere (radius grows +6 every 3rd
  // box so consecutive boxes never stack), centered on the mean XZ
  // direction toward the centroid of the owner's outgoing-wire targets —
  // neighbors sorted by index so the sum is deterministic; owners with no
  // outgoing wires face their callers instead. Ownership then reads at a
  // glance: box color, a short stalk to the sphere surface, and the arc's
  // facing all point at the owner, where wire midpoints blurred it.
  const AGG_MAX = 6;
  const byFile = new Map();
  for (let i = 0; i < fnMeta.length; i++) {
    const fi = fnMeta[i].file;
    let arr = byFile.get(fi);
    if (!arr) byFile.set(fi, arr = []);
    arr.push(i);
  }
  const aggs = [];
  for (const [fi, arr] of byFile) {
    const tgts = new Set(), srcs = new Set();
    for (const e of visEdges) {
      if (e[0] === fi) tgts.add(e[2]);
      if (e[2] === fi) srcs.add(e[0]);
    }
    const facing = [...(tgts.size ? tgts : srcs)].sort((a, b) => a - b);
    let mx = 0, mz = 0;
    for (const j of facing) { mx += pos[j*3] - pos[fi*3]; mz += pos[j*3+2] - pos[fi*3+2]; }
    const th = (mx || mz) ? Math.atan2(mz, mx) : 0;
    const oR = sphR(fi);   // rendered sphere radius (satellite-boosted)
    const arcR = oR + 14;
    const n = arr.length;
    // multi-ring arc: 12 boxes per ring, +8 out per ring — a 40-fn subject
    // spreads over 4 readable rings instead of collapsing into one 'n×'
    const rows = Math.max(1, Math.ceil(n / 12));
    const per = Math.ceil(n / rows);
    for (let i = 0; i < n; i++) {
      const m = fnMeta[arr[i]];
      const ring = Math.floor(i / per), k = i % per;
      const cnt = Math.min(per, n - ring * per);
      const ang = th - Math.PI / 3 + (Math.PI * 2 / 3) * (k + 0.5) / cnt;
      const r = arcR + ring * 8;
      m.p[0] = pos[fi*3] + Math.cos(ang) * r;
      m.p[1] = pos[fi*3+1];
      m.p[2] = pos[fi*3+2] + Math.sin(ang) * r;
      fpos[arr[i]*3] = m.p[0]; fpos[arr[i]*3+1] = m.p[1]; fpos[arr[i]*3+2] = m.p[2];
    }
    // file aggregation: a file owning more than AGG_MAX fns collapses to
    // ONE 'n×' box at the arc center — a ring of 7+ scale-4 boxes reads as
    // clutter, and '7×' carries the information denser. fnMeta keeps its
    // indices (flabs + hover map by ix): members keep their arc slot but
    // render scale-0 and are not hoverable; the aggregate is an appended
    // entry with count=n.
    if (n > AGG_MAX && level[fi] > 0) {   // the focus subject keeps every fn visible
      for (const ix of arr) fnMeta[ix].agg = true;
      // sphereClear parity: individual boxes ride rings arcR..arcR+12; the
      // aggregate takes the OUTER ring (arcR+12) so a big owner sphere plus
      // its hover lift never swallows it
      aggs.push({ file: fi, count: n,
        p: [pos[fi*3] + Math.cos(th) * (arcR + 12), pos[fi*3+1],
            pos[fi*3+2] + Math.sin(th) * (arcR + 12)] });
    }
  }
  for (const ag of aggs) {
    fpos.push(ag.p[0], ag.p[1], ag.p[2]);
    fcol.push(colArr[ag.file*3], colArr[ag.file*3+1], colArr[ag.file*3+2]);
    fnMeta.push({ file: ag.file, name: "", p: ag.p, count: ag.count });
  }
  // collapsed members (agg, no count) render at scale 0 — wires and
  // chevrons that touch them must land on the file's VISIBLE aggregate
  // box, else deliveries aim at invisible boxes and read as floating
  const aggP = new Map();
  for (const ag of aggs) aggP.set(ag.file, ag.p);
  // fn boxes: true 3D cubes on their owner arcs, colored by owning cluster hue
  const fdummy = new THREE.Object3D();
  fnMesh = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshLambertMaterial(),
    fnMeta.length
  );
  fnMesh.frustumCulled = false;   // positions rebuilt on every focus change
  for (let i = 0; i < fnMeta.length; i++) {
    fdummy.position.set(fpos[i*3], fpos[i*3+1], fpos[i*3+2]);
    const m = fnMeta[i];
    fdummy.scale.setScalar(m.count ? 6 : (m.agg ? 0 : 4));
    fdummy.updateMatrix();
    fnMesh.setMatrixAt(i, fdummy.matrix);
    _col.setRGB(fcol[i*3], fcol[i*3+1], fcol[i*3+2]);
    fnMesh.setColorAt(i, _col);
  }
  fnMesh.instanceMatrix.needsUpdate = true;
  if (fnMesh.instanceColor) fnMesh.instanceColor.needsUpdate = true;
  scene.add(fnMesh);
  // persistent fn-ownership stalks: one SHORT segment per rendered box from
  // the box down to its owner's sphere SURFACE, in the owner's own hue —
  // the arc already says "these fns live here", the stalk pins each box to
  // its sphere. The bright hover stalk (fnStalk) rides on top of these;
  // lifetime matches the fn layer (rebuilt + disposed with it, so cbFn
  // visibility carries over).
  const stk = new Float32Array(fnMeta.length * 6);
  const stc = new Float32Array(fnMeta.length * 6);
  for (let i = 0; i < fnMeta.length; i++) {
    const m = fnMeta[i], o = i * 6;
    const collapsed = m.agg && !m.count;   // no box → no stalk
    let ux = m.p[0] - pos[m.file*3], uy = m.p[1] - pos[m.file*3+1], uz = m.p[2] - pos[m.file*3+2];
    const ul = Math.sqrt(ux*ux + uy*uy + uz*uz) || 1;
    const oR = sphR(m.file);
    stk[o]   = m.p[0]; stk[o+1] = m.p[1]; stk[o+2] = m.p[2];
    stk[o+3] = collapsed ? m.p[0] : pos[m.file*3] + ux / ul * oR;
    stk[o+4] = collapsed ? m.p[1] : pos[m.file*3+1] + uy / ul * oR;
    stk[o+5] = collapsed ? m.p[2] : pos[m.file*3+2] + uz / ul * oR;
    for (let v = 0; v < 6; v += 3) {
      stc[o+v] = fcol[i*3]; stc[o+v+1] = fcol[i*3+1]; stc[o+v+2] = fcol[i*3+2];
    }
  }
  const g3 = new THREE.BufferGeometry();
  g3.setAttribute("position", new THREE.BufferAttribute(stk, 3));
  g3.setAttribute("color", new THREE.BufferAttribute(stc, 3));
  fnStalks = new THREE.LineSegments(g3, new THREE.LineBasicMaterial(
    { vertexColors: true, transparent: true, opacity: 0.5, depthWrite: false }));
  scene.add(fnStalks);
  // call wires connect box to box — arcs park near their owners, so these
  // read as short owner-to-owner jumps through the fn boxes
  // call wires connect box to box as gentle arcs (per-wire lift separates
  // crossings in height). Two TIER laws keep a depth-1 hub readable:
  //   - BRIGHT: wires touching a focus-subject file (level 0) — 0.75 ink,
  //     gradient source→target, arrowhead cone, dash-flow
  //   - QUIET: neighbor↔neighbor wires — same geometry, 0.16 ink, no arrows
  //     (the user: "extreme clutter on the game manager ... wires all over
  //     the place"). Present for tracing, silent in the overview.
  const FS = 8;
  const mk = () => ({ ep: [], ec: [], ed: [], meta: [] });
  const tierB = mk(), tierQ = mk();
  const aPos = [], aDir = [], aCol = [], aBox = [];
  const cA = new THREE.Color(), cB = new THREE.Color();
  // BUS pass — two granularities, both SHARED-DESTINATION only (blind
  // bundling hurts path tracing, McGee & Dingliana 2012):
  //  1. FILE-PAIR TRUNKS: >=3 bright wires between the same two files
  //     collapse into ONE trunk arc (the 2D spine law brought to 3D).
  //     Member wires become short taps from their fn box to the trunk
  //     midpoint — no per-wire arrows (the cone storm was the clutter);
  //     the trunk carries the one direction arrow.
  //  2. FN JUNCTIONS: among non-trunked wires, >=3 into the same fn box
  //     merge at an onramp short of the box (one trunk, one pin arrow).
  const pairCnt = new Map(), qPairCnt = new Map(), wireTier = [];
  for (let i = 0; i < eidx.length; i += 2) {
    const a = eidx[i], b = eidx[i+1];
    const bright = level[fnMeta[a].file] === 0 || level[fnMeta[b].file] === 0;
    wireTier.push(bright ? 1 : 0);
    const k = fnMeta[a].file + ">" + fnMeta[b].file;
    if (bright) pairCnt.set(k, (pairCnt.get(k) || 0) + 1);
    else qPairCnt.set(k, (qPairCnt.get(k) || 0) + 1);
  }
  const trunked = new Set();
  for (const [k, n] of pairCnt) if (n >= 3) trunked.add(k);
  const qTrunked = new Set();
  for (const [k, n] of qPairCnt) if (n >= QUIET_TRUNK_MIN) qTrunked.add(k);
  // ---- bus stations (bus3d_spec D1-D3): per-file junctions on a ring
  // BEYOND the outermost occupied fn-box ring — provably open air. The
  // old sphR*1.16 entry shell sat INSIDE ring 1 for every lit file
  // (1.16*sphR < sphR+14 whenever sphR < 87.5; max seen 32.1), which is
  const STATION_R = 32, SUBJ_R = 27;
  // shell could never escape the swarm radially.
  const ringOut = new Map();   // fi -> outermost occupied fn-box ring
  for (const [fi, arr] of byFile) {
    // aggregated files (n > AGG_MAX, non-focus) render ONE 'n×' box at
    // arcR+12 — 12 PAST the member rings; it owns the outermost radius
    const agg = arr.length > AGG_MAX && level[fi] > 0;
    ringOut.set(fi, Math.max(sphR(fi) + 14 + (Math.max(1, Math.ceil(arr.length / 12)) - 1) * 8,
                               agg ? sphR(fi) + 26 : 0));
  }
  const boxesOf = new Map();   // fi -> rendered fn indices (agg members out)
  for (let i = 0; i < fnMeta.length; i++) {
    const m = fnMeta[i]; if (m.agg && !m.count) continue;
    let arr = boxesOf.get(m.file); if (!arr) boxesOf.set(m.file, arr = []);
    arr.push(i);
  }
  const angMin = (a, b) => { const d = Math.abs(a - b) % (2*Math.PI);
    return d > Math.PI ? 2*Math.PI - d : d; };
  const cirMean = bs => { let x = 0, z = 0;
    for (const b of bs) { x += Math.cos(b); z += Math.sin(b); }
    return Math.atan2(z, x); };
  const brgOf = (fi, p) => Math.atan2(p[2] - pos[fi*3+2], p[0] - pos[fi*3]);
  // scored bearing: 16 fixed candidates + the mean-fan bearing — argmin
  // occlusion against EVERY rendered box (own swarm first, foreign
  // swarms too — rubric clearance is file-agnostic) + a small pull
  // toward the mean. Ties: lower index.
  const allBoxes = [];
  for (const [, arr] of boxesOf) for (const ix of arr) allBoxes.push(ix);
  const stationAt = (fi, mean) => {
    const R = (ringOut.get(fi) || sphR(fi)) + STATION_R;
    const cx = pos[fi*3], cy = pos[fi*3+1], cz = pos[fi*3+2];
    let best = null;
    for (let ci = 0; ci <= 16; ci++) {
      const phi = ci < 16 ? ci * Math.PI / 8 : mean;
      const px = cx + Math.cos(phi) * R, pz = cz + Math.sin(phi) * R;
      let cost = 0.4 * angMin(phi, mean) * angMin(phi, mean);
      for (const ix of allBoxes) {
        const dx = px - fpos[ix*3], dy = cy - fpos[ix*3+1], dz = pz - fpos[ix*3+2];
        const d2 = dx*dx + dy*dy + dz*dz;
        if (d2 > 3600) continue;   // 60 wu and out: no say
        cost += 1 / Math.max(d2, 36);
      }
      // +Y depth stagger: same-plane dots merge edge-on (skeptic R3) —
      // stations float 12 wu above the box plane; tree legs approach in
      // the empty lane BELOW the horizontal trunk fan
      if (!best || cost < best.cost - 1e-12)
        best = { cost, phi, p: [px, cy + 16, pz] };
    }
    return best;
  };
  // sector split: cut at the largest circular gap when spread > 2.1 rad,
  // one more internal cut if a side is still wide (<= 3 groups).
  const sectorize = items => {
    if (items.length < 2) return [items.slice()];
    const s = items.slice().sort((a, b) => a.brg - b.brg);
    let gi = 0, gw = -1;
    for (let i = 0; i < s.length; i++) {
      const nx = i + 1 < s.length ? s[i+1].brg : s[0].brg + 2*Math.PI;
      if (nx - s[i].brg > gw) { gw = nx - s[i].brg; gi = i; }
    }
    if (2*Math.PI - gw <= 2.1) return [s];
    const cut2 = g => {
      if (g.length < 2 || g[g.length-1].brg - g[0].brg <= 2.1) return [g];
      let j = 0, w = -1;
      for (let i = 0; i + 1 < g.length; i++)
        if (g[i+1].brg - g[i].brg > w) { w = g[i+1].brg - g[i].brg; j = i; }
      return [g.slice(0, j+1), g.slice(j+1)];
    };
    return cut2(s.slice(gi + 1)).concat(cut2(s.slice(0, gi + 1)));
  };
  // per-FILE trunked-pair groups (arrivals and departures SHARE one
  // station per sector — a Blueprint reroute is bidirectional, and split
  // in/out stations 13 wu apart made their trunk fans graze at ~0.4 wu)
  const stGrp = new Map();
  for (const k of trunked) {
    const parts = k.split(">"), sf = +parts[0], tf = +parts[1];
    for (const [fi, oth, role] of [[sf, tf, 0], [tf, sf, 1]]) {
      let g = stGrp.get(fi);
      if (!g) stGrp.set(fi, g = { fi, items: [], wires: 0 });
      g.items.push({ tk: k, role, w: pairCnt.get(k),
        brg: Math.atan2(pos[oth*3+2] - pos[fi*3+2], pos[oth*3] - pos[fi*3]) });
      g.wires += pairCnt.get(k);
    }
  }
  const feedOf = new Map();   // tk -> {out:[src box], in:[tgt box]}
  for (let i = 0, p = 0; i < eidx.length; i += 2, p++) {
    if (!wireTier[p]) continue;
    const k = fnMeta[eidx[i]].file + ">" + fnMeta[eidx[i+1]].file;
    if (!trunked.has(k)) continue;
    let f = feedOf.get(k); if (!f) feedOf.set(k, f = { out: [], in: [] });
    if (!fnMeta[eidx[i]].agg || fnMeta[eidx[i]].count) f.out.push(eidx[i]);
    if (!fnMeta[eidx[i+1]].agg || fnMeta[eidx[i+1]].count) f.in.push(eidx[i+1]);
  }
  const stations = new Map();  // tk -> {0: srcStation, 1: tgtStation}
  const stList = [];           // all stations, stable order
  for (const [, g] of stGrp)
    for (const grp of sectorize(g.items)) {
      const mean = cirMean(grp.map(x => x.brg));
      const st = stationAt(g.fi, mean);
      const wires = grp.reduce((sm, x) => sm + x.w, 0);
      const S = { id: stList.length, fi: g.fi, p: st.p, brg: st.phi,
                  wires, tks: [], subJ: [], boxSub: new Map() };
      for (const x of grp) S.tks.push(x.tk);
      // shallow fan tree (spec D3): merges with >3 wires split into moat
      // sub-junctions by box bearing — taps leave radially OUTWARD and no
      // dot sees more than ~3 legs (skeptic R7). boxSub keyed by role:box
      // (a box can both feed and receive through this station).
      if (wires > 3) {
        const feed = new Map();   // role:ix -> ix (dedup, stable)
        for (const x of grp)
          for (const ix of (feedOf.get(x.tk) || {out:[],in:[]})[x.role ? "in" : "out"])
            feed.set(x.role + ":" + ix, ix);
        const fb = [...feed.keys()].map(key => feed.get(key))
          .sort((a, b) => brgOf(g.fi, [fpos[a*3], 0, fpos[a*3+2]]) -
                        brgOf(g.fi, [fpos[b*3], 0, fpos[b*3+2]]));
        const nSub = Math.min(6, Math.max(2, Math.ceil(wires / 4))), per = Math.ceil(fb.length / nSub);
        const R = (ringOut.get(g.fi) || sphR(g.fi)) + SUBJ_R;
        // subJ bearings are FORCED APART around the station bearing (0.9
        // rad steps, half-shifted so no dot sits ON the bearing): member
        // boxes still assign by sorted bearing, but the dots themselves
        // never sit collinear with the tangent slot axis (skeptic B1) —
        // and the Y ladder runs DOWNWARD below the swarm plane (station
        // holds +Y): in-plane gaps foreshortened by the d2 camera split on
        // the vertical axis instead (skeptic R3 real-pairs)
        for (let si = 0; si < nSub; si++) {
          const mem = fb.slice(si * per, (si + 1) * per);
          if (!mem.length) continue;
          const sb = S.brg + (si - (nSub - 1) / 2) * 0.9 + 0.45;
          const sp = [pos[g.fi*3] + Math.cos(sb) * R,
                      pos[g.fi*3+1] - 4 - Math.min(20, 5 * si),
                      pos[g.fi*3+2] + Math.sin(sb) * R];
          for (const ix of mem) {
            // a box in both roles maps by whichever role the wire uses
            S.boxSub.set("0:" + ix, sp);
            S.boxSub.set("1:" + ix, sp);
          }
          S.subJ.push(sp);
        }
      }
      for (const x of grp) {
        let m = stations.get(x.tk); if (!m) stations.set(x.tk, m = {});
        m[x.role] = S;
      }
      stList.push(S);
    }
  // (moat relaxation runs after Jof placement below — stations stay fixed,
  // sub-junctions and Jof dots rotate on their rings)
  const inB = new Map(), dirB = new Map();
  for (let i = 0, p = 0; i < eidx.length; i += 2, p++) {
    const a = eidx[i], b = eidx[i+1];
    if (!wireTier[p] || trunked.has(fnMeta[a].file + ">" + fnMeta[b].file)) continue;
    inB.set(b, (inB.get(b) || 0) + 1);
    const dx = fpos[b*3] - fpos[a*3], dy = fpos[b*3+1] - fpos[a*3+1],
          dz = fpos[b*3+2] - fpos[a*3+2];
    const l = Math.hypot(dx, dy, dz) || 1;
    const d = dirB.get(b) || [0, 0, 0];
    d[0] += dx / l; d[1] += dy / l; d[2] += dz / l;
    dirB.set(b, d);
  }
  const Jof = new Map(), JofR = new Map();
  for (const [b, n] of inB) if (n >= 2) {
    // moat anchor: radially OUTWARD past the outermost occupied radius
    // (spec D4) — the old 14-off-box dot sat inside the swarm band
    const fi = fnMeta[b].file;
    const ox = fpos[b*3] - pos[fi*3], oz = fpos[b*3+2] - pos[fi*3+2];
    const ol = Math.hypot(ox, oz) || 1;
    const L = Math.max(SUBJ_R, (ringOut.get(fi) || sphR(fi)) - ol + SUBJ_R);
    Jof.set(b, [fpos[b*3] + ox / ol * L, pos[fi*3+1], fpos[b*3+2] + oz / ol * L]);
    JofR.set(b, L);
  }
  // final moat relaxation: rotate sub-junctions + Jof dots away from ANY
  // same-file dot within 10 wu and any box center within 16 wu — 8 bounded
  // rounds of clamped tangential rotation, deterministic scan order.
  {
    const moat = [];
    for (const S of stList)
      for (const sp of S.subJ)
        moat.push({ fi: S.fi, p: sp, r: (ringOut.get(S.fi) || sphR(S.fi)) + SUBJ_R });
    for (const [b, J] of Jof)
      moat.push({ fi: fnMeta[b].file, p: J, r: JofR.get(b) });
    const sgn = (a, b) => ((a - b + Math.PI) % (2*Math.PI) + 2*Math.PI) % (2*Math.PI) - Math.PI >= 0 ? 1 : -1;
    for (let round = 0; round < 8; round++)
      for (const md of moat) {
        const brg = brgOf(md.fi, md.p);
        let rot = 0;
        for (const S of stList) {
          if (S.fi !== md.fi) continue;
          for (const q of [S.p, ...S.subJ]) {
            if (q === md.p) continue;
            if (Math.hypot(md.p[0]-q[0], md.p[1]-q[1], md.p[2]-q[2]) < 10)
              rot += sgn(brg, brgOf(md.fi, q)) * 0.10;
          }
        }
        for (const om of moat) {
          if (om === md || om.fi !== md.fi) continue;
          if (Math.hypot(md.p[0]-om.p[0], md.p[1]-om.p[1], md.p[2]-om.p[2]) < 10)
            rot += sgn(brg, brgOf(md.fi, om.p)) * 0.10;
        }
        // cross-file moat dots: adjacent files' rings can intersect (a vfx
        // subJ and a gm subJ sat 22 wu apart, 2.4 wu depth gap, 5.8px on
        // screen at d2) — rotate away from foreign dots too, gentler weight
        for (const om of moat) {
          if (om === md || om.fi === md.fi) continue;
          if (Math.hypot(md.p[0]-om.p[0], md.p[1]-om.p[1], md.p[2]-om.p[2]) < 24)
            rot += sgn(brg, brgOf(md.fi, om.p)) * 0.05;
        }
        for (const ix of allBoxes) {
          const q = [fpos[ix*3], fpos[ix*3+1], fpos[ix*3+2]];
          if (Math.hypot(md.p[0]-q[0], md.p[1]-q[1], md.p[2]-q[2]) < 16)
            rot += sgn(brg, brgOf(md.fi, q)) * 0.06;
        }
        rot = Math.max(-0.15, Math.min(0.15, rot));
        if (rot) {
          const nb = brg + rot;
          md.p[0] = pos[md.fi*3] + Math.cos(nb) * md.r;
          md.p[2] = pos[md.fi*3+2] + Math.sin(nb) * md.r;
        }
      }
  }
  // shared arc emitter: 8 quadratic segments (16 verts — the harness
  // counts wires as verts/16), optional arrowhead at the end tangent
  const emitArc = (T, ax, ay, az, bx, by, bz,
                   ar, ag, ab, br, bg, bb, phase, liftFrac, arrow, meta) => {
    if (meta) T.meta.push(meta);   // 1 meta entry per arc (8 segments each)
    const dist = Math.hypot(bx-ax, by-ay, bz-az) || 1;
    const lift = liftFrac * dist;   // ALWAYS +Y: no sign hack, no -Y dives
    const mx = (ax+bx)/2, my = (ay+by)/2 + lift, mz = (az+bz)/2;
    let px = ax, py = ay, pz = az, pd = 0;
    for (let s = 1; s <= FS; s++) {
      const t = s / FS, u = 1 - t;
      const x = u*u*ax + 2*u*t*mx + t*t*bx;
      const y = u*u*ay + 2*u*t*my + t*t*by;
      const z = u*u*az + 2*u*t*mz + t*t*bz;
      const dd = pd + Math.hypot(x-px, y-py, z-pz);
      T.ep.push(px, py, pz, x, y, z);
      T.ec.push(ar, ag, ab, br, bg, bb);
      T.ed.push(pd + phase, dd + phase);
      if (s === FS && arrow) {
        // delivery arrow sits EXACTLY at the arc end with the end tangent
        // (t=1 derivative 2(B-M)) — tips must land on the delivery
        // geometry, not one segment short of it (skeptic R4: 1.9px)
        const axp = bx - mx, ayp = by - my, azp = bz - mz;
        const al = Math.hypot(axp, ayp, azp) || 1;
        aPos.push(bx, by, bz);
        aDir.push(axp/al, ayp/al, azp/al);
        aCol.push(br, bg, bb);
        aBox.push(bx, by, bz);
      }
      px = x; py = y; pz = z; pd = dd;
    }
  };
  // file-pair trunks: surface-to-surface arc between each pair's fn-box
  // centroids, colored by the DESTINATION file (the bus feeds that file)
  const fnCen = new Map(), fnCnt = new Map();
  for (let i = 0; i < fnMeta.length; i++) {
    const fi = fnMeta[i].file;
    const c = fnCen.get(fi) || [0, 0, 0];
    c[0] += fpos[i*3]; c[1] += fpos[i*3+1]; c[2] += fpos[i*3+2];
    fnCen.set(fi, c);
    fnCnt.set(fi, (fnCnt.get(fi) || 0) + 1);
  }
  const surf = (fi, c) => {
    const dx = c[0] - pos[fi*3], dy = c[1] - pos[fi*3+1], dz = c[2] - pos[fi*3+2];
    const l = Math.hypot(dx, dy, dz) || 1;
    const r = sphR(fi);
    return [pos[fi*3] + dx / l * r, pos[fi*3+1] + dy / l * r, pos[fi*3+2] + dz / l * r];
  };
  const trunkEnds = new Map();  // reroute junctions: {e, x, c, m: trunk meta}
  const busSegs = [];   // {a:[x,y,z], b:[x,y,z], col:[r,g,b], k} conduit pieces
  const busJunc = [];   // junction bollards: {p:[x,y,z], c:[r,g,b]}
  trunkMetaMap = new Map();
  const jcons = [];     // junction constraints: {p, kind:"box", b, r}
  const trunkGeom = []; // deferred trunk emission (after junction separation)
  fnTrunkN = 0;
  fnTrunkW = 0;
  fnJstubN = 0;
  fnQuietTrunkN = 0;
  fnQuietTrunkW = 0;
  fnLegN = 0;
  fnJclip = 0;
  const corridorTier = new Map();   // "srcId>tgtId" -> 0..2 (first-seen order)
  const corridorOrd = new Map();    // "srcId>tgtId" -> trunk count so far
  // fan trunk termini out of shared stations: corridors leaving/arriving at
  // one bollard offset along the station tangent (ranked by the OTHER end's
  // bearing) so coincident first/last conduit segments separate instead of
  // stacking at 0 wu; trunks still read as leaving the dot. Sector stations
  // of one bollard CLUSTER are grouped by proximity (60 wu) — object
  // identity split the visual fan and left whole clusters unranked.
  const stTermR = new Map();  // "k:end" -> fan rank (0..n-1, by other-end bearing)
  const claimed = new Set();  // stations already ranked via an earlier cluster rep
  for (const S of stList) {
    if (claimed.has(S)) continue;
    const terms = [];
    for (const S2 of stList) {
      if (Math.hypot(S2.p[0]-S.p[0], S2.p[1]-S.p[1], S2.p[2]-S.p[2]) > 60) continue;
      claimed.add(S2);
      for (const k of trunked) {
        const stB = stations.get(k);
        if (!stB) continue;
        if (stB[0] === S2) terms.push({ k, end: 0, other: stB[1].p });
        if (stB[1] === S2) terms.push({ k, end: 1, other: stB[0].p });
      }
    }
    if (terms.length < 2) continue;
    terms.sort((a, b) => brgOf(S.fi, a.other) - brgOf(S.fi, b.other));
    // ZERO-GAP ATTACHMENT (user sighting #12): trunk termini land exactly
    // ON the station bollard (S.p) — no tangent-line fan slots. Ranking is
    // retained only as fanR, which terraces APEX HEIGHTS so stacked trunks
    // still read over/under instead of braiding at the shared dot.
    terms.forEach((t, r) => { stTermR.set(t.k + ":" + t.end, r); });
  }
  for (const k of trunked) {
    const parts = k.split(">");
    const sf = +parts[0], tf = +parts[1];
    const c0 = fnCen.get(sf), c1 = fnCen.get(tf);
    if (!c0 || !c1) continue;
    const stBoth = stations.get(k);
    const stS = stBoth && stBoth[0], stT = stBoth && stBoth[1];
    if (!stS || !stT) continue;
    // entry/exit = the two files' stations (shared per sector — ONE bollard
    // per sector, not one per pair). Delivery legs run station/subJ -> box
    // per wire; single-destination trunks need no special stop.
    // ZERO-GAP (user sighting #12): trunk termini land exactly ON the
    // station bollard — the vertex IS the marker position, no fan slots.
    const p0 = stS.p, p1 = stT.p;
    // trunk color = DESTINATION FILE's cluster color (colArr is per-file;
    // fcol is per-fn-box — indexing it by file reads garbage => black tubes)
    const tc = [colArr[tf*3], colArr[tf*3+1], colArr[tf*3+2]];
    const tmeta = { kind: "trunk", k, sf, tf, mates: [] };
    trunkMetaMap.set(k, tmeta);
    const ck = stS.id + ">" + stT.id;
    if (!corridorTier.has(ck)) corridorTier.set(ck, corridorTier.size % 3);
    // depth-banding (skeptic B5/R5): trunks of ONE corridor fly at distinct
    // heights — corridor seed + per-trunk ordinal over 6 tiers
    const ord = corridorOrd.get(ck) || 0; corridorOrd.set(ck, ord + 1);
    // apex terrace (declutter R1, sighting-#12 revision): sibling corridors
    // sharing a bollard descend in fan-rank order via APEX height only —
    // landings stay exactly on the dot, so ranks read over/under instead
    // of braiding at a shared landing slot
    const fanR = (stTermR.get(k + ":0") || 0) + (stTermR.get(k + ":1") || 0);
    trunkGeom.push({ p0, p1, tc, tmeta, tier: (corridorTier.get(ck) + ord) % 6, fanR });
    trunkEnds.set(k, { e: p0, x: p1, c: tc, m: tmeta });
  }
  // station bollards: a DISJOINT ivory family (S<=0.12, L>=0.84) — cluster
  // hues fill the wheel densely (golden-ratio over 29 clusters), so hue
  // disjointness is impossible; saturation+lightness distance is provable:
  // fn boxes sit at S=0.72, L<=0.70. Bollards are the only near-white
  // elements, so a merge dot is identifiable BY COLOR ALONE at any zoom
  // (skeptic round-3 pre-ruling 3.ii; assert after the instancing below).
  const BOL_COL = [ new THREE.Color().setHSL(0.10, 0.12, 0.90),   // station
                    new THREE.Color().setHSL(0.12, 0.07, 0.87),   // sub-junction
                    new THREE.Color().setHSL(0.12, 0.05, 0.84) ]; // Jof
  for (const S of stList) {
    busJunc.push({ p: S.p, c: [BOL_COL[0].r, BOL_COL[0].g, BOL_COL[0].b], k: 1, of: S.fi, st: 1,
      nl: S.subJ.length, hd: 1,
      info: { kind: "station", fi: S.fi, wires: S.wires, trks: S.tks.length } });
    for (let li = 0; li < S.subJ.length; li++) {
      const sp = S.subJ[li];
      busJunc.push({ p: sp, c: [BOL_COL[1].r, BOL_COL[1].g, BOL_COL[1].b], k: 0.72, of: S.fi, st: 1,
        nl: 1, lk: "L|" + S.fi + "|" + S.id + "|" + li,
        info: { kind: "sub", fi: S.fi, wires: S.wires } });
      // legs land on a tangent line 5 wu BELOW the station — the empty
      // lane under the horizontal trunk fan (trunks bow +Y from termini
      // on the mid line), slotted wide of them: >=9 horizontal + >=5
      // vertical clearance by construction
      const tx = -Math.sin(S.brg), tz = Math.cos(S.brg);
      const off = (li - (S.subJ.length - 1) / 2) * 18;
      const ep = [S.p[0] + tx * off, S.p[1] - 5, S.p[2] + tz * off];
      const dist = Math.hypot(ep[0]-sp[0], ep[1]-sp[1], ep[2]-sp[2]) || 1;
      const lift = 0.24 * dist;   // apex = 0.12*dist: >= 0.10*dist law with margin
      // (tilted chords + 8-seg sampling eat a thin 0.105 one)
      const mx = (sp[0]+ep[0])/2, my = (sp[1]+ep[1])/2 + lift, mz = (sp[2]+ep[2])/2;
      let lx = sp[0], ly = sp[1], lz = sp[2];
      for (let s = 1; s <= FS; s++) {
        const t = s / FS, u = 1 - t;
        const x = u*u*sp[0] + 2*u*t*mx + t*t*ep[0];
        const y = u*u*sp[1] + 2*u*t*my + t*t*ep[1];
        const z = u*u*sp[2] + 2*u*t*mz + t*t*ep[2];
        busSegs.push({ a: [lx, ly, lz], b: [x, y, z],
                       col: [BOL_COL[1].r, BOL_COL[1].g, BOL_COL[1].b],
                       k: "L|" + S.fi + "|" + S.id + "|" + li, rf: 0.55 });
        lx = x; ly = y; lz = z;
      }
      // DRAWS-TO-ITS-ANCHOR (user sighting #8 ruling): the leg's terminus
      // IS the station bollard — the tangent-offset ep was a visual seam
      // the leads-home clause legalized as float (up to 262px at close
      // zoom). One closing segment lands the ink on the dot itself; it
      // shares k so the whole chain culls/serves atomically and picks as
      // a jleg. The junction end already lands on its subJ bollard (sp).
      busSegs.push({ a: [lx, ly, lz], b: [S.p[0], S.p[1], S.p[2]],
                     col: [BOL_COL[1].r, BOL_COL[1].g, BOL_COL[1].b],
                     k: "L|" + S.fi + "|" + S.id + "|" + li, rf: 0.55 });
      fnLegN++;
    }
  }
  // quiet-tier trunks: >= QUIET_TRUNK_MIN neighbor↔neighbor wires between
  // the same file pair collapse into ONE background arc between the
  // surfaced fn-centroid points — the bright-trunk law mirrored down a
  // tier. No junction-separation pass: background ink, build cost flat.
  const qTrunkGeo = new Map();   // tk -> {e, x, c, tm} for member taps
  for (const k of qTrunked) {
    const parts = k.split(">");
    const sf = +parts[0], tf = +parts[1];
    const c0 = fnCen.get(sf), c1 = fnCen.get(tf);
    if (!c0 || !c1) continue;
    const p0 = surf(sf, [c0[0]/fnCnt.get(sf), c0[1]/fnCnt.get(sf), c0[2]/fnCnt.get(sf)]);
    const p1 = surf(tf, [c1[0]/fnCnt.get(tf), c1[1]/fnCnt.get(tf), c1[2]/fnCnt.get(tf)]);
    const tc = [colArr[tf*3], colArr[tf*3+1], colArr[tf*3+2]];
    const tm = { kind: "trunk", k, sf, tf, mates: [] };
    trunkMetaMap.set(k, tm);
    emitArc(tierQ, p0[0], p0[1], p0[2], p1[0], p1[1], p1[2],
            tc[0], tc[1], tc[2], tc[0], tc[1], tc[2], 0,
            QUIET_LIFT_FRAC, false, tm);
    fnQuietTrunkN++;
    qTrunkGeo.set(k, { e: p0, x: p1, c: tc, tm });
  }
  // junction separation: 3 deterministic depenetration rounds — box-anchored
  // junctions (fn stops + Jof moat dots) closer than 6 spread apart
  // tangentially around their fn, then re-anchor at their own radius
  for (const [b, J] of Jof) {
    // bollard instancing happens AFTER the separation rounds + merge pass
    // (this push only registers the dot for the rounds; J mutates in place)
    jcons.push({ p: J, kind: "box", b, r: JofR.get(b) });
  }
  for (let round = 0; round < 3; round++) {
    for (let i = 0; i < jcons.length; i++) for (let j = i + 1; j < jcons.length; j++) {
      const jcA = jcons[i], jcB = jcons[j];
      const A = jcA.p, B = jcB.p;
      const dx = B[0] - A[0], dy = B[1] - A[1], dz = B[2] - A[2];
      const dd = Math.hypot(dx, dy, dz);
      if (dd > 6 || dd < 1e-6) continue;
      const push = 6 - dd;
      const slideBox = (jc, away) => {
        // slide a box-anchored junction around its fn ring away from `away` —
        // radial push loses to re-anchor, tangential displacement survives it
        const fb = [fpos[jc.b*3], fpos[jc.b*3+1], fpos[jc.b*3+2]];
        const P = jc.p;
        const rl = Math.hypot(P[0]-fb[0], P[1]-fb[1], P[2]-fb[2]) || 1;
        const rx = (P[0]-fb[0]) / rl, ry = (P[1]-fb[1]) / rl, rz = (P[2]-fb[2]) / rl;
        const sx = away[0]-P[0], sy = away[1]-P[1], sz = away[2]-P[2];
        const radial = sx*rx + sy*ry + sz*rz;
        const tx = sx - radial*rx, ty = sy - radial*ry, tz = sz - radial*rz;
        const tl = Math.hypot(tx, ty, tz);
        if (tl < 1e-6) return;   // head-on: next round settles it
        P[0] += tx / tl * push; P[1] += ty / tl * push; P[2] += tz / tl * push;
      };
      if (jcA.b === jcB.b) slideBox(jcB, A);   // same fn: one ring, later yields
      else { slideBox(jcB, A); slideBox(jcA, B); }   // different fns: both yield
    }
    for (const jc of jcons) {
      const fb = [fpos[jc.b*3], fpos[jc.b*3+1], fpos[jc.b*3+2]];
      const dx = jc.p[0] - fb[0], dy = jc.p[1] - fb[1], dz = jc.p[2] - fb[2];
      const l = Math.hypot(dx, dy, dz) || 1;
      const r = jc.r || 14;
      jc.p[0] = fb[0] + dx / l * r;
      jc.p[1] = fb[1] + dy / l * r;
      jc.p[2] = fb[2] + dz / l * r;
    }
  }
  // Jof merge pass + Y-ladder (skeptic R3 real-pairs): same-depth moat dots
  // within 22 wu collapse onto ONE shared reroute point (bollards are file-
  // neutral ivory now, so one dot serving several stubs tells no lie), and
  // the survivors get a deterministic +7wu-per-index Y ladder so in-plane
  // separations foreshortened by the d2 camera still split on screen.
  {
    const jofs = [...Jof.entries()].sort((A, B) => A[0] - B[0]);
    for (let i = 0; i < jofs.length; i++) {
      if (!Jof.has(jofs[i][0])) continue;
      for (let j = i + 1; j < jofs.length; j++) {
        if (!Jof.has(jofs[j][0]) || jofs[j][1] === jofs[i][1]) continue;
        const A = jofs[i][1], B = jofs[j][1];
        if (Math.hypot(A[0]-B[0], A[1]-B[1], A[2]-B[2]) < 26) {
          A[0] = (A[0]+B[0])/2; A[1] = (A[1]+B[1])/2; A[2] = (A[2]+B[2])/2;
          Jof.set(jofs[j][0], A);   // share the array: fans + stubs follow
        }
      }
    }
    // snap pass: a Jof dot within 18 wu of a station/subJ tree dot SHARES
    // that dot (sub|jof pairs at 12 wu projected 4.8px at d2 — two ivory
    // bus dots that close read as one smear). Tree points are skipped by
    // the Y-ladder and the instancing loop — they are already bollards.
    const treePts = [];
    for (const S of stList) { treePts.push(S.p); for (const q of S.subJ) treePts.push(q); }
    const isTree = J2 => treePts.some(q => q === J2);
    for (const [b, J] of Jof) {
      if (isTree(J)) continue;
      for (const S of stList) {
        const near = [S.p, ...S.subJ].find(q =>
          Math.hypot(q[0]-J[0], q[1]-J[1], q[2]-J[2]) < 18);
        if (near) { Jof.set(b, near); break; }
      }
    }
    const seen = new Set(); let ord = 0;
    for (const [b, J] of Jof) {   // map iteration: merged points share refs
      if (isTree(J)) continue;
      const kk = J[0].toFixed(2) + "," + J[1].toFixed(2) + "," + J[2].toFixed(2);
      if (!seen.has(kk)) { seen.add(kk); J[1] += 7 * (ord % 3); ord++; }
    }
    // instancing: one bollard per DISTINCT merged point
    const done = new Set();
    for (const [b, J] of Jof) {
      if (isTree(J)) continue;
      const kk = J[0].toFixed(2) + "," + J[1].toFixed(2) + "," + J[2].toFixed(2);
      if (done.has(kk)) continue;
      done.add(kk);
      busJunc.push({ p: [J[0], J[1], J[2]], c: [BOL_COL[2].r, BOL_COL[2].g, BOL_COL[2].b], k: 0.72, of: fnMeta[b].file, st: 0,
        info: { kind: "jof", fi: fnMeta[b].file } });
    }
  }
  // obstacle-aware conduit lift (spec D5): raise the control point so the
  // arc clears foreign swarm hulls that straddle the chord. Closed form:
  // height(t) = base(t) + 2t(1-t)*lift >= obstacle top over the crossing
  // interval. Cap keeps masts sane; capped trunks are counted in fnJclip.
  const litFiles = [];
  {
    const seen = new Set();
    for (const e of visEdges) {
      if (!seen.has(e[0])) { seen.add(e[0]); litFiles.push(e[0]); }
      if (!seen.has(e[2])) { seen.add(e[2]); litFiles.push(e[2]); }
    }
  }
  const obsLift = (p0, p1, sf, tf) => {
    const dx = p1[0] - p0[0], dz = p1[2] - p0[2];
    const A2 = dx*dx + dz*dz;
    if (A2 < 1e-9) return 0;
    let need = 0;
    for (const fi of litFiles) {
      if (fi === sf || fi === tf) continue;
      const sr = sphR(fi), ro = ringOut.get(fi);
      const hull = ro !== undefined ? ro : sr;
      for (const [R, top] of [[sr, pos[fi*3+1] + sr + 3], [hull + 5, pos[fi*3+1] + 6]]) {
        const fx = p0[0] - pos[fi*3], fz = p0[2] - pos[fi*3+2];
        const B = 2*(fx*dx + fz*dz), C = fx*fx + fz*fz - R*R;
        const disc = B*B - 4*A2*C;
        if (disc <= 0) continue;
        const sq = Math.sqrt(disc);
        const t1 = Math.max(0.12, (-B - sq) / (2*A2)), t2 = Math.min(0.88, (-B + sq) / (2*A2));
        if (t2 <= t1) continue;
        for (const t of [t1, t2, (t1 + t2) / 2]) {
          const base = p0[1] + (p1[1] - p0[1]) * t;
          const n = (top + 4 - base) / (2*t*(1 - t));
          if (n > need) need = n;
      }
    }
    }
    return need;
  };
  // phase 2: trunk geometry, emitted AFTER separation so arcs, conduits and
  // the shared reroute ends all read the final junction positions
  for (const g of trunkGeom) {
    const p0 = g.p0, p1 = g.p1, tc = g.tc, tmeta = g.tmeta;
    // 1px lines reads as nothing. Tier by corridor (same station pair =>
    // same corridor), raised by the obstacle law, then TERRACED down by fan
    // rank (fanR, declutter R1) so siblings sharing a bollard read as a
    // staircase instead of interleaved hills; apex stays >= 0.11*dist and
    // ALWAYS +Y (conduit lane law). The guide arc rides the SAME lift as
    // the tube — one path, two inks.
    const dist = Math.hypot(p1[0]-p0[0], p1[1]-p0[1], p1[2]-p0[2]) || 1;
    const Ltier = (CONDUIT_LIFT_BASE + 0.03 * g.tier) * dist;
    const Lob = obsLift(p0, p1, tmeta.sf, tmeta.tf);
    const cap = 0.70 * dist;   // 0.80 bow cleared boxes but +2..6 TT at cu fan
    const lift = Math.min(cap, Math.max(0.11 * dist,
                    Math.max(Ltier, Lob) - 8 * (g.fanR || 0)));
    emitArc(tierB, p0[0], p0[1], p0[2], p1[0], p1[1], p1[2],
            tc[0], tc[1], tc[2], tc[0], tc[1], tc[2], 0, lift / dist, false,
            tmeta);
    fnTrunkN++;
    const qx = (p0[0]+p1[0])/2, qy = (p0[1]+p1[1])/2 + lift, qz = (p0[2]+p1[2])/2;
    let bx2 = p0[0], by2 = p0[1], bz2 = p0[2];
    for (let s = 1; s <= FS; s++) {
      const t = s / FS, u = 1 - t;
      const x = u*u*p0[0] + 2*u*t*qx + t*t*p1[0];
      const y = u*u*p0[1] + 2*u*t*qy + t*t*p1[1];
      const z = u*u*p0[2] + 2*u*t*qz + t*t*p1[2];
      busSegs.push({ a: [bx2, by2, bz2], b: [x, y, z], col: tc, k: tmeta.k });
      bx2 = x; by2 = y; bz2 = z;
    }
  }
  const Jstub = new Set();   // fns already carrying a junction delivery stub
  const tgtArrow = new Set();   // delivery arrows: ONE per target box (R4)
  // ONE delivery chevron per RENDERED box, all paths. Key must be the
  // RENDERED position, not the original box index: collapsed members remap
  // to their file's aggregate box, and keying on `b` stacked five chevrons
  // on one projected point (pair-engineer census, round 5b)
  const boxKey = b => (fnMeta[b].agg && !fnMeta[b].count) ? "agg" + fnMeta[b].file : "b" + b;
  const boxArrow = new Set();
  for (let i = 0, p = 0; i < eidx.length; i += 2, p++) {
    const a = eidx[i], b = eidx[i+1];
    const T = wireTier[p] ? tierB : tierQ;
    // collapsed members (scale-0) land on their file's VISIBLE aggregate
    // box — deliveries must read at a rendered box, not open air
    const ap = fnMeta[a].agg && !fnMeta[a].count ? aggP.get(fnMeta[a].file) : null;
    const bp = fnMeta[b].agg && !fnMeta[b].count ? aggP.get(fnMeta[b].file) : null;
    const ax = ap ? ap[0] : fpos[a*3], ay = ap ? ap[1] : fpos[a*3+1], az = ap ? ap[2] : fpos[a*3+2];
    const bx = bp ? bp[0] : fpos[b*3], by = bp ? bp[1] : fpos[b*3+1], bz = bp ? bp[2] : fpos[b*3+2];
    cA.setRGB(fcol[a*3], fcol[a*3+1], fcol[a*3+2]);
    cB.setRGB(fcol[b*3], fcol[b*3+1], fcol[b*3+2]);
    const phase = ((i + 1) * 2654435761 >>> 3) % 911 / 911 * 13;
    const ln = wireRows[p] ? wireRows[p][4] : -1;   // source line for the tip
    let done = false;
    if (T === tierB) {
      const tk = fnMeta[a].file + ">" + fnMeta[b].file;
      const ends = trunked.has(tk) ? trunkEnds.get(tk) : null;
      if (ends) {
        // Blueprint reroute node: the wire leaves its fn, merges at its
        // sector sub-junction (or the station), rides the trunk, and
        // arrives via the exit-side fan — every wire of the pair at the
        // same end. Taps dim to the trunk hue (ink#1) and carry no arrow
        // (ink#2 — direction lives on the ONE trunk tip).
        done = true;
        fnTrunkW++;
        const wmeta = { kind: "wire", a, b, ln, tk };
        const dc = [ends.c[0]*0.38, ends.c[1]*0.38, ends.c[2]*0.38];
        const stBoth = stations.get(tk);
        ends.m.mates.push(wmeta);
        // ZERO-GAP ATTACHMENT (user sighting #12): rider ink lands exactly
        // ON the merge point — the sub-junction/station dot. The old 14wu
        // moat (gap handoff) left visible gaps at every junction; the dot
        // itself is the merge, so the arc ends on it.
        const e0 = stBoth && stBoth[0] ? (stBoth[0].boxSub.get("0:" + a) || stBoth[0].p) : ends.e;
        emitArc(T, ax, ay, az, e0[0], e0[1], e0[2],
                dc[0], dc[1], dc[2], dc[0], dc[1], dc[2], phase, 0.16, false, wmeta);
        // exit leg: delivery fan leaves from the target box's sub-junction
        // (or the in-station directly on quiet merges), also landing exactly
        // on the dot. Emitted BOX-FIRST (like the entry tap) so both ramp
        // kinds read as leaving the box and joining the merge.
        const e1 = stBoth && stBoth[1] ? (stBoth[1].boxSub.get("1:" + b) || stBoth[1].p) : ends.x;
        emitArc(T, bx, by, bz, e1[0], e1[1], e1[2],
                cB.r, cB.g, cB.b, dc[0], dc[1], dc[2], phase + 2.5, 0.16, false, wmeta);
        // DELIVERY chevron (skeptic r4 #2): one per trunked TARGET BOX, at
        // its face aimed inward — not at the station terminus, where the
        // 11px merge disk swallowed every trunk-tip arrow (measured 0
        // amber px under the disk)
        if (!boxArrow.has(boxKey(b))) {
          boxArrow.add(boxKey(b));
          let ddx = bx-e1[0], ddy = by-e1[1], ddz = bz-e1[2];
          const dl = Math.hypot(ddx, ddy, ddz) || 1;
          ddx /= dl; ddy /= dl; ddz /= dl;
          aPos.push(bx - ddx*3.5, by - ddy*3.5, bz - ddz*3.5);
          aDir.push(ddx, ddy, ddz);
          aCol.push(cB.r, cB.g, cB.b);
          aBox.push(bx, by, bz);
        }
      } else {
        const J = Jof.get(b);
        if (J) {
          // reroute junction: wires merge AT the moat junction, then ONE
          // shared stub delivers into the fn box — the junction must not
          // be a dead end in open space ("supposed to go into
          // a cross-system call). Fan dims to the target hue (ink#1).
          const d6 = [cB.r*0.38, cB.g*0.38, cB.b*0.38];
          emitArc(T, ax, ay, az, J[0], J[1], J[2],
                  d6[0], d6[1], d6[2], d6[0], d6[1], d6[2], phase, 0.16, false,
                  { kind: "wire", a, b, ln });
          if (!Jstub.has(b)) {
            Jstub.add(b);
            fnJstubN++;
            const jArr = !boxArrow.has(boxKey(b)); boxArrow.add(boxKey(b));
            emitArc(T, J[0], J[1], J[2], bx, by, bz,
                    cB.r, cB.g, cB.b, cB.r, cB.g, cB.b, phase, 0.16, jArr,
                    { kind: "wire", a, b, ln });
          }
          done = true;
        }
      }
    }
    if (!done && T === tierQ) {
      const qtk = fnMeta[a].file + ">" + fnMeta[b].file;
      const qt = qTrunkGeo.get(qtk);
      if (qt) {
        // absorbed by the quiet trunk: entry tap fn -> trunk head, exit
        // tap trunk tail -> fn (bright-trunk tap pattern, minus arrows —
        // the arrow overlay is tier-blind and quiet ink stays silent)
        done = true;
        fnQuietTrunkW++;
        const wmeta = { kind: "wire", a, b, ln, tk: qtk };
        qt.tm.mates.push(wmeta);
        emitArc(T, ax, ay, az, qt.e[0], qt.e[1], qt.e[2],
                cA.r, cA.g, cA.b, qt.c[0], qt.c[1], qt.c[2],
                phase, 0.10, false, wmeta);
        emitArc(T, qt.x[0], qt.x[1], qt.x[2], bx, by, bz,
                qt.c[0], qt.c[1], qt.c[2], cB.r, cB.g, cB.b,
                phase + 2.5, 0.10, false, wmeta);
      }
    }
    if (!done) {
      // direct wire: one delivery arrow per TARGET box — parallel wires into
      // the same fn share the direction cue (skeptic R4: 31 -> ~18 arrows)
      const arr = T === tierB && !boxArrow.has(boxKey(b));
      if (T === tierB) boxArrow.add(boxKey(b));
      emitArc(T, ax, ay, az, bx, by, bz,
              cA.r, cA.g, cA.b, cB.r, cB.g, cB.b, phase,
              0.08 + 0.10 * (((i + 1) * 2654435761 >>> 0) % 97) / 97,
              arr, { kind: "wire", a, b, ln });
    }
  }
  const makeWires = (T, op) => {
    // fat lines: WebGL ignores linewidth on classic LineSegments — Line2
    // renders real screen-space width (default wires read at 2px)
    const g = new LineSegmentsGeometry();
    g.setPositions(new Float32Array(T.ep));
    g.setColors(new Float32Array(T.ec));
    const m = new LineMaterial({
      vertexColors: true, transparent: true, opacity: op,
      linewidth: 2, worldUnits: false, dashed: true,
      dashSize: 7, gapSize: 4, blending: THREE.NormalBlending,
      depthWrite: false, alphaToCoverage: false });
    m.resolution.set(glW(), innerHeight);
    const ls = new LineSegments2(g, m);
    ls.computeLineDistances();
    ls.frustumCulled = false;
    scene.add(ls);
    return ls;
  };
  fnLines = makeWires(tierB, 0.75);
  if (tierQ.ep.length) fnQuiet = makeWires(tierQ, 0.16);
  fnLines.userData.meta = tierB.meta;   // wire pick descriptions
  if (fnQuiet) fnQuiet.userData.meta = tierQ.meta;
  if (busSegs.length) {
    const cyl = new THREE.CylinderGeometry(1, 1, 1, 6);
    fnBus = new THREE.InstancedMesh(cyl, new THREE.MeshBasicMaterial({
      transparent: true, opacity: 1.0 }), busSegs.length);
    const M = new THREE.Matrix4(), Q = new THREE.Quaternion(),
          V = new THREE.Vector3(), UP = new THREE.Vector3(0, 1, 0),
          S1 = new THREE.Vector3(), C = new THREE.Color();
    busSegs.forEach((s, k) => {
      const ax = new THREE.Vector3(...s.a), bx = new THREE.Vector3(...s.b);
      const d = bx.clone().sub(ax), len = d.length() || 1;
      Q.setFromUnitVectors(UP, d.normalize());
      V.copy(ax).addScaledVector(d, len / 2);
      const rr = 4 * (s.rf || 1);   // legs start at 0.55x — base radius
      S1.set(rr, len, rr);   // tick rescales to keep ~4px on screen
      M.compose(V, Q, S1);
      fnBus.setMatrixAt(k, M);
      fnBus.setColorAt(k, C.setRGB(s.col[0], s.col[1], s.col[2]));
    });
    fnBusRi = new Float32Array(busSegs.length).map((_, q) => 4 * (busSegs[q].rf || 1));
    if (fnBus.instanceColor) fnBus.instanceColor.needsUpdate = true;
    fnBus.frustumCulled = false;
    busPts = busSegs;
  busPtsMeta = busSegs.map(s => String(s.k).startsWith("L|")
    ? { kind: "jleg", fi: +String(s.k).split("|")[1], k: String(s.k) }
    : (trunkMetaMap.get(s.k) || null));
    scene.add(fnBus);
  }
  // probe surfaces (extend-only __dbg contract): the station map + the
  // worst junction->box clearance, so QA can assert open-air placement
  fnStationsArr = stList.map(S => ({ fi: S.fi, p: S.p, id: S.id,
    brg: S.brg, wires: S.wires, tks: S.tks.slice(), subJ: S.subJ.map(p => [p[0], p[1], p[2]]) }));
  fnJclearV = -1;
  if (busJunc.length) {
    let jmin = Infinity;
    for (const j of busJunc) for (let i = 0; i < fnMeta.length; i++) {
      const m = fnMeta[i];
      if (m.agg && !m.count) continue;
      const dd = Math.hypot(j.p[0]-m.p[0], j.p[1]-m.p[1], j.p[2]-m.p[2]);
      if (dd < jmin) jmin = dd;
    }
    if (isFinite(jmin)) fnJclearV = jmin;
  }
  // per-junction size factors (screen-constant bollard law in the tick)
  fnJDotOf = null; fnJDotSt = null; stationTks = null; fnBoxScale = null; arrowFile = null; _lod = null; fnJDotKey = null;
  juncPickInfo = busJunc.map(j => j.info || null);
  if (busJunc.length) {
    fnJDotOf = Int32Array.from(busJunc, j => j.of | 0);
    fnJDotSt = Uint8Array.from(busJunc, j => j.st | 0);
    fnJDotLegs = Int32Array.from(busJunc, j => j.nl | 0);
    fnJDotKey = busJunc.map(j => j.hd ? "T|" + (j.of | 0) : (j.lk || null));
  }
  stationTks = new Map();
  for (const S of stList) {
    let tks = stationTks.get(S.fi);
    if (!tks) stationTks.set(S.fi, tks = []);
    for (const tk of S.tks) tks.push(tk);
  }
  fnBoxScale = new Map();
  for (const m of fnMeta) {
    if (m.agg && !m.count) continue;
    const sc = m.count ? 6 : 4;
    if (sc > (fnBoxScale.get(m.file) || 0)) fnBoxScale.set(m.file, sc);
  }
  fnJDotK = null;
  if (busJunc.length)
    fnJDotK = Float32Array.from(busJunc, j => j.k || 1);
  if (busJunc.length) {
    // reroute bollards: the junction must be a THING — a dot where the fan
    // merges and where the bus delivers onto the fn box (Blueprint reroute)
    const jg = new THREE.SphereGeometry(3, 8, 6);
    fnJDot = new THREE.InstancedMesh(jg, new THREE.MeshBasicMaterial({
      transparent: true, opacity: 0.9, depthWrite: false }), busJunc.length);
    fnJDot.renderOrder = 3;   // junctions sit ON TOP of the wire tangle
    fnJDot.material.depthTest = false;  // occlusion may never fully hide a dot (pre-ruling 3.iii)
    const M = new THREE.Matrix4(), C = new THREE.Color();
    fnJDotPos = new Float32Array(busJunc.length * 3);
    fnJDotR = new Float32Array(busJunc.length).fill(2.6);
    busJunc.forEach((j, k) => {
      M.makeTranslation(j.p[0], j.p[1], j.p[2]);
      fnJDot.setMatrixAt(k, M);
      fnJDotPos[k*3] = j.p[0]; fnJDotPos[k*3+1] = j.p[1]; fnJDotPos[k*3+2] = j.p[2];
      fnJDot.setColorAt(k, C.setRGB(j.c[0], j.c[1], j.c[2]));
    });
    // disjointness assert (skeptic pre-ruling 3.ii): every bollard is
    // near-white (S<=0.15, L>=0.80) while every RENDERED fn box carries a
    // cluster color at S=0.72, L<=0.70 — bollards are identifiable by
    // color alone; a violation means the family drifted
    const hsl = { h: 0, s: 0, l: 0 };
    for (const j of busJunc) {
      C.setRGB(j.c[0], j.c[1], j.c[2]).getHSL(hsl);
      if (hsl.s > 0.15 || hsl.l < 0.80) console.error("[bus] bollard family drift", hsl);
    }
    fnJDot.instanceMatrix.needsUpdate = true;
    if (fnJDot.instanceColor) fnJDot.instanceColor.needsUpdate = true;
    fnJDot.frustumCulled = false;
    scene.add(fnJDot);
  }
  if (aPos.length) {
    // 2D SCREEN-SPACE CHEVRON (skeptic r4): 3D cones render as round dots
    // edge-on at 5px; a flat chevron billboarded to the camera with its
    // local +Y aimed along the wire's projected tangent reads as
    // DIRECTION at any camera. Amber family: S 0.92 / L 0.55 is disjoint
    // from the ivory bollards (S<=0.12, L>=0.84) AND the golden-ratio box
    // family (S=0.72, L<=0.70) on BOTH axes — arrows are the only warm
    // saturated mid-light element in the layer.
    // CHUNKY chevron: the r4 shape (arms 0.30 wide) painted only ~5px of
    // saturated amber inside its 12px vertex span at the USER's window —
    // the V read as a small dot (pair-engineer pixel census). Fat arms +
    // a short notch keep the V legible at any viewport
    const chev = new THREE.Shape();
    chev.moveTo(-0.72, -0.42); chev.lineTo(0, 0.62); chev.lineTo(0.72, -0.42);
    chev.lineTo(0.30, -0.42); chev.lineTo(0, 0.10); chev.lineTo(-0.30, -0.42);
    chev.closePath();
    const arrowGeo = new THREE.ShapeGeometry(chev);
    // toneMapped:false — the renderer's ACES tone mapping crushes a
    // mid-lightness amber into brown (measured: HSL(0.085,0.92,0.55)
    // painted ~(170,105,85)); raw color keeps the warm pop
    // fog:false too — scene fog blended the chevron toward the dark fog
    // color at hub distance (isolated-pixel census: brightest core only
    // (200,162,105), a ~0.6-opacity ghost)
    const arrowMat = new THREE.MeshBasicMaterial({ transparent: true,
      opacity: 1.0, depthWrite: false, side: THREE.DoubleSide,
      toneMapped: false, fog: false });
    fnArrows = new THREE.InstancedMesh(arrowGeo, arrowMat, aPos.length / 3);
    fnArrows.frustumCulled = false;
    fnArrows.renderOrder = 4;   // above tubes AND bollards: cone tips sit at
    fnArrows.material.depthTest = false;  // termini, half-inside tube volumes
    const C = new THREE.Color().setHSL(0.085, 0.92, 0.60);   // amber chevron
    for (let k = 0; k < aPos.length / 3; k++) fnArrows.setColorAt(k, C);
    fnArrowTang = new Float32Array(aDir);
    fnArrowBox = new Float32Array(aBox.length ? aBox : aPos);
    // round-5 LOD: owning FILE per chevron (nearest rendered box — aBox
    // may carry the remapped aggregate position)
    arrowFile = new Int32Array(aPos.length / 3);
    const rBoxes = [];
    for (const m of fnMeta) if (!(m.agg && !m.count)) rBoxes.push(m);
    for (let ai = 0; ai < arrowFile.length; ai++) {
      let best = -1, bd = Infinity;
      for (let bi = 0; bi < rBoxes.length; bi++) {
        const dd = Math.hypot(rBoxes[bi].p[0]-fnArrowBox[ai*3],
                              rBoxes[bi].p[1]-fnArrowBox[ai*3+1],
                              rBoxes[bi].p[2]-fnArrowBox[ai*3+2]);
        if (dd < bd) { bd = dd; best = bi; }
      }
      arrowFile[ai] = best >= 0 ? rBoxes[best].file : -1;
    }
    // matrices come from aimArrows() (billboard basis); build-time call so
    // the first painted frame is already correct
    aimArrows();
    fnArrows.instanceMatrix.needsUpdate = true;
    if (fnArrows.instanceColor) fnArrows.instanceColor.needsUpdate = true;
    scene.add(fnArrows);
    fnArrowPos = new Float32Array(aPos);
    fnArrowR = new Float32Array(aPos.length / 3).fill(1);   // half-height wu
  }
}

const legend = document.getElementById("legend");
// chips double as the empty-state undo handles, so keep a cid -> element map
const legendChips = new Map();
function buildLegend() {
  legend.innerHTML = ""; legendChips.clear();
  if (groupsMode && groups) {
    // supergroup chips: one per group; clicking isolates all member
    // fine-clusters at once (activeClusters stays fine-grained underneath)
    const byG = {};
    nodes.forEach(n => { if (n.gid >= 0) byG[n.gid] = (byG[n.gid] || 0) + 1; });
    Object.entries(byG).sort((a, b) => b[1] - a[1]).forEach(([gid, count]) => {
      const g = +gid;
      const c = new THREE.Color().setHSL(hue(g), 0.72, lightOf(g));
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.background = `#${c.getHexString()}22`;
      chip.style.color = `#${c.getHexString()}`;
      chip.textContent = `${gNames[g] || "g" + g} · ${count}`;
      const cids = (groups.find(gr => gr.id === g) || {}).cids || [];
      chip.onclick = () => {
        const on = !chip.classList.contains("on");
        cids.forEach(cid => { if (on) activeClusters.add(cid); else activeClusters.delete(cid); });
        chip.classList.toggle("on", on);
        applyVisibility();
        if (activeClusters.size || activeDirs.size) frameVisible(); else frameGraph();
      };
      legend.appendChild(chip);
      cids.forEach(cid => legendChips.set(cid, chip));
    });
    return;
  }
  // fine clusters: ALL clusters get a chip (the panel scrolls)
  const topClusters = Object.entries(
    nodes.reduce((acc, n) => { if (n.cluster >= 0) acc[n.cluster] = (acc[n.cluster]||0)+1; return acc; }, {})
  ).sort((a,b) => b[1]-a[1]);
  topClusters.forEach(([cid, count]) => {
    const c = new THREE.Color().setHSL(hue(+cid), 0.72, lightOf(+cid));
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.background = `#${c.getHexString()}22`;
    chip.style.color = `#${c.getHexString()}`;
    chip.textContent = `${cNames[cid] || "c" + cid} · ${count}`;
    chip.onclick = () => {
      // multi-select: chips stack, each toggles its cluster independently
      if (activeClusters.has(+cid)) { activeClusters.delete(+cid); chip.classList.remove("on"); }
      else { activeClusters.add(+cid); chip.classList.add("on"); }
      applyVisibility();
      if (activeClusters.size || activeDirs.size) frameVisible(); else frameGraph();
    };
    legend.appendChild(chip);
    legendChips.set(+cid, chip);
  });
}
buildLegend();
if (!groups) document.getElementById("bGroups").style.display = "none";
document.getElementById("bGroups").onclick = e => {
  groupsMode = !groupsMode;
  e.target.classList.toggle("on", groupsMode);
  // fine-cluster selection from the other level is stale — drop it
  activeClusters.clear();
  buildLegend();
  buildContainment();
  applyVisibility();
};

// dir filter row: top-8 directories as chips + a tests chip (tests/tools
// files are hidden by default; toggling them in rebuilds containment)
const dirsEl = document.getElementById("dirs");
const dirChips = new Map();   // dir -> chip element (empty-state undo)
{
  const byDir = {};
  nodes.forEach(n => { if (!isTestNode(n)) byDir[n.dir] = (byDir[n.dir] || 0) + 1; });
  Object.entries(byDir).sort((a, b) => b[1] - a[1]).slice(0, 8).forEach(([dir, count]) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.color = "#b0bec5";
    // root-level files carry an empty dir string — label the chip, not a blank
    chip.textContent = (dir === "" ? "(root)" : dir) + " · " + count;
    chip.onclick = () => {
      // multi-select: chips stack, each toggles its dir independently
      if (activeDirs.has(dir)) { activeDirs.delete(dir); chip.classList.remove("on"); }
      else { activeDirs.add(dir); chip.classList.add("on"); }
      buildContainment(); applyVisibility();
      if (activeDirs.size || activeClusters.size) frameVisible(); else frameGraph();
    };
    dirsEl.appendChild(chip);
    dirChips.set(dir, chip);
  });
  const tchip = document.createElement("span");
  tchip.className = "chip";
  tchip.style.color = "#ffb74d";
  tchip.textContent = "tests";
  tchip.onclick = () => {
    // toggles tests/tools visibility only — dir selections are independent
    showTests = !showTests;
    tchip.classList.toggle("on", showTests);
    buildContainment(); applyVisibility();
  };
  dirsEl.appendChild(tchip);
}

document.getElementById("bDead").onclick = e => {
  deadOnly = !deadOnly;
  e.target.classList.toggle("on", deadOnly);
  applyVisibility();
  // frame the dead archipelago: 9-90 scattered files read better zoomed
  if (deadOnly) frameVisible();
};
document.getElementById("bCyc").onclick = e => {
  cycOnly = !cycOnly;
  e.target.classList.toggle("on", cycOnly);
  applyVisibility();
  if (cycOnly) frameVisible();
};
document.getElementById("bCalls").onclick = e => {
  showCalls = !showCalls;
  e.target.classList.toggle("on", showCalls);
  applyVisibility();
};
document.getElementById("bSignals").onclick = e => {
  showSignals = !showSignals;
  e.target.classList.toggle("on", showSignals);
  applyVisibility();
};
document.getElementById("bMut").onclick = e => {
  // fn layer must be on for the mutators filter to mean anything
  if (!fnMode) { fnMode = true; document.getElementById("cbFn").checked = true; }
  mutOnly = !mutOnly;
  e.target.classList.toggle("on", mutOnly);
  applyVisibility();
};
document.getElementById("bInst").onclick = e => {
  showInst = !showInst;
  e.target.classList.toggle("on", showInst);
  applyVisibility();
};
document.getElementById("bVar").onclick = e => {
  showVar = !showVar;
  e.target.classList.toggle("on", showVar);
  applyVisibility();
};
document.getElementById("bGhost").onclick = e => {
  showGhost = !showGhost;   // quiet layer: ghost wires behind the budget
  e.target.classList.toggle("on", showGhost);
  applyVisibility();
};

const mapPane = document.getElementById("mapPane");
const MAP_MAX = 40;            // lit-node cap: past this the pane refuses
const FN_PORT_MAX = 4;          // roster rows per expanded box, then "+N more"
const NH = 22, RH = 13, GAPX = 12, TOP = 46;   // header / row / gap / first-row Y
const MAP_FONT = sz => sz + "px ui-monospace, Menlo, Consolas, monospace";
// type glyphs (spec section 3): stroke color / dash pattern / terminator.
// one font constant (above) covers ALL map text.
const MGLYPH = {
  call:   { c: "#cfe0ea", dash: null,   term: "tri" },   // cooler: stops matching roster gray
  signal: { c: "#ffb347", dash: [6, 4], term: "hollow" },
  var:    { c: "#73e68c", dash: [2, 3], term: "dot" },
  attach: { c: "#3dccf2", dash: [1, 3], term: "tbar" },
  inst:   { c: "#3dccf2", dash: [1, 3], term: "tbar" },
};
// named-wire rows (spec section 0): [ty, sf, sfn, df, dfn, line, extra]
const mwires = DATA.mwires || [];
const mfns = DATA.fns || {};                // path -> [[fn, line], ...]
// wire degree (callee / caller in-degree over ALL rows): rank for the 3D
// wire-hover tooltip's "strongest wire" pick (map F14 rank family)
const wireDeg = new Map(), wireSrcDeg = new Map();
mwires.forEach(w => {
  const dk = w[3] + "::" + w[4], sk = w[1] + "::" + w[2];
  wireDeg.set(dk, (wireDeg.get(dk) || 0) + 1);
  wireSrcDeg.set(sk, (wireSrcDeg.get(sk) || 0) + 1);
});
let mapZ = 0, mapPX = 0, mapPY = 0;      // view: zoom + pan over the world
// zoom-gated ink tiers (paint-only declutter): the fine layers (underlays,
// named wires, port dots/arrowheads) hide when zoomed out and return with
// hysteresis so wheel jitter at the threshold cannot flicker. LAYOUT IS
// NEVER TOUCHED - the wiring diagram stays THE layout at every zoom.
// Thresholds are FIT-RELATIVE (set when a layout fits): fine ink stays ON at
// the fit zoom (wire clicks work at the overview) and drops one notch out.
let mapInkLo = 0.55, mapInkHi = 0.62;
let mapInkOn = true;
const mapInkEval = () => {
  if (!mapInkOn && mapZ >= mapInkHi) mapInkOn = true;
  else if (mapInkOn && mapZ < mapInkLo) mapInkOn = false;
};
let mapDrag = null, mapDragged = false;
let mapRects = [];             // last drawn node rects (click hit-testing)
// map-center-on-selection: a 3D node click asks the 2D pane to pan the
// node's box to pane center (exactly once) and pulse it. Pane closed =
// clean no-op — nothing deferred to reopen.
let mapCenterReq = -1;
let mapPulse = null;
const MAP_CENTER_TOL_PX = 12, MAP_PULSE_MS = 900;
const mapCenterOn = i => { if (mapVisible) mapCenterReq = i; };
let mapVarsOn = false;         // var wires OFF by default, map-local chip [F10]
const mapExpandUser = new Map();   // file ix -> bool override (dblclick)
let mapHover = -1;             // hovered named-wire ix (L1 disclosure)
let mapHoverChip = -1;         // hovered bundle chip ix (cursor affordance)
let mapFrozenIx = -1;          // L3 roster-row pick: local dim 0.08, rows frozen
let mapLayout = null;          // layout cache - keyed (focus, expansion, size)
let mapDirty = false;          // rAF dirty flag: one draw per frame [F7]
let mapRefocusTimer = 0;       // click-vs-dblclick discriminator on headers
let mapVarsChipRect = null;    // screen-space vars chip rect (click hit)
function sizeMapPane() {
  const dpr = Math.min(devicePixelRatio || 1, 2);
  mapPane.width = Math.round((mapPane.clientWidth || 440) * dpr);
  mapPane.height = Math.round((mapPane.clientHeight || innerHeight) * dpr);
}
// css twin of the 3D cluster palette: stroke / translucent body fill / text
function mapCols(c) {
  if (c < 0) return { s: "hsl(198,8%,62%)", f: "hsl(198,10%,14%)", t: "hsl(198,8%,84%)" };
  const h = Math.round(hue(c) * 360), l = Math.round(lightOf(c) * 100);
  return { s: `hsl(${h},72%,${l}%)`, f: `hsl(${h},30%,12%)`, t: `hsl(${h},72%,${Math.min(92, l +
  22)}%)` };
}
// ---- map-local DOM overlays: tooltip (L1), bundle list (section 7),
// "+N more" fn picker (section 4). One container spans the pane area.
const mapOvEl = document.createElement("div");
mapOvEl.id = "mapOv";
mapOvEl.innerHTML = '<div id="mapTip"></div><div id="mapList"></div>' +
  '<div id="mapPick"><input placeholder="filter fns..."><div class="rows"></div></div>';
document.body.appendChild(mapOvEl);
const mapTipEl = mapOvEl.querySelector("#mapTip");
const mapListEl = mapOvEl.querySelector("#mapList");
const mapPickEl = mapOvEl.querySelector("#mapPick");
const mapPickIn = mapOvEl.querySelector("#mapPick input");
const mapPickRows = mapOvEl.querySelector("#mapPick .rows");
let mapPickRc = null;
function mapTipHide() { mapTipEl.style.display = "none"; }
function mapClosePick() { mapPickEl.style.display = "none"; mapPickRc = null; }
// ---- 3D wire/bus tooltip (position:fixed, follows cursor over the WebGL
// canvas; describes the picked fn wire or bus trunk)
const wireTipEl = document.createElement("div");
wireTipEl.id = "wireTip";
function hideWireTip() { wireTipEl.style.display = "none"; wireTipAnchor = null; }
document.body.appendChild(wireTipEl);
// 3D vocabulary legend (user r5): collapsed '?' chip bottom-left; one line
// when open. legendOpen is harness-pinned via __dbg.
const lg3dEl = document.getElementById("lg3d"), lg3dxEl = document.getElementById("lg3dx");
if (lg3dEl) lg3dEl.addEventListener("click", () => {
  legendOpen = !legendOpen;
  lg3dxEl.style.display = legendOpen ? "flex" : "none";
});
// strongest named wires for a file-level link — shared by edge hover and
// wire-click descriptions
function strongPair(l) {
  const pair = [];
  mwires.forEach(w => {
    if ((w[1] === l.s && w[3] === l.t) || (w[1] === l.t && w[3] === l.s)) pair.push(w);
  });
  pair.sort((a, b) =>
    (wireDeg.get(b[3] + "::" + b[4]) || 0) - (wireDeg.get(a[3] + "::" + a[4]) || 0) ||
    (wireSrcDeg.get(b[1] + "::" + b[2]) || 0) - (wireSrcDeg.get(a[1] + "::" + a[2]) || 0) ||
    (a[4] < b[4] ? -1 : a[4] > b[4] ? 1 : 0) ||
    (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0) ||
    (a[5] - b[5]));
  return pair;
}
// rider enumeration (tooltip semantics, user sighting on 7e334e3): the
// fn→fn wires a file's bus actually holds. sid < 0 = all stations of fi.
function riderWiresOf(fi, sid) {
  const out = [], seen = new Set();
  const stations = fnStationsArr || [];
  for (const S of stations) {
    if (S.fi !== fi || (sid >= 0 && S.id !== sid)) continue;
    for (const tk of (S.tks || [])) {
      const tm = trunkMetaMap && trunkMetaMap.get(tk);
      if (!tm) continue;
      for (const m of (tm.mates || [])) {
        if (fnMeta[m.a].file !== fi && fnMeta[m.b].file !== fi) continue;
        const kk = m.a + ">" + m.b + "@" + m.ln;
        if (seen.has(kk)) continue;
        seen.add(kk);
        out.push(m);
      }
    }
  }
  return out;
}
function wireDesc(meta) {
  // what the wire contains and where it goes — fn names on both ends
  if (meta.kind === "link") {
    const pair = strongPair(links[meta.li]);
    if (!pair.length) {
      // inst/signal strands carry no named wires — describe the link itself
      const l = links[meta.li];
      return "hub wire\n" + nodes[l.s].path + "  \u2192  " + nodes[l.t].path +
        "  (" + l.ty + ", w=" + l.w + ")";
    }
    return "hub wire\n" + nodes[pair[0][1]].path + " :: " + pair[0][2] +
      "  @L" + pair[0][5] + "\n  ↓ into\n" + nodes[pair[0][3]].path + " :: " + pair[0][4] +
      (pair.length > 1 ? "\n+" + (pair.length - 1) + " more named wires on this pair" : "");
  }
  if (meta.kind === "trunk") {
    const mates = meta.mates || [];
    const head = "🚌 bus " + nodes[meta.sf].path + "  →  " +
      nodes[meta.tf].path + "  (" + mates.length + " wires)";
    const body = mates.map(m =>
      "  · " + fnMeta[m.a].name + "() → " + fnMeta[m.b].name + "()"
      + (m.ln >= 0 ? "  @L" + m.ln : "")).join("\n");
    return head + (body ? "\n" + body : "");
  }
  if (meta.kind === "jleg" || meta.kind === "sub" || meta.kind === "jof") {
    const pp = meta.k ? String(meta.k).split("|") : null;
    const sid = pp ? +pp[2] : -1;
    const mates = riderWiresOf(meta.fi, sid);
    const srcs = [...new Set(mates.map(m =>
      fnMeta[fnMeta[m.a].file === meta.fi ? m.a : m.b].name))];
    const dests = [...new Set(mates.map(m =>
      fnMeta[fnMeta[m.a].file === meta.fi ? m.b : m.a].file))];
    const head = (meta.kind === "jleg" ? "🚌 junction leg · " : "🚌 bus fan · ") +
      nodes[meta.fi].path + "\n" +
      srcs.slice(0, 4).join(", ") + (srcs.length > 4 ? " +" + (srcs.length - 4) : "") +
      "  →  bus to " + dests.slice(0, 3).map(fi2 => nodes[fi2].label).join(", ") +
      (dests.length > 3 ? " +" + (dests.length - 3) : "") +
      "  · " + mates.length + " wire" + (mates.length !== 1 ? "s" : "");
    const body = mates.slice(0, 8).map(m =>
      "  · " + fnMeta[m.a].name + "() → " + fnMeta[m.b].name + "()"
      + (m.ln >= 0 ? "  @L" + m.ln : "")).join("\n");
    return head + (body ? "\n" + body : "") +
      (mates.length > 8 ? "\n+" + (mates.length - 8) + " more" : "");
  }
  if (meta.kind === "station") {
    const mates = riderWiresOf(meta.fi, -1);
    const dests = [...new Set(mates.map(m =>
      fnMeta[fnMeta[m.a].file === meta.fi ? m.b : m.a].file))];
    const head = "🚌 bus · " + nodes[meta.fi].path + "  (" + mates.length + " wires)";
    const body = mates.slice(0, 8).map(m =>
      "  · " + fnMeta[m.a].name + "() → " + fnMeta[m.b].name + "()"
      + (m.ln >= 0 ? "  @L" + m.ln : "")).join("\n");
    return head + (body ? "\n" + body : "") +
      (mates.length > 8 ? "\n+" + (mates.length - 8) + " more" : "") +
      "\n→ " + dests.length + " corridor" + (dests.length !== 1 ? "s" : "") +
      ": " + dests.slice(0, 4).map(fi2 => nodes[fi2].label).join(", ") +
      (dests.length > 4 ? " +" + (dests.length - 4) : "");
  }
  const a = fnMeta[meta.a], b = fnMeta[meta.b];
  const src = nodes[a.file], dst = nodes[b.file];
  const ty = "call";
  return ty + " wire\n" + src.path + " :: " + a.name + "()" +
    (meta.ln >= 0 ? "  @L" + meta.ln : "") +
    "\n  ↓ into\n" + dst.path + " :: " + b.name + "()";
}
function showWireTip(meta, cx, cy) {
  wireTipEl.textContent = wireDesc(meta);
  wireTipEl.style.display = "block";
  wireTipAnchor = meta || null;   // sighting #11: lifetime-tracked anchor
  const pad = 14;
  let x = cx + pad, y = cy + pad;
  const r = wireTipEl.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = cx - r.width - pad;
  if (y + r.height > innerHeight - 8) y = cy - r.height - pad;
  wireTipEl.style.left = x + "px";
  wireTipEl.style.top = y + "px";
}
// ESC priority (section 8): picker (fn picker first, then map picker),
// then pinned list, then the L3 freeze
function mapOvCloseOne() {
  let closed = false;
  if (fnPickEl.style.display === "block") { fnClosePick(); closed = true; }
  if (mapPickEl.style.display === "block") { mapClosePick(); closed = true; }
  if (mapListEl.style.display === "block") { mapListEl.style.display = "none"; closed = true; }
  if (mapFrozenIx >= 0) { mapFrozenIx = -1; closed = true; drawMapPane(); }
  return closed;
}
// L2: click a named wire -> fn panel. showFnInfo reads fnMeta[k] only, so a
// miss pushes a temporary entry around the synchronous call (removed after).
function mapShowFn(fi, name) {
  let k = -1;
  for (let j = 0; j < fnMeta.length; j++)
    if (fnMeta[j].file === fi && fnMeta[j].name === name) { k = j; break; }
  const temp = k < 0;
  if (temp) { fnMeta.push({ file: fi, name, p: [0, 0, 0] }); k = fnMeta.length - 1; }
  showFnInfo(k);
  if (temp) fnMeta.splice(k, 1);
}
// 6px SCREEN-space wire hit test (section 8): world tolerance = 6 / mapZ
const mapDistSeg = (px, py, ax, ay, bx, by) => {
  const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy || 1;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2));
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
};
function mapWireAt(wx, wy) {
  if (!mapLayout || !mapInkOn) return -1;   // ink tier off: no invisible-wire hits
  const tol = 6 / mapZ;
  let best = -1, bd = tol;
  mapLayout.wires.forEach((w, ix) => {
    if (w.bez) {   // S-curve: sample the cubic pin-to-pin
      const x0 = w.pts[0][0], y0 = w.pts[0][1],
            x1 = w.pts[1][0], y1 = w.pts[1][1];
      let qx = x0, qy = y0;
      for (let k = 1; k <= 24; k++) {
        const t = k / 24, mt = 1 - t;
        const bx = mt * mt * mt * x0 + 3 * mt * mt * t * w.c1[0] +
                   3 * mt * t * t * w.c2[0] + t * t * t * x1;
        const by = mt * mt * mt * y0 + 3 * mt * mt * t * w.c1[1] +
                   3 * mt * t * t * w.c2[1] + t * t * t * y1;
        const d = mapDistSeg(wx, wy, qx, qy, bx, by);
        if (d < bd) { bd = d; best = ix; }
        qx = bx; qy = by;
      }
      return;
    }
    for (let s = 0; s < w.pts.length - 1; s++) {
      const d = mapDistSeg(wx, wy, w.pts[s][0], w.pts[s][1],
                           w.pts[s + 1][0], w.pts[s + 1][1]);
      if (d < bd) { bd = d; best = ix; }
    }
  });
  return best;
}
function mapChipAt(wx, wy) {
  if (!mapLayout) return -1;
  for (let c = 0; c < mapLayout.chips.length; c++) {
    const ch = mapLayout.chips[c];
    if (wx >= ch.x && wx <= ch.x + ch.w && wy >= ch.y && wy <= ch.y + ch.h) return c;
  }
  return -1;
}
// L1 tooltip (section 8): call/var = A::sfn() -> B::dfn() + line + fio
// signature/writes/mutates; signal = scene > sig > B::handler [F5]
function mapTipText(w) {
  const A = nodes[w.sf], B = nodes[w.df];
  if (w.stub) {   // bus delivery: destination + member roster
    let t = "\uD83D\uDE8C bus \u2192 " + B.label + "::" + w.dfn +
      " (" + w.busN + " wires)";
    (w.mates || []).forEach(mn => { t += "\n  \u00b7 " + mn; });
    return t;
  }
  if (w.ty === "signal")
    return A.label + " > " + w.sfn + " > " + B.label + "::" + w.dfn;
  let t = A.label + "::" + w.sfn + "() \u2192 " + B.label + "::" + w.dfn + "()";
  if (w.line) t += "\nline " + w.line;
  const io = (DATA.fio || {})[B.path + "::" + w.dfn];
  if (io) {
    if (io.sig) t += "\n" + io.sig + (io.ret ? " -> " + io.ret : "");
    if (io.w.length) t += "\n\u270e " + io.w.join(", ");
    if (io.mp.length) t += "\n\u21c4 " + io.mp.join(", ");
  }
  return t;
}
// bundle list (section 7): every wire on the corridor, enumerated + scrollable
function mapOpenList(ci) {
  const ch = mapLayout.chips[ci];
  mapListEl.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = ch.origin
    ? nodes[ch.s].label + " trunk  (\u00d7" + ch.n + " wires on the pipe)"
    : ch.peel
    ? nodes[ch.s].label + " \u2192 row " + ch.row + "  (\u00d7" + ch.n + " wires)"
    : nodes[ch.s].label + " \u2192 " + nodes[ch.t].label +
      "  (" + ch.ty + " \u00d7" + ch.n + ")";
  ch.wires.forEach(wr => {
    const row = document.createElement("div");
    row.className = "row";
    row.textContent = nodes[wr.sf].label + "::" + wr.sfn + " \u2192 " +
      nodes[wr.df].label + "::" + wr.dfn + " :" + wr.line;
    row.onclick = () => { if (wr.ty !== "var") mapShowFn(wr.df, wr.dfn); };
    mapListEl.appendChild(row);
  });
  const b = mapPane.getBoundingClientRect();
  const sx = Math.max(4, Math.min((ch.x - mapPX) * mapZ + 16, mapPane.clientWidth - 262));
  const sy = Math.max(4, Math.min((ch.y - mapPY) * mapZ + 10, (b.height || innerHeight) - 170));
  mapListEl.style.left = sx + "px";
  mapListEl.style.top = sy + "px";
  mapListEl.style.display = "block";
}
// "+N more" picker (section 4 [F11]): edge-anchored, searchable, closes on
// canvas input + ESC (wire both below)
const mapPickFill = q => {
  if (!mapPickRc || !mapLayout) return;
  const rost = mapLayout.geo.get(mapPickRc.i).roster;
  mapPickRows.innerHTML = "";
  const ql = q.toLowerCase();
  rost.more.filter(r => !ql || r[0].toLowerCase().includes(ql)).slice(0, 80).forEach(r => {
    const row = document.createElement("div");
    row.className = "row";
    row.textContent = r[0] + "  :" + r[1];
    row.onclick = () => { mapShowFn(mapPickRc.i, r[0]); mapClosePick(); };
    mapPickRows.appendChild(row);
  });
};
function mapOpenPicker(rc) {
  mapPickRc = rc;
  mapPickFill("");
  const p = mapLayout.place.get(rc.i);
    mapPickEl.style.left = Math.max(4, Math.min((p.x + p.w - mapPX) * mapZ, mapPane.clientWidth - 258)) + "px";
  mapPickEl.style.top = Math.max(4, (rc.more.y0 - mapPY) * mapZ) + "px";
  mapPickEl.style.display = "block";
  mapPickIn.value = "";
  setTimeout(() => mapPickIn.focus(), 0);
}
mapPickIn.addEventListener("input", () => mapPickFill(mapPickIn.value));
mapPickIn.addEventListener("keydown", e => {
  if (e.key === "Escape") { e.stopPropagation(); mapClosePick(); }
});
// fn picker over the 3D view: clicking a per-file aggregate fn box ('n×')
// must not call showFnInfo with an empty fn name — it opens this picker
// listing the file's full roster (DATA.fns), ranked by incident-wire count
// then name; a row click = the same fn panel as clicking that fn box
// (mapShowFn covers roster fns that have no fnMeta box). Styling is
// #mapPick's via the shared selectors above.
const fnPickEl = document.createElement("div");
fnPickEl.id = "fnPick";
fnPickEl.innerHTML = '<input placeholder="filter fns..."><div class="rows"></div>';
document.body.appendChild(fnPickEl);
const fnPickIn = fnPickEl.querySelector("input");
const fnPickRows = fnPickEl.querySelector(".rows");
let fnPickFile = -1;
function fnClosePick() { fnPickEl.style.display = "none"; fnPickFile = -1; }
function fnPickFill(q) {
  if (fnPickFile < 0) return;
  const fi = fnPickFile;
  const inc = new Map();   // fn -> incident wire count (calls in + out)
  mwires.forEach(w => {
    if (w[1] === fi) inc.set(w[2], (inc.get(w[2]) || 0) + 1);
    if (w[3] === fi) inc.set(w[4], (inc.get(w[4]) || 0) + 1);
  });
  fnPickRows.innerHTML = "";
  const ql = q.toLowerCase();
  (mfns[nodes[fi].path] || [])
    .filter(r => !ql || r[0].toLowerCase().includes(ql))
    .map(r => [r[0], r[1], inc.get(r[0]) || 0])
    .sort((a, b) => (b[2] - a[2]) ||
      (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .slice(0, 80)
    .forEach(r => {
      const row = document.createElement("div");
      row.className = "row";
      row.textContent = r[0] + "  :" + r[1];
      row.onclick = () => { fnClosePick(); mapShowFn(fi, r[0]); };
      fnPickRows.appendChild(row);
    });
}
function openFnPicker(fi, x, y) {
  fnPickFile = fi;
  fnPickFill("");
  fnPickEl.style.left = Math.max(4, Math.min(x, innerWidth - 270)) + "px";
  fnPickEl.style.top = Math.max(4, Math.min(y, innerHeight - 40)) + "px";
  fnPickEl.style.display = "block";
  fnPickIn.value = "";
  setTimeout(() => fnPickIn.focus(), 0);
}
fnPickIn.addEventListener("input", () => fnPickFill(fnPickIn.value));
fnPickIn.addEventListener("keydown", e => {
  if (e.key === "Escape") { e.stopPropagation(); fnClosePick(); }
});
const MAP_WORLD_W = 1100;   // world width CAP - the pane is a window onto it
function mapRender() {
  if (!mapVisible) return;
  const ctx = mapPane.getContext("2d");
  const cwView = mapPane.clientWidth || 440;
  const chView = mapPane.clientHeight || innerHeight;
  const dpr = mapPane.width / cwView || 1;
  // world width FOLLOWS the pane (pane + 170, clamped): the fit zoom then
  // lands near min(paneW/worldW, paneH/worldH) so every box is visible at
  // boot while text keeps >=~8px effective (F15 rev2: fit below 1.0 allowed,
  // floor 0.55 protects text on pathological repos)
  const cw = MAP_WORLD_W;   // scan ceiling; the fit scan picks the real width
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0b0f14";
  ctx.fillRect(0, 0, cwView, chView);
  const hint = txt => {
    ctx.fillStyle = "#546e7a";
    ctx.font = MAP_FONT(12);
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(txt, cwView / 2, chView / 2);
  };
  // NOTE: mapRects is NOT cleared here - a cache-hit repaint below skips the
  // rebuild and must keep the last hit-test rects; only discard/rebuild paths
  // touch it
  if (!focusActive) { mapLayout = null; mapRects = []; hint("focus a node to see its map"); return; }
  const litAll = [];
  for (let i = 0; i < N; i++) if (level[i] >= 0 && nodeVisible(nodes[i])) litAll.push(i);
  // cap by connectivity: keep the MAP_MAX most-connected lit files so a hub
  // focus still draws a diagram instead of a "narrow the focus" shrug
  let lit = litAll;
  const capNote = litAll.length > MAP_MAX ? litAll.length : 0;
  if (capNote) {
    lit = litAll.slice().sort((a, b) => (degree[b] - degree[a]) || (a - b)).slice(0, MAP_MAX);
  }
  if (!lit.length) { mapLayout = null; mapRects = []; hint("focus a node to see its map"); return; }
  // ONE layout at every zoom (owner mandate): no admission tiers, no doc
  // scoping - the wiring diagram below is zoom-independent. Relayouts fire
  // only on focus/visibility/expansion changes (cache key below).
  // focus signature ("focusVersion"): every input that changes the lit set
  // or the typed admission. pan/zoom never touch it (section 5).
  const sig = lit.join(",") + "|" + query + "|" + mapVarsOn + "|" +
    typeVisible("call") + typeVisible("signal") + typeVisible("inst");
  if (mapLayout && mapLayout.sig !== sig) mapZ = 0;   // focus change -> refit
  // tier-1 admission: file skeleton unchanged (survives section 10)
  const litSet = new Set(lit);
  const cand = [];
  const seenPair = new Set();
  links.forEach(l => {
    if (l.s === l.t || !litSet.has(l.s) || !litSet.has(l.t) || !typeVisible(l.ty)) return;
    const k = l.s + "_" + l.t;
    if (seenPair.has(k)) return;
    seenPair.add(k);
    cand.push(l);
  });
  const top8 = new Set([...lit].sort((a, b) => degree[b] - degree[a] || a - b).slice(0, 8));
  cand.sort((a, b) => (b.w || 1) - (a.w || 1) || a.s - b.s || a.t - b.t);
  let edges = cand.filter((l, i) =>
    (l.w || 1) >= 2 || i < 120 || top8.has(l.s) || top8.has(l.t)).slice(0, 160);
  const E = edges.length;
  // expansion set: user dblclick override > every wired box opens (roster
  // rows are THE layout - named wires terminate on fn rows) > seeds open
  // at L0. Zoom-independent: structure never changes with zoom.
  const wireInc = new Map();
  edges.forEach(l => {
    wireInc.set(l.s, (wireInc.get(l.s) || 0) + 1);
    wireInc.set(l.t, (wireInc.get(l.t) || 0) + 1);
  });
  const expand = new Set();
  lit.forEach(i => {
    const u = mapExpandUser.get(i);
    const wired = (wireInc.get(i) || 0) >= 1;
    // seed = BFS level 0 (click seeds AND query matches): roster open at L0
    if (u !== undefined ? u : (wired || level[i] === 0))
      expand.add(i);
  });
  // layout cache (section 5 [F7]): hit = pure repaint under pan/zoom
  const key = sig + "||" +
    [...expand].sort((a, b) => a - b).join(",") + "||" +
    Math.round(cwView) + "x" + Math.round(chView);
  if (mapLayout && mapLayout.key === key) {
    mapConsumeCenterReq();
    mapPaint(ctx, dpr, cwView, chView, capNote);
    return;
  }
  // ---- tier-2 named wires over the admitted corridors (section 2 [F1]) ----
  const pairSet = new Set(edges.map(l => l.s + "_" + l.t));
  const vw = [];
  mwires.forEach(w => {
    if (w[0] === "var" ? !mapVarsOn : !typeVisible(w[0])) return;
    const sf = w[1], df = w[3];
    if (typeof sf !== "number" || typeof df !== "number") return;
    if (!pairSet.has(sf + "_" + df)) return;   // named wires ride admitted pairs
    vw.push({ ty: w[0], sf, sfn: String(w[2]), df, dfn: String(w[4]), line: w[5] || 0 });
  });
  const inDegFn = new Map();   // callee in-degree: head of the F14 rank
  vw.forEach(w => {
    const k = w.df + "::" + w.dfn;
    inDegFn.set(k, (inDegFn.get(k) || 0) + 1);
  });
  const byPair = new Map();
  vw.forEach(w => {
    const k = w.sf + "_" + w.df;
    let a = byPair.get(k);
    if (!a) byPair.set(k, a = []);
    a.push(w);
  });
  const rk = (a, b) =>
    ((inDegFn.get(b.df + "::" + b.dfn) || 0) - (inDegFn.get(a.df + "::" + a.dfn) || 0)) ||
    (a.dfn < b.dfn ? -1 : a.dfn > b.dfn ? 1 : 0) ||
    (a.sf - b.sf) ||
    (a.sfn < b.sfn ? -1 : a.sfn > b.sfn ? 1 : 0) ||
    (a.line - b.line);
  const indiv = [];            // top-1 per pair: drawn + labeled [F14]
  byPair.forEach(a => { a.sort(rk); indiv.push(a[0]); });
  indiv.sort(rk);
  const indivSet = new Set(indiv);
  // ---- rosters (section 4) ----
  const fioMap = DATA.fio || {};
  const rosterOf = i => {
    const all = (mfns[nodes[i].path] || []).slice();   // complete roster
    if (!all.length) return null;
    const pin = new Set();
    byPair.forEach(arr => arr.forEach(w => {   // every named wire pins its rows
      if (w.df === i) pin.add(w.dfn);
      if (w.sf === i) pin.add(w.sfn);
    }));
    indiv.forEach(w => {   // a named wire must terminate on its fn row
      if (w.df === i) pin.add(w.dfn);
      if (w.sf === i) pin.add(w.sfn);
    });
    const rank = nm => inDegFn.get(i + "::" + nm) || 0;
    all.sort((a, b) => ((pin.has(b[0]) ? 1 : 0) - (pin.has(a[0]) ? 1 : 0)) ||
      rank(b[0]) - rank(a[0]) ||
      (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0) || (a[1] - b[1]));
    const shown = all.slice(0, FN_PORT_MAX);
    return {
      rows: shown.map(r => ({ nm: r[0], ln: r[1], io: fioMap[nodes[i].path + "::" + r[0]] })),
      more: all.slice(FN_PORT_MAX),
    };
  };
  // geometry (F6: expansion resolved BEFORE the row wrap); NH/RH are module consts
  ctx.font = MAP_FONT(10);
  const txtW = t => Math.ceil(ctx.measureText(t).width);
  const wOf = i => Math.max(38, txtW(nodes[i].label) + 18);
  const geo = new Map();
  let rosterRows = 0;
  lit.forEach(i => {
    if (!expand.has(i)) { geo.set(i, { w: wOf(i), h: NH, roster: null }); return; }
    const r = rosterOf(i);
    if (!r || !r.rows.length) { geo.set(i, { w: wOf(i), h: NH, roster: null }); return; }
    let w = wOf(i);
    r.rows.forEach(row => {
      w = Math.max(w, txtW(row.nm) +
        (row.io && row.io.w.length ? txtW("\u270e" + row.io.w.length) + 8 : 0) + 16);
    });
    w = Math.min(420, w);
    rosterRows += r.rows.length;
    geo.set(i, { w, h: NH + (r.rows.length + (r.more.length ? 1 : 0)) * RH, roster: r, more: r.more });
  });
  // flow rows: longest call-path depth INSIDE the lit set (Bellman-Ford
  // relaxation, cycle-capped by lit.length) so callers sit above callees —
  // one flow direction (Mermaid/Blueprint law). BFS level rings callers and
  // callees together (wires then read upward); level still seeds lit and
  // expansion. Every lit node gets an fd, so the placer covers lit even
  // when the pinned subject sits outside the search BFS (old level -1 case).
  const callF = edges.filter(l => l.ty === "call");
  const fd = new Map();
  lit.forEach(i => fd.set(i, 0));
  for (let sweep = 0; sweep < lit.length; sweep++) {
    let moved = 0;
    callF.forEach(l => {
      const d = fd.get(l.s) + 1;
      if (l.s !== l.t && d > fd.get(l.t) && d < lit.length) { fd.set(l.t, d); moved++; }
    });
    if (!moved) break;
  }
  const rows = [];
  lit.forEach(i => (rows[fd.get(i)] = rows[fd.get(i)] || []).push(i));
  const preds = new Map();
  edges.forEach(l => {
    if (!preds.has(l.t)) preds.set(l.t, []);
    preds.get(l.t).push(l.s);
  });
  const col = new Map();
  for (let r = 0; r < rows.length; r++) if (rows[r]) rows[r].forEach((i, k) => col.set(i, k));
  for (let sw = 0; sw < 3; sw++) {
    for (let r = 0; r < rows.length; r++) {
      const row = rows[r];
      if (!row || row.length < 2) continue;
      const bc = row.map(i => {
        const ps = preds.get(i);
        if (!ps || !ps.length) return { i, b: col.get(i) };
        let s = 0; for (const p of ps) s += col.get(p);
        return { i, b: s / ps.length };
      });
      bc.sort((a, b) => a.b - b.b || a.i - b.i);
      rows[r] = bc.map(x => x.i);
      rows[r].forEach((i, k) => col.set(i, k));
    }
  }
  // rows WRAP to world width against the resolved box widths. The world
  // width is CHOSEN: a narrow world wraps into more/taller chunks, a wide
  // one into fewer/flatter - the fit zoom z = min(paneW/W, paneH/H(W)) has
  // an interior optimum. Wrap once per candidate width (cheap: <=40 boxes),
  // keep the width that maximizes z (ties: narrower world). Deterministic:
  // same data + pane => same scan.
  const wrapChunks = W => {
    const ch = [];
    for (let r = 0; r < rows.length; r++) {
      const row = rows[r];
      if (!row) continue;
      let cur = [], twc = -GAPX;
      row.forEach(i => {
        const w = geo.get(i).w;
        if (twc + GAPX + w > W && cur.length) { ch.push(cur); cur = []; twc = -GAPX; }
        cur.push(i); twc += GAPX + w;
      });
      if (cur.length) ch.push(cur);
    }
    return ch;
  };
  // band demand: how many admitted edges cross each chunk boundary. Hot
  // corridors (hub adjacency) get TALLER gap bands so channel capacity
  // grows with traffic - horizontals ladder instead of piling on the band
  // floor (the near-parallel wall). Deterministic from edges order.
  const bandPads = ch => {
    const cOf = new Map();
    ch.forEach((chunk, g) => chunk.forEach(i => cOf.set(i, g)));
    const cross = ch.map(() => 0);
    edges.forEach(l => {
      const a = cOf.get(l.s), b = cOf.get(l.t);
      if (a === undefined || b === undefined || a === b) return;
      for (let g = Math.min(a, b); g < Math.max(a, b); g++) cross[g]++;
    });
    return cross.map(n => 36 + 7 * Math.max(0, Math.min(16, n - 4)));
  };
  const wrapH = ch => {   // per-chunk rowH = max(56, tallest + band pad) [F6]
    // band pads grow with crossing demand (capacity for hot corridors) but
    // under a HARD height budget: the fit zoom may not fall below ~0.70 or
    // the whole pane shrinks to a thumbnail. Extras scale down to fit.
    const Hmax = chView / 0.70;
    const rowHOf = (tall, pad) => Math.max(56, tall + pad);
    const total = pads => {
      let y = TOP;
      ch.forEach((chunk, g) => {
        let tall = NH;
        chunk.forEach(i => { tall = Math.max(tall, geo.get(i).h); });
        y += rowHOf(tall, pads[g]);
      });
      return y + 20;
    };
    let pads = bandPads(ch);
    const base = ch.map(() => 36);
    const H1 = total(pads);
    if (H1 > Hmax && H1 > total(base)) {
      const s = Math.max(0, (Hmax - total(base)) / (H1 - total(base)));
      pads = pads.map((p, g) => base[g] + Math.round((p - base[g]) * s));
    }
    const tops = [];
    ch.forEach((chunk, g) => {
      let tall = NH;
      chunk.forEach(i => { tall = Math.max(tall, geo.get(i).h); });
      tops.push(tall);
      pads[g] = rowHOf(tall, pads[g]) - tall;   // effective pad after floor
    });
    return { h: Math.max(chView, total(pads)), tops, pads };
  };
  let cwBest = Math.min(640, cw), zBest = -1;
  for (let W = 560; W <= cw; W += 20) {
    const z = Math.min(cwView / W, chView / wrapH(wrapChunks(W)).h);
    if (z > zBest + 1e-9) { zBest = z; cwBest = W; }
  }
  const cwL = cwBest;                    // resolved world width for THIS pane
  const chunks = wrapChunks(cwL);
  const wrapRes = wrapH(chunks);
  const chunkRowH = [], chunkY = [], chunkTop = [];
  let wy = TOP;
  chunks.forEach((chunk, g) => {
    let tall = NH;
    chunk.forEach(i => { tall = Math.max(tall, geo.get(i).h); });
    chunkTop.push(tall);
    chunkRowH.push(Math.max(56, tall + wrapRes.pads[g]));
    chunkY.push(wy);
    wy += Math.max(56, tall + wrapRes.pads[g]);
  });
  const worldH = Math.max(chView, wy + 20);
  const place = new Map();
  chunks.forEach((chunk, rr) => {
    const tw = chunk.reduce((a, i) => a + geo.get(i).w, 0) + GAPX * (chunk.length - 1);
    let x = Math.max(8, (cwL - tw) / 2);
    const y = chunkY[rr];
    chunk.forEach(i => {
      place.set(i, { x, y, w: geo.get(i).w, h: geo.get(i).h, row: rr });
      x += geo.get(i).w + GAPX;
    });
  });
  if (!mapZ) {   // focus change / first draw: fit BOTH dims [F15 rev2]
    mapZ = Math.max(0.55, Math.min(1.0, Math.min(cwView / cwL, chView / worldH)));
    mapPX = (cwL - cwView / mapZ) / 2;    // negative when world < pane: centers
    mapPY = (worldH - chView / mapZ) / 2; // the shrunken content (D2)
    // fit-relative ink tier: fine ink ON at the overview (wire clicks work
    // there), one wheel-notch out it drops; restore needs +14% (hysteresis)
    mapInkLo = Math.max(0.35, Math.min(0.85, mapZ * 0.97));
    mapInkHi = Math.min(1.0, mapInkLo * 1.14);
  }
  mapInkEval();   // hysteresis re-arm after every zoom change
  // inter-row gap bands + lane machinery (survives section 10)
  const rects = [];
  place.forEach(p => rects.push({ x0: p.x, x1: p.x + p.w, y0: p.y, y1: p.y + p.h }));
  const gapY = [];
  for (let g = 0; g < chunks.length - 1; g++)
    gapY.push({ y0: chunkY[g] + chunkTop[g] + 2, y1: chunkY[g + 1] - 2 });
  const laneX = [];
  const crossedBands = (ya, yb) => {
    const out = [];
    for (let g = 0; g < gapY.length; g++)
      if (gapY[g].y1 > ya && gapY[g].y0 < yb) out.push(g);
    return out;
  };
  let maxX = 90;
  geo.forEach(g => { maxX = Math.max(maxX, g.w); });   // freeX radius scales [F6]
  const freeX = (x, ya, yb, maxR, pad) => {
    x = Math.max(6, Math.min(cwL - 6, x));   // lanes never leave the world
    const span = rects.filter(r => r.y1 > ya && r.y0 < yb);
    const bands = crossedBands(ya, yb);
    const spanAt = c => span.some(r => c >= r.x0 && c <= r.x1);
    // 7px exclusivity: parallel verticals closer than this read as ONE line
    // at 1x zoom (user: "vertical lines overlap") — lanes are a scarce
    // resource, bundling only as the last resort. Trunks scan at 12px so two
    // thick corridors never parallel inside one screen glance (pad param)
    const laneAt = c =>
      bands.some(g => laneX[g] && laneX[g].some(u => Math.abs(u - c) < (pad || 7)));
    // cost 0 = clean lane, 1 = shares a band lane (last resort), 2 = crosses
    // a box (never acceptable) — scan outward, take the first clean slot.
    // Long hauls may scan the whole world: with maxX exhausted they used to
    // accept a box-crossing x (the piercing bug) — a clean column always
    // exists near the world margins, and the trip there is worth it.
    const cost = c => (spanAt(c) ? 2 : 0) + (laneAt(c) ? 1 : 0);
    let bestX = x, bestB = cost(x);
    for (let d = 5; d <= (maxR || maxX) && bestB > 0; d += 5) {
      for (const c of [x + d, x - d]) {
        if (c < 6 || c > cwL - 6) continue;
        const b = cost(c);
        if (b < bestB) { bestB = b; bestX = c; }
        if (b === 0) break;
      }
    }
    // never a bezier; a shared lane bundles, a box-span never survives
    return { x: bestX, ok: bestB < 2, clean: bestB === 0 };
  };
  const claimLane = (x, ya, yb) => {
    for (const g of crossedBands(ya, yb)) (laneX[g] || (laneX[g] = [])).push(x);
  };
  const usedY = [];
  const spanHits = (x0, x1, y) => rects.some(r =>
    y >= r.y0 && y <= r.y1 && x1 >= r.x0 && x0 <= r.x1);
  const nextY = (x0, x1, startY, limitY, step) => {
    step = step || 7;   // 7px channel lattice; thin tap rails may pack at 5
    // channel claims are X-AWARE: two horizontals may share a y when their
    // x-spans barely overlap (a shared channel far apart reads as one line
    // anyway); within an overlapping span the lattice keeps them >=step apart
    const xa = Math.min(x0, x1), xb = Math.max(x0, x1);
    const blocked = y =>
      usedY.some(u => Math.abs(u.y - y) < step &&
        Math.min(u.b, xb) - Math.max(u.a, xa) > 10) ||
      spanHits(xa, xb, y);
    let y = startY;
    while (blocked(y) && y < limitY) y += step;
    // a blocked band admits exhaustion — never let the step overshoot PAST
    // the band floor onto the box tops of the next chunk. Landing EXACTLY on
    // the floor counts as exhaustion too (the 7px lattice from below hits it
    // dead on), and the floor spreads: each exhausted caller climbs one
    // lattice step above the last so exhaustion never stacks two overlapping
    // horizontals within the pairing gate.
    if (y >= limitY) {
      y = limitY;
      while (blocked(y) && y - step >= Math.min(startY, limitY)) y -= step;
    }
    usedY.push({ y, a: xa, b: xb });
    return { y, ok: !blocked(y) };
  };
  // roster row lookup: "fileIx<null>fn" -> row index (wires terminate on rows)
  const rowOf = new Map();
  place.forEach((p, i) => {
    const g = geo.get(i);
    if (!g.roster) return;
    g.roster.rows.forEach((r, k) => rowOf.set(i + "\x00" + r.nm, k));
  });
  // port spreads: box-level for spines/underlays/row-less wires, row-level
  // for wires that own a roster row
  const outN = new Map(), inN = new Map(), outIx = new Map(), inIx = new Map();
  edges.forEach(l => {
    outN.set(l.s, (outN.get(l.s) || 0) + 1);
    inN.set(l.t, (inN.get(l.t) || 0) + 1);
  });
  const rowTotOut = new Map(), rowTotIn = new Map(),
        rowIxOut = new Map(), rowIxIn = new Map();
  indiv.forEach(w => {
    const sr = rowOf.get(w.sf + "\x00" + w.sfn);
    if (sr === undefined) outN.set(w.sf, (outN.get(w.sf) || 0) + 1);
    else rowTotOut.set(w.sf + "_" + sr, (rowTotOut.get(w.sf + "_" + sr) || 0) + 1);
    const dr = rowOf.get(w.df + "\x00" + w.dfn);
    if (dr === undefined) inN.set(w.df, (inN.get(w.df) || 0) + 1);
    else rowTotIn.set(w.df + "_" + dr, (rowTotIn.get(w.df + "_" + dr) || 0) + 1);
  });
  const underlays = [], spines = [], wires = [];
  const routeOrtho = (A, B, sy, ty, sameRow, sx0, tx0, bus, lanePad, yPad) => {
    // long hauls cross every chunk between the two rows — hand the router
    // the FULL band range so the staircase can hop chunk-by-chunk. The old
    // 1px window at the target edge returned at most one band, so the
    // staircase never fired and every haul fell to the single-band channel.
    let bands = sameRow
      ? crossedBands(sy, sy + 1)
      : crossedBands(Math.min(sy, ty), Math.max(sy, ty));
    if (!bands.length) {
      // same-row dip: drop to the first gap band BELOW the row
      const bi = gapY.findIndex(g => g.y0 > sy);
      if (bi >= 0) bands = [bi];
    }
    // exit-hoist: if A is shorter than a row-mate, the bottom-edge exit
    // horizontal would slice through it — drop to the row's true bottom
    // first (the sx0 drop is clean: same-row boxes never overlap A's x-span)
    let syE = sy;
    for (const r of rects) {
      if (r === A || r.y0 >= sy - 2 || r.y1 <= sy + 2) continue;
      syE = Math.max(syE, r.y1);
    }
    const up = ty < sy;
    // STAIRCASE for long hauls (ELK between-layer law): a clean column
    // through EVERY chunk rarely exists, so hop band-by-band — one vertical
    // per chunk, each cleared against that chunk only, channels accumulating
    // in the bands as a metro yard. Verticals can no longer pierce a row.
    if (bands.length >= 2) {
      // both build directions run top-to-bottom (up-hauls start at the
      // target's bottom edge), so bands ascend toward the cursor either way
      const seq = bands;
      const xStart = up ? tx0 : sx0, xEnd = up ? sx0 : tx0;
      // attach-edge law: up-hauls leave the source TOP edge, down-hauls the
      // bottom (syE hoist) — the vertical to the first band then only ever
      // crosses the 2px margin, never the row's own boxes
      const yTop = up ? ty : syE, yBot = up ? A.y : ty;
      const B2 = up ? A : B;
      const cols = [];
      let seed = xStart, prevY = yTop;
      for (let i = 0; i < seq.length; i++) {
        const g = gapY[seq[i]];
        const yc = nextY(Math.min(seed, xEnd), Math.max(seed, xEnd),
                         g.y0 + 3 + (yPad || 0), g.y1);
        const fx = freeX(seed, prevY, yc.y, cwL, lanePad);
        if (fx.ok) claimLane(fx.x, prevY, yc.y);
        cols.push({ x: fx.x, yCh: yc.y, yFloor: g.y1 });
        seed = fx.x;
        prevY = yc.y;
      }
      const fxN = freeX(seed, prevY, yBot, cwL, lanePad);
      const xN = Math.max(B2.x + 2, Math.min(B2.x + B2.w - 2, fxN.x));
      if (fxN.ok) claimLane(xN, prevY, yBot);
      const p = [[xStart, yTop]];
      let cx = xStart, cy = yTop;
      for (let i = 0; i < cols.length; i++) {
        const c = cols[i], nx = i + 1 < cols.length ? cols[i + 1].x : xN;
        const d = nx >= c.x ? 1 : -1;
        const chIn = Math.max(0, Math.min(8,
          Math.abs(c.x - cx) / 2, (c.yCh - cy) / 2));
        if (Math.abs(c.x - cx) > 0.5) p.push([c.x, cy]);
        p.push([c.x, c.yCh - chIn], [c.x + d * chIn, c.yCh]);
        // chamfer descent never leaves the band: floor-clamped, else the
        // post-channel horizontal slices the next chunk's box tops
        const chOut = Math.max(0, Math.min(8, Math.abs(nx - c.x) / 2,
          c.yFloor - c.yCh));
        p.push([nx - d * chOut, c.yCh], [nx, c.yCh + chOut]);
        cx = nx; cy = c.yCh + chOut;
      }
      // orthogonal arrival: chamfer into the target column, then drop/rise
      // vertically onto the port — the final leg must never be a diagonal
      const dE = xEnd >= cx ? 1 : -1;
      const chE = Math.max(0, Math.min(8, Math.abs(xEnd - cx) / 2,
        Math.abs(yBot - cy) / 2));
      p.push([xEnd - dE * chE, cy], [xEnd, cy + (yBot >= cy ? chE : -chE)],
             [xEnd, yBot]);
      if (up) p.reverse();
      if (!up && syE > sy) p.unshift([sx0, sy]);
      // terminator anchors on pts[last] = (tx0, ty) for BOTH directions;
      // the old up ? sx0 floated arrowheads / T-ticks off the path end
      return { pts: p, bez: false, tx: tx0, ty,
               back: ty < sy };
    }
    // channel placement: scan each gap band in order; a band that admits a
    // clear horizontal wins, an exhausted band is only the bundled fallback
    // (channels never slice through the chunk between bands)
    let yc = null;
    for (const bi of bands) {
      const g = gapY[bi];
      if (!g) continue;
      const t = nextY(Math.min(sx0, tx0), Math.max(sx0, tx0),
                      g.y0 + 3 + (yPad || 0), g.y1);
      if (!yc) yc = t;
      if (t.ok) { yc = t; break; }
    }
    if (!yc) yc = { y: (sy + ty) / 2, ok: false };
    const yCh = yc.y;
    const sY = up ? A.y : syE;   // attach edge = the side facing the channel
    const fx = freeX(sx0, Math.min(sY, yCh), Math.max(sY, yCh), null, lanePad);
    const sx = fx.x;
    if (fx.ok) claimLane(sx, sY, yCh);
    const fx2 = freeX(tx0, Math.min(yCh, ty), Math.max(yCh, ty), null, lanePad);
    const tx = Math.max(B.x + 2, Math.min(B.x + B.w - 2, fx2.x));
    if (fx2.ok) claimLane(tx, yCh, ty);
    // freeX/nextY no longer fail: clean lanes first, else bundled corridors —
    // the bezier fallback died with the diagonal layer
    const dir = tx >= sx ? 1 : -1;
    const ch = Math.max(0, Math.min(8, Math.abs(tx - sx) / 2,
      Math.abs(yCh - sY) / 2, Math.abs(yCh - ty) / 2));
    const tyDir = ty >= yCh ? 1 : -1;   // approach side of the channel
    const sDir = yCh >= sY ? 1 : -1;    // source side of the channel
    return {
      pts: !up && syE > sy
        ? [[sx0, sy], [sx0, syE], [sx, syE], [sx, yCh - ch],
           [sx + dir * ch, yCh], [tx - dir * ch, yCh],
           [tx, yCh + tyDir * ch], [tx, ty]]
        : [[sx0, sY], [sx, sY], [sx, yCh - sDir * ch], [sx + dir * ch, yCh],
           [tx - dir * ch, yCh], [tx, yCh + tyDir * ch],
           [tx, ty]],
      bez: false, tx, ty,
      back: !sameRow && ty < sy,
    };
  };
  // 1) attach/inst underlays: anonymous + demoted (section 1) - 1px, alpha
  //    0.40, T-junction entry, routed FIRST so named wires claim lanes first
  edges.forEach(l => {
    if (l.ty !== "attach" && l.ty !== "inst") return;
    const A = place.get(l.s), B = place.get(l.t);
    if (!A || !B) return;
    const sy = A.y + A.h;
    // same CHUNK row, not same BFS depth: depth rows wrap to world width,
    // so equal fd can land in adjacent chunks — routing those as a same-row
    // dip dropped the channel mid-air (yCh = midpoint) through box interiors
    const sameRow = A.row === B.row;
    // up-hauls enter the target's bottom edge (the source sits below it)
    const ty = (sameRow || B.y + B.h <= sy) ? B.y + B.h : B.y;
    const sIx = outIx.get(l.s) || 0; outIx.set(l.s, sIx + 1);
    const tIx = inIx.get(l.t) || 0; inIx.set(l.t, tIx + 1);
    const sx0 = A.x + A.w * (sIx + 1) / ((outN.get(l.s) || 1) + 1);
    const tx0 = B.x + B.w * (tIx + 1) / ((inN.get(l.t) || 1) + 1);
    const ur = routeOrtho(A, B, sy, ty, sameRow, sx0, tx0, true);
    ur.flow = sameRow ? "same" : (ty > sy ? "down" : "up");
    underlays.push(Object.assign({ s: l.s, t: l.t, ty0: l.ty }, ur));
  });
  // 2) corridor spines (section 2 tier-1): records first - routing waits for
  //    the hub-bus grouping, which decides who rides a shared trunk. A
  //    signal pair that resolved zero handlers renders as an anonymous amber
  //    corridor [F13]. wty = wire TYPE (sp.ty stays the y-coordinate that
  //    routeOrtho returns; the old build let the y overwrite l.ty, so every
  //    spine painted call-gray - the "near-identical gray wires" complaint).
  edges.forEach(l => {
    if (l.ty === "attach" || l.ty === "inst") return;
    const A = place.get(l.s), B = place.get(l.t);
    if (!A || !B) return;
    const sy = A.y + A.h;
    // same CHUNK row, not same BFS depth: depth rows wrap to world width,
    // so equal fd can land in adjacent chunks (see underlays)
    const sameRow = A.row === B.row;
    // up-hauls enter the target's bottom edge (the source sits below it)
    const ty = (sameRow || B.y + B.h <= sy) ? B.y + B.h : B.y;
    const sIx = outIx.get(l.s) || 0; outIx.set(l.s, sIx + 1);
    const tIx = inIx.get(l.t) || 0; inIx.set(l.t, tIx + 1);
    const sx0 = A.x + A.w * (sIx + 1) / ((outN.get(l.s) || 1) + 1);
    const tx0 = B.x + B.w * (tIx + 1) / ((inN.get(l.t) || 1) + 1);
    spines.push({ s: l.s, t: l.t, wty: l.ty, pair: l.s + "_" + l.t,
      amber: l.ty === "signal" && !(byPair.get(l.s + "_" + l.t) || []).length,
      sRow: A.row, tRow: B.row, sx0, tx0, sy, ty, sameRow,
      flowSum: l.w || 1, con: false, pts: [], bez: false,
      flow: sameRow ? "same" : (ty > sy ? "down" : "up"), back: false });
  });
  // ---- hub buses (Blueprint reroute, tier-1): every corridor leaving ONE
  // source in ONE direction (down/up/same) merges onto a single thick trunk
  // that rides routeOrtho to the deterministic farthest rider's REAL box;
  // each rider is delivered by a thin type-colored tap peeling off the trunk
  // in the band adjacent to its row. Peel points are router OUTPUT (points
  // on the routed trunk), never invented geometry - routeOrtho stays the one
  // authority, so the no-diagonal law holds by construction.
  // Deterministic: buses keyed source+direction, insertion = edges order
  // (w desc, s, t); farthest row numeric; port picks are lower medians.
  const hubBuses = [];                  // {trunk, taps, peels, members}
  const hubDots = [];                   // junction dots painted ON paths
  let hubTrunks = 0, hubTaps = 0, hubRiders = 0, hubPeels = 0;
  const dirOf = sp => sp.sameRow ? "same" : (sp.tRow > sp.sRow ? "down" : "up");
  const spineBusMap = new Map();
  spines.forEach(sp => {
    if (sp.amber) return;               // F13: amber corridors stay individual
    const k = sp.s + "|" + dirOf(sp);
    let a = spineBusMap.get(k);
    if (!a) spineBusMap.set(k, a = []);
    a.push(sp);
  });
  spineBusMap.forEach(a => {
    if (a.length < 2) return;
    const dir = dirOf(a[0]);
    // same-row dips need a band below the source chunk to park the channel
    if (dir === "same" && !gapY[a[0].sRow]) return;
    const rows = [...new Set(a.map(x => x.tRow))].sort((p, q) => p - q);
    const farRow = dir === "up" ? rows[0] : rows[rows.length - 1];
    const inFar = a.filter(x => x.tRow === farRow)
      .sort((x, y) => x.tx0 - y.tx0 || (x.pair < y.pair ? -1 : 1));
    const far = inFar[Math.floor((inFar.length - 1) / 2)];
    const A = place.get(far.s), Bf = place.get(far.t);
    const flowSum = a.reduce((s, x) => s + x.flowSum, 0);
    const wTr = Math.min(2 + 0.85 * Math.log2(flowSum), 5.5);
    // trunk: STRAIGHT-COLUMN FIRST (metro few-bends law): if one clean
    // vertical lane spans the whole haul (freeX cost 0 - usually near the
    // world margins), the trunk is exit-channel -> column -> arrival-channel
    // (5 bends) instead of a per-band staircase (2+ bends per band crossed).
    // Staircase is the fallback when every column pierces a box.
    // Wide lane pad (no two trunks parallel inside 12px), channel floor
    // seeded below the stroke half-width so it never bleeds onto box tops.
    const lanePad = Math.max(9, 9 / mapZ), yPad = 3 + Math.ceil(wTr / 2);
    let tr = null;
    if (dir !== "same") {
      const bi = dir === "down" ? A.row : A.row - 1;      // exit-side band
      const ai = dir === "down" ? farRow - 1 : farRow;    // arrival band
      const gb = gapY[bi], ga = gapY[ai];
      const sYx = dir === "down" ? A.y + A.h : A.y;
      if (gb && ga && ((dir === "down" && sYx <= gb.y0) ||
                       (dir === "up" && sYx >= gb.y1))) {
        const fx = freeX(far.sx0, gb.y0 + 3, ga.y1, cwL, lanePad);
        if (fx.clean) {
          const yEx = nextY(Math.min(far.sx0, fx.x), Math.max(far.sx0, fx.x),
            gb.y0 + 3 + yPad, gb.y1);
          const yAr = nextY(Math.min(fx.x, far.tx0), Math.max(fx.x, far.tx0),
            ga.y0 + 3 + yPad, ga.y1);
          claimLane(fx.x, yEx.y, yAr.y);
          tr = { pts: [[far.sx0, sYx], [far.sx0, yEx.y], [fx.x, yEx.y],
                       [fx.x, yAr.y], [far.tx0, yAr.y], [far.tx0, far.ty]],
                 bez: false, tx: far.tx0, ty: far.ty, back: dir === "up" };
        }
      }
    }
    if (!tr) tr = routeOrtho(A, Bf, far.sy, far.ty, far.sameRow, far.sx0,
      far.tx0, true, lanePad, yPad);
    // dominant type color: honest flow per type, ties by member count, name
    const tySum = new Map();
    a.forEach(x => tySum.set(x.wty, (tySum.get(x.wty) || 0) + x.flowSum));
    const tyCnt = new Map();
    a.forEach(x => tyCnt.set(x.wty, (tyCnt.get(x.wty) || 0) + 1));
    const domTy = [...tySum.keys()].sort((p, q) =>
      tySum.get(q) - tySum.get(p) || tyCnt.get(q) - tyCnt.get(p) ||
      (p < q ? -1 : 1))[0];
    const trunk = Object.assign({ s: far.s, t: far.t, wty: domTy, pair: far.pair,
      hub: "trunk", trunkW: a.length, flowSum }, tr);
    trunk.flow = dir === "same" ? "same" : dir;
    spines.push(trunk);   // strokes in the spine pass, before its taps
    // peel points: the trunk's horizontal run inside each rider row's
    // adjacent band (down: band above the row; up/same: band below it)
    const peels = [];
    rows.forEach(r => {
      const bi = dir === "down" ? r - 1 : r;
      const g = gapY[bi];
      if (!g || bi < 0) return;
      let seg = null;
      for (let i = 0; i < trunk.pts.length - 1; i++) {
        const p = trunk.pts[i], q = trunk.pts[i + 1];
        if (p[1] === q[1] && p[1] > g.y0 && p[1] < g.y1) {
          seg = { x0: Math.min(p[0], q[0]), x1: Math.max(p[0], q[0]), y: p[1] };
          break;
        }
      }
      if (!seg) {   // degenerate: vertical trunk through the band - peel at
        let best = null, bd = 1e9;   // the waypoint nearest the band centre
        trunk.pts.forEach(p => {
          const d = Math.abs(p[1] - (g.y0 + g.y1) / 2);
          if (d < bd) { bd = d; best = p; }
        });
        if (best) seg = { x0: best[0], x1: best[0], y: best[1] };
      }
      if (!seg) return;
      const mr = a.filter(x => x.tRow === r).map(x => x.tx0).sort((p, q) => p - q);
      const med = mr[Math.floor((mr.length - 1) / 2)];
      const px = seg.x1 - seg.x0 >= 8
        ? Math.max(seg.x0 + 2, Math.min(seg.x1 - 2, med)) : (seg.x0 + seg.x1) / 2;
      peels.push({ row: r, x: px, y: seg.y });
    });
    const busRec = { trunk, peels, members: a, taps: [] };
    // taps: ONE delivery rail per (bus x row), 7px off the trunk's channel
    // (side facing the targets), placed by the same nextY/usedY machinery.
    // Each tap = peel dot -> rail -> port drop: strict orthogonal, 2 bends,
    // no chamfers - the EDA "one thick trunk, thin perpendicular taps" read.
    // One rail instead of k fan polylines keeps channels - and the 4px
    // near-parallel budget - free for other traffic.
    const served = new Set();
    peels.forEach(P => {
      const bi = dir === "down" ? P.row - 1 : P.row;
      const g = gapY[bi];
      if (!g) return;
      const members = a.filter(sp => sp.tRow === P.row);
      const ports = members.map(sp => sp.tx0);
      // rail = 7px off the trunk channel, on the target-facing side. If that
      // would clamp onto the band floor (where exhausted traffic piles into
      // one gray mass), flip to the channel's other side instead - rails
      // must ladder, never join the floor pile.
      const off = dir === "down" ? 7 : -7;
      let railSeed = P.y + off;
      if (railSeed > g.y1 - 3 || railSeed < g.y0 + 3) railSeed = P.y - off;
      railSeed = Math.max(g.y0 + 3, Math.min(g.y1 - 3, railSeed));
      const rail = nextY(Math.min(P.x, Math.min(...ports)),
        Math.max(P.x, Math.max(...ports)), railSeed, g.y1, 5);
      members.forEach(sp => {
        spines.push({ s: sp.s, t: sp.t, wty: sp.wty, pair: sp.pair,
          hub: "tap", tapBus: busRec, bez: false, tx: sp.tx0, ty: sp.ty,
          back: false, flow: dir === "same" ? "up" : dir,
          pts: [[P.x, P.y], [P.x, rail.y], [sp.tx0, rail.y], [sp.tx0, sp.ty]] });
        busRec.taps.push(spines[spines.length - 1]);
        served.add(sp);
        hubTaps++;
      });
    });
    a.forEach(sp => {
      if (!served.has(sp)) return;       // unserved riders stroke solo below
      sp.con = true;                     // rider: chip carrier, no stroke
      sp.gLeader = trunk;
      sp.bus = busRec;
      sp.pts = [];
    });
    hubDots.push({ x: trunk.pts[0][0], y: trunk.pts[0][1], c: domTy });
    peels.forEach(p => hubDots.push({ x: p.x, y: p.y, c: domTy }));
    hubBuses.push(busRec);
    hubTrunks++; hubRiders += a.length; hubPeels += peels.length;
  });
  // band consolidation backstop (L-C): leftover cross-row corridors sharing
  // the same chunk-row hop still share ONE trunk - the leader's route at
  // combined width. Hub-bus riders and amber corridors are not eligible;
  // eligibility is sRow !== tRow (the old xb field died with its unordered
  // crossedBands(sy, ty) args - up-hauls always read as band-less).
  const spineGroups = new Map();
  spines.forEach(sp => {
    if (sp.amber || sp.bus || sp.con || sp.hub || sp.sameRow) return;
    const k = sp.sRow + ">" + sp.tRow;
    let a = spineGroups.get(k);
    if (!a) spineGroups.set(k, a = []);
    a.push(sp);
  });
  let trunkGroups = 0;
  spineGroups.forEach(a => {
    if (a.length < 2) return;
    trunkGroups++;
    a[0].trunkW = a.length;               // leader strokes at combined width
    a[0].flowSum = a.reduce((s, x) => s + x.flowSum, 0);   // honest flow
    for (let j = 1; j < a.length; j++) {
      a[j].con = true;                    // twin: chip carrier, no stroke
      a[j].gLeader = a[0];
      a[j].gIx = j;
    }
  });
  // routing pass: everyone still stroking gets its route now, in edges
  // order, AFTER the hub trunks/taps claimed their lanes (deterministic)
  spines.forEach(sp => {
    if (sp.con || sp.hub) return;
    const sr = routeOrtho(place.get(sp.s), place.get(sp.t), sp.sy, sp.ty,
      sp.sameRow, sp.sx0, sp.tx0, true);
    Object.assign(sp, sr);
  });
  // stroked-pair -> honest flow: the width driver mapPaint reads. Riders
  // are excluded - they stroke nothing, their ink rides the trunk.
  // zero-length waypoints (staircase corner artifacts) inflate bend counts
  // and segment censuses - collapse consecutive duplicate points
  spines.forEach(sp => {
    if (sp.pts.length < 2) return;
    const q = [sp.pts[0]];
    for (let k = 1; k < sp.pts.length; k++) {
      const l = q[q.length - 1];
      if (Math.hypot(sp.pts[k][0] - l[0], sp.pts[k][1] - l[1]) > 0.01) q.push(sp.pts[k]);
    }
    sp.pts = q;
  });
  const pairW = new Map();
  spines.forEach(sp => { if (!sp.con && sp.pts.length) pairW.set(sp.pair, sp.flowSum); });
  // 3) individual named wires (tier-2 top-1/pair): terminate ON their fn rows
  // Blueprint-reroute buses (2D twin of the 3D bus law): named wires of the
  // same type converging on ONE fn row (>=2) merge at a junction dot parked
  // in open air beside the destination box; members route to the junction
  // (arrowless), ONE shared stub delivers the whole bus into the fn row with
  // a single arrowhead. Shared-destination only (McGee & Dingliana 2012).
  const busGroups = new Map();
  indiv.forEach(w => {
    if (w.ty === "var") return;         // var wires keep their own dot terminus
    const k = w.df + "\x00" + w.dfn + "\x00" + w.ty;
    let a = busGroups.get(k);
    if (!a) busGroups.set(k, a = []);
    a.push(w);
  });
  const busOf = new Map();               // wire record -> its bus
  const buses = [];                      // junction records for paint + audit
  busGroups.forEach(a => {
    if (a.length < 2) return;
    const B = place.get(a[0].df);
    if (!B) return;
    const dRow = rowOf.get(a[0].df + "\x00" + a[0].dfn);
    if (dRow === undefined) return;      // row-less dests keep individual routes
    // approach side: count source boxes left vs right of the destination
    let Lc = 0, Rc = 0;
    a.forEach(w => {
      const A0 = place.get(w.sf);
      if (!A0) return;
      if (A0.x + A0.w <= B.x) Lc++;
      else if (A0.x >= B.x + B.w) Rc++;
    });
    const side = Lc >= Rc ? -1 : 1;
    const jy = B.y + NH + dRow * RH + RH / 2;      // destination row centre
    // junction must sit in open air: nudge outward twice, else scan the
    // inter-box gaps at 2px pads - depth-varied chunk-row neighbours sit
    // 10-15px apart, and the old 4px pads rejected the whole gap, leaving
    // 9-wire arrival fans where a bus belonged (P3 root cause)
    const jHit = (x, pad) => rects.some(r =>
      x > r.x0 - pad && x < r.x1 + pad && jy > r.y0 - 3 && jy < r.y1 + 3);
    let jx = side < 0 ? B.x - 14 : B.x + B.w + 14;
    if (jHit(jx, 4)) jx = side < 0 ? jx - 10 : jx + 10;
    if (jHit(jx, 4)) {
      let ok = false;
      for (let s = 4; s <= 44 && !ok; s += 2) {
        for (const dx of (side < 0 ? [-s, s] : [s, -s])) {
          const c = B.x + B.w * (side < 0 ? 0 : 1) + (side < 0 ? -14 : 14) + dx;
          if (!jHit(c, 2)) { jx = c; ok = true; break; }
        }
      }
      if (!ok) return;
    }
    const bus = { df: a[0].df, dfn: a[0].dfn, ty: a[0].ty,
                  x: jx, y: jy, n: a.length, wires: a };
    buses.push(bus);
    a.forEach(w => busOf.set(w, bus));
  });
  indiv.forEach(w => {
    const A = place.get(w.sf), B = place.get(w.df);
    if (!A || !B) return;
    const sRow = rowOf.get(w.sf + "\x00" + w.sfn);
    const dRow = rowOf.get(w.df + "\x00" + w.dfn);
    const sameRow = A.row === B.row;   // chunk-row truth: fd wraps (see spines)
    let sx0;
    if (sRow === undefined) {
      const sIx = outIx.get(w.sf) || 0; outIx.set(w.sf, sIx + 1);
      sx0 = A.x + A.w * (sIx + 1) / ((outN.get(w.sf) || 1) + 1);
    } else {
      const kk = w.sf + "_" + sRow;
      const sIx = rowIxOut.get(kk) || 0; rowIxOut.set(kk, sIx + 1);
      sx0 = A.x + A.w * (sIx + 1) / ((rowTotOut.get(kk) || 1) + 1);
    }
    const sy = sRow === undefined ? A.y + A.h : A.y + NH + (sRow + 1) * RH;
    const bus = busOf.get(w);
    if (bus) {
      // reroute member: source port -> junction dot. A 2px virtual box at
      // the junction keeps routeOrtho's lane/claim machinery authoritative.
      const JB = { x: bus.x - 1, w: 2, y: bus.y - 1, h: 2 };
      const wr = routeOrtho(A, JB, sy, bus.y, false, sx0, bus.x);
      wr.flow = bus.y > sy ? "down" : "up";
      wr.noArr = true;                   // the junction dot is the terminus
      wires.push(Object.assign({
        sf: w.sf, sfn: w.sfn, df: w.df, dfn: w.dfn, ty: w.ty, line: w.line,
        up: false, pair: w.sf + "_" + w.df,
      }, wr));
      return;
    }
    let tx0;
    if (dRow === undefined) {
      const tIx = inIx.get(w.df) || 0; inIx.set(w.df, tIx + 1);
      tx0 = B.x + B.w * (tIx + 1) / ((inN.get(w.df) || 1) + 1);
    } else {
      const kk = w.df + "_" + dRow;
      const tIx = rowIxIn.get(kk) || 0; rowIxIn.set(kk, tIx + 1);
      tx0 = B.x + B.w * (tIx + 1) / ((rowTotIn.get(kk) || 1) + 1);
    }
    const upW = !sameRow && B.y + B.h <= sy;
    const ty = dRow === undefined
      ? (sameRow || upW ? B.y + B.h : B.y)
      : (sameRow || upW ? B.y + NH + (dRow + 1) * RH
                        : B.y + NH + dRow * RH);
    // cardinal routing: two boxes side by side on the SAME row with a clear
    // corridor connect STRAIGHT ACROSS — exit one side edge, enter the other
    // (Unreal/Mermaid law: no dip-down-up detour for a horizontal neighbor).
    // A box standing between the pair blocks the corridor -> dip route.
    let wr;
    const side = B.x >= A.x + A.w ? 1 : (B.x + B.w <= A.x ? -1 : 0);
    const gapL = Math.min(A.x + A.w, B.x + B.w);
    const gapR = Math.max(A.x, B.x);
    const blocked = !side || rects.some(r =>
      r !== A && r !== B && sy >= r.y0 && sy <= r.y1 &&
      r.x1 > gapL && r.x0 < gapR);
    // the shortcut is same-row-only: side is x-only, so for cross-row pairs
    // it drew a straight line at the source port height through foreign boxes
    if (sameRow && !blocked) {
      const x0 = side > 0 ? A.x + A.w : A.x;
      const x1 = side > 0 ? B.x : B.x + B.w;
      const ch2 = Math.max(0, Math.min(8, Math.abs(x1 - x0) / 2));
      // straight-across rides the SOURCE row line; a hidden source fn exits
      // at the box BOTTOM, which can sit past the target row - jog onto the
      // row so the terminus never floats below the box (P2)
      const p = [[sx0, sy], [x0 + side * ch2, sy],
                 [x1 - side * ch2, sy], [tx0, sy]];
      if (Math.abs(ty - sy) > 0.5) p.push([tx0, ty]);
      wr = { pts: p,
             bez: false, tx: tx0, ty, back: false };
    } else wr = routeOrtho(A, B, sy, ty, sameRow, sx0, tx0);
    wr.flow = sameRow ? "same" : (ty > sy ? "down" : "up");
    wires.push(Object.assign({
      sf: w.sf, sfn: w.sfn, df: w.df, dfn: w.dfn, ty: w.ty, line: w.line,
      up: sameRow, pair: w.sf + "_" + w.df,
    }, wr));
  });
  // bus delivery stubs: one shared arrival per junction (Blueprint reroute
  // law: many in, one corridor, all arrive at the same end)
  buses.forEach(bus => {
    const B = place.get(bus.df);
    if (!B) return;
    const dRow = rowOf.get(bus.df + "\x00" + bus.dfn);
    if (dRow === undefined) return;
    const ty = B.y + NH + dRow * RH;            // fn-row top = delivery port
    const tx0 = B.x + B.w / 2;
    wires.push({
      sf: bus.df, sfn: bus.dfn, df: bus.df, dfn: bus.dfn, ty: bus.ty,
      line: -1, up: false, pair: bus.df + "_bus", stub: true, busN: bus.n,
      mates: bus.wires.map(m =>
        nodes[m.sf].label + "::" + m.sfn + " \u2192 @" + m.line),
      pts: [[bus.x, bus.y], [bus.x, ty], [tx0, ty]],
      bez: false, tx: tx0, ty, back: false, flow: "down",
    });
  });
  // quiet edges (addendum rule 5): no wire text by default - identity is
  // the pin a wire leaves from + the hover tooltip; chips carry bundles
  // ---- bundle chips (section 7 [F3]): typed "xN" micro-chips on the spine
  const chipAnchor = pts => {
    let tot = 0;
    for (let s = 0; s < pts.length - 1; s++)
      tot += Math.hypot(pts[s + 1][0] - pts[s][0], pts[s + 1][1] - pts[s][1]);
    let acc = 0;
    for (let s = 0; s < pts.length - 1; s++) {
      const d = Math.hypot(pts[s + 1][0] - pts[s][0], pts[s + 1][1] - pts[s][1]);
      if (acc + d >= tot / 2) {
        const t = (tot / 2 - acc) / (d || 1);
        return [pts[s][0] + (pts[s + 1][0] - pts[s][0]) * t,
                pts[s][1] + (pts[s + 1][1] - pts[s][1]) * t];
      }
      acc += d;
    }
    return pts[0];
  };
  const chips = [];
  const ridersOf = sps => sps.flatMap(sp =>
    (byPair.get(sp.pair) || []).filter(w => !indivSet.has(w)));
  // peel badges: ONE aggregate per (hub bus x delivery row), anchored at the
  // peel dot where that row's taps split off (EDA: badge at the tap). The
  // badge lists every named wire riding the trunk into that row; x1 badges
  // are suppressed - a single rider is the visible wire itself.
  hubBuses.forEach(bus => {
    bus.peels.forEach(P => {
      const members = bus.members.filter(m => m.tRow === P.row);
      const riders = ridersOf(members);
      if (riders.length < 2) return;
      const txt = "\u00d7" + riders.length;
      const cwid = txtW(txt) + 10;
      chips.push({ pair: null, s: bus.members[0].s, t: -1, row: P.row,
        peel: true, ty: bus.trunk.wty, n: riders.length,
        x: P.x - cwid / 2, y: P.y - 21, w: cwid, h: 14, wires: riders });
    });
    // origin badge: ONE per bus at the trunk's departure dot, n = TOTAL
    // riders - disambiguates the per-row peel badges (x4 at a row of a
    // 12-rider trunk now reads as tap-count vs pipe-count)
    const all = ridersOf(bus.members);
    if (all.length >= 2 && bus.trunk.pts && bus.trunk.pts.length) {
      const t0 = bus.trunk.pts[0];
      const txt0 = "\u00d7" + all.length;
      const cw0 = txtW(txt0) + 10;
      chips.push({ pair: null, s: bus.members[0].s, t: -1, row: -1,
        origin: true, ty: bus.trunk.wty, n: all.length,
        x: t0[0] - cw0 - 6, y: t0[1] - 20, w: cw0, h: 14, wires: all });
    }
  });
  // remaining stroked spines keep per-pair badges (L-C leaders aggregate
  // their twins' riders; singles list their own), same n>=2 gate
  const chipSpines = spines.filter(sp =>
    !sp.bus && !sp.con && sp.pts.length && !sp.hub);
  const donePair = new Set();
  chipSpines.forEach(sp => {
    if (donePair.has(sp.pair)) return;
    donePair.add(sp.pair);
    const grp = spineGroups.get(sp.sRow + ">" + sp.tRow);
    const members = grp && grp[0] === sp ? grp : [sp];
    const riders = ridersOf(members);
    if (riders.length < 2) return;
    const anc = chipAnchor(sp.pts);
    const txt = "\u00d7" + riders.length;
    const cwid = txtW(txt) + 10;
    chips.push({ pair: sp.pair, s: sp.s, t: sp.t, ty: sp.wty,
      n: riders.length, x: anc[0] - cwid / 2, y: anc[1] - 15,
      w: cwid, h: 14, wires: riders });
  });
  // badge de-overlap at LAYOUT time: first-clear grid over dy (band-side
  // first) x dx offsets against box rects and earlier chips (2px pad).
  // Deterministic in chips order; the paint ladder remains the last resort.
  chips.forEach((c, ci) => {
    const hitR = (x, y) =>
      rects.some(r => x - 2 < r.x1 && x + c.w + 2 > r.x0 &&
                      y - 2 < r.y1 && y + c.h + 2 > r.y0) ||
      chips.some((o, oi) => oi < ci &&
        x - 2 < o.x + o.w && x + c.w + 2 > o.x &&
        y - 2 < o.y + o.h && y + c.h + 2 > o.y);
    if (!hitR(c.x, c.y)) return;
    const xs = [0, c.w + 8, -(c.w + 8), 2 * (c.w + 8), -2 * (c.w + 8)];
    const ys = [0, 7, -7, 14, -14, 21, -21, 28, -28, 35, -35, 42];
    outer:
    for (const dy of ys) for (const dx of xs) {
      const nx = Math.max(6, Math.min(cwL - c.w - 6, c.x + dx));
      if (!hitR(nx, c.y + dy)) { c.x = nx; c.y += dy; break outer; }
    }
  });
  // wires + bus stubs: collapse zero-length waypoints (same law as spines -
  // the lone stray arrowhead read its direction off a zero-length tail)
  wires.forEach(w => {
    if (!w.pts || w.pts.length < 2) return;
    const q = [w.pts[0]];
    for (let k = 1; k < w.pts.length; k++) {
      const l = q[q.length - 1];
      if (Math.hypot(w.pts[k][0] - l[0], w.pts[k][1] - l[1]) > 0.01) q.push(w.pts[k]);
    }
    w.pts = q;
  });
  mapRects = [];
  place.forEach((p, i) => {
    const g = geo.get(i);
    const rc = { i, x: p.x, y: p.y, w: p.w, h: g.h, rows: [], more: null };
    if (g.roster) {
      g.roster.rows.forEach((r, k) => rc.rows.push({
        nm: r.nm, y0: p.y + NH + k * RH, y1: p.y + NH + (k + 1) * RH }));
      if (g.more.length)
        rc.more = { y0: p.y + NH + g.roster.rows.length * RH,
                    y1: p.y + NH + (g.roster.rows.length + 1) * RH };
    }
    mapRects.push(rc);
  });
  // route audit: numeric truth for the merge gate (VLM reads of 1.5px
  // curves are unreliable). Rebuilt on every layout build; cached repaints
  // keep the last build's numbers — they describe the same layout.
  const audit = { named: vw.length, indiv: indiv.length, admitted: E,
    spines: 0, underlays: underlays.length, wires: wires.length,
    trunkGroups, buses: buses.length,
    busW: buses.reduce((s, b) => s + b.n, 0),
    hubTrunks, hubTaps, hubRiders, hubPeels,
    bez: 0, back: 0, down: 0, up: 0, sameRow: 0 };
  // stroked spines only (riders carry pts:[] - their ink rides the trunk)
  spines.forEach(sp => { if (!sp.con && sp.pts.length) audit.spines++; });
  [underlays, spines, wires].forEach(arr => arr.forEach(rec => {
    if (!rec.pts.length && !rec.bez) return;   // rider stubs census nothing
    if (rec.bez) audit.bez++;
    if (rec.back) audit.back++;
    if (rec.flow === "same") audit.sameRow++;
    else if (rec.flow) audit[rec.flow]++;
  }));
  mapLayout = {
    key, sig, lit, edges, E, place, geo, rects, wires, spines, underlays,
    chips, rosterRows, expandedSet: expand, worldH, worldW: cwL, capNote,
    trunkGroups, trunkTotal: trunkGroups + hubTrunks, hubBuses, hubDots,
    pairW, chunkY, chunkRowH, audit, buses,
  };
  window.routeAudit = mapLayout.audit;
  mapConsumeCenterReq();
  mapPaint(ctx, dpr, cwView, chView, capNote);
}
// consume a pending center request: pan the box to pane center when it is
// meaningfully off-center (tol), then pulse it. Runs in BOTH mapRender
// paint paths (cache-hit + rebuild) so the aim survives layout caching.
// Pan only — mapZ untouched (refit owns zoom). Consumed exactly once, so
// it never fights later manual pans; the pulse fires even when the pan is
// skipped (box already centered).
function mapConsumeCenterReq() {
  if (mapCenterReq < 0) return;
  const rc = mapRects.find(r => r.i === mapCenterReq);
  mapCenterReq = -1;
  if (!rc) return;
  const cwView = mapPane.clientWidth || 440,
        chView = mapPane.clientHeight || innerHeight;
  const dx = (rc.x + rc.w / 2 - mapPX) * mapZ - cwView / 2;
  const dy = (rc.y + rc.h / 2 - mapPY) * mapZ - chView / 2;
  if (Math.abs(dx) > MAP_CENTER_TOL_PX || Math.abs(dy) > MAP_CENTER_TOL_PX) {
    mapPX += dx / mapZ;
    mapPY += dy / mapZ;
    mapClampView();
  }
  mapPulse = { i: rc.i, t0: performance.now() };
}
function mapPaint(ctx, dpr, cwView, chView, capNote) {
  const L = mapLayout;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0b0f14";
  ctx.fillRect(0, 0, cwView, chView);
  if (capNote) {
    ctx.fillStyle = "#546e7a";
    ctx.font = MAP_FONT(11);
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText("top " + MAP_MAX + " of " + capNote + " files (by connectivity)", cwView / 2, 8);
  }
  // window on the world: pan/zoom = pure transform of the cached layout
  ctx.setTransform(dpr * mapZ, 0, 0, dpr * mapZ, -mapPX * dpr * mapZ, -mapPY * dpr * mapZ);
  // disclosure dimming: L1 hover dims non-incident to 0.12; L3 freeze to 0.06
  const hov = mapHover >= 0 && L.wires[mapHover] ? L.wires[mapHover] : null;
  const dim = (a, b) => {
    if (mapFrozenIx >= 0) return (a === mapFrozenIx || b === mapFrozenIx) ? 1 : 0.06;
    if (hov) return (a === hov.sf || a === hov.df || b === hov.sf || b === hov.df) ? 1 : 0.12;
    return 1;
  };
  const seg = (rec, color, width, dash, alpha) => {
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    if (rec.bez) {
      ctx.moveTo(rec.pts[0][0], rec.pts[0][1]);
      ctx.bezierCurveTo(rec.c1[0], rec.c1[1], rec.c2[0], rec.c2[1],
                        rec.pts[1][0], rec.pts[1][1]);
    } else {
      rec.pts.forEach((p, k) => k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    }
    ctx.stroke();
  };
  // render order (section 9): underlays -> spines (taps, singles, trunks) ->
  // hub junction dots -> wires -> boxes/rosters -> labels/chips/terminators.
  // Underlay alpha 0.18 (declutter lever 5). Zoom-gated ink tiers: the fine
  // layers (underlays, named wires, port dots/arrowheads) hide when zoomed
  // out - PAINT-ONLY, the layout never changes (mapInkEval hysteresis).
  // Structure (spines, buses, junction dots, boxes, chips) stays on always.
  if (mapInkOn) L.underlays.forEach(u => {
    seg(u, MGLYPH[u.ty0] ? MGLYPH[u.ty0].c : MGLYPH.attach.c, 1,
        MGLYPH.attach.dash, 0.18 * dim(u.s, u.t));
    // T-junction terminator: short tick across the entry, no arrow
    ctx.globalAlpha = 0.18 * dim(u.s, u.t);
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(u.tx - 4, u.ty); ctx.lineTo(u.tx + 4, u.ty);
    ctx.stroke();
  });
  L.spines.forEach(sp => {
    if (sp.con || !sp.pts.length) return;  // riders: ink rides the trunk
    // wire color = TYPE (Blueprint law); wty carries it (the old build let
    // the y-coordinate overwrite the type, painting everything call-gray)
    const color = sp.amber ? "#ffb347" : (MGLYPH[sp.wty] || MGLYPH.call).c;
    if (sp.hub === "trunk") {
      // thick trunk: width reads the summed admitted weight (log2 taper,
      // cap 5.5), floored at 1.3 screen px so trunks stay fat when zoomed
      // out; full-alpha - the trunk IS the structure (InkKnobs #3)
      seg(sp, color, Math.max(Math.min(2 + 0.85 * Math.log2(sp.flowSum), 5.5), 1.3 / mapZ),
          sp.back ? [2, 3] : null, 0.78 * dim(sp.s, sp.t));
    } else if (sp.hub === "tap") {
      // thin tap at the rider's type color: access road, not corridor
      seg(sp, color, 1, null, 0.6 * dim(sp.s, sp.t));
    } else {
      // single spine / L-C trunk leader
      const lead = sp.trunkW >= 2;
      seg(sp, color, Math.min(2 + 0.85 * Math.log2(sp.flowSum), 5.5),
          sp.back ? [2, 3] : null, (lead ? 0.78 : 0.45) * dim(sp.s, sp.t));
    }
  });
  // hub junction dots (Blueprint reroute nodes): origin + peel points sit ON
  // the routed trunk/tap paths; drawn after the spine pass so converging ink
  // visually terminates ON the marker. Screen-size floor keeps them visible
  // when zoomed out (InkKnobs #16).
  ctx.setLineDash([]);
  (L.hubDots || []).forEach(d => {
    ctx.globalAlpha = 0.95;
    ctx.beginPath();
    ctx.arc(d.x, d.y, Math.max(3.2, 2.5 / mapZ), 0, Math.PI * 2);
    ctx.fillStyle = (MGLYPH[d.c] || MGLYPH.call).c;
    ctx.fill();
    ctx.strokeStyle = "#0a0e12";
    ctx.lineWidth = 1;
    ctx.stroke();
  });
  if (mapInkOn) L.wires.forEach(w => {
    const g = MGLYPH[w.ty] || MGLYPH.call;
    // backward edges (against flow gravity) read as dashed; type color kept
    seg(w, g.c, 1.5, w.back ? [2, 3] : g.dash, 0.9 * dim(w.sf, w.df));
  });
  // boxes + rosters
  ctx.setLineDash([]);
  L.lit.forEach(i => {
    const p = L.place.get(i), g = L.geo.get(i);
    if (!p || !g) return;   // pinned subject can outlive the placer (belt)
    const c = mapCols(nodes[i].cluster);
    const a = dim(i);
    const isSel = i === mapFrozenIx;   // amber = selection emphasis (L3 freeze)
    ctx.globalAlpha = a;
    ctx.fillStyle = c.f;
    ctx.strokeStyle = isSel ? "#ffb347" : c.s;
    ctx.lineWidth = isSel ? 3 : 1.5;
    const rad = 6;
    ctx.beginPath();
    ctx.moveTo(p.x + rad, p.y);
    ctx.arcTo(p.x + p.w, p.y, p.x + p.w, p.y + g.h, rad);
    ctx.arcTo(p.x + p.w, p.y + g.h, p.x, p.y + g.h, rad);
    ctx.arcTo(p.x, p.y + g.h, p.x, p.y, rad);
    ctx.arcTo(p.x, p.y, p.x + p.w, p.y, rad);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = c.t;
    ctx.font = MAP_FONT(11);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(nodes[i].label, p.x + p.w / 2, p.y + 11 + 0.5);
    if (g.roster) {
      ctx.font = MAP_FONT(10);
      ctx.textAlign = "left";
      g.roster.rows.forEach((r, k) => {
        const ry = p.y + 22 + k * RH + RH / 2 + 0.5;
        ctx.globalAlpha = a;
        ctx.fillStyle = c.t;
        ctx.fillText(r.nm, p.x + 8, ry);
        if (r.io && r.io.w.length) {   // writes badge (section 4)
          ctx.fillStyle = "#80cbc4";
          ctx.textAlign = "right";
          ctx.fillText("\u270e" + r.io.w.length, p.x + p.w - 6, ry);
          ctx.textAlign = "left";
        }
      });
      if (g.roster.more.length) {
        ctx.fillStyle = "#546e7a";
        ctx.fillText("+" + g.roster.more.length + " more...",
          p.x + 8, p.y + 22 + g.roster.rows.length * RH + RH / 2 + 0.5);
      }
    }
  });
  // chips (click -> pinned enumeration list) + labels share ONE screen-space
  // collision ladder: overlap with boxes, chips or earlier labels = slide
  // down a few steps, still colliding = hidden. Never on top of text.
  const m2s = (x, y) => ({ x: (x - mapPX) * mapZ, y: (y - mapPY) * mapZ });
  const taken2 = [];
  mapRects.forEach(rc => {
    const a = m2s(rc.x, rc.y);
    taken2.push({ x: a.x, y: a.y, w: rc.w * mapZ, h: rc.h * mapZ });
  });
  const hit2 = (r) => taken2.some(t =>
    r.x < t.x + t.w && r.x + r.w > t.x && r.y < t.y + t.h && r.y + r.h > t.y);
  const place2 = (x, y, w, h) => {
    // slide ladder with an X leg: badges first try straight up/down off the
    // ink, then slide ALONG their anchor row (screen px) to escape box edges
    // and neighboring badges - a chip with no vertical room still finds a
    // home beside the trunk instead of overlapping (D4)
    for (const dy of [0, -10, 10, -20, 20, -30, 30, -40])
      for (const dx of [0, 14, -14, 28, -28, 42, -42]) {
        const r = { x: x + dx - w / 2, y: y + dy * mapZ - h / 2, w, h };
        if (!hit2(r)) { taken2.push(r); return { dy, dx }; }
      }
    return null;   // no room: hide rather than stack
  };
  const inView = (x, y) => {
    const sx = (x - mapPX) * mapZ, sy = (y - mapPY) * mapZ;
    return sx >= -30 && sx <= cwView + 30 && sy >= -10 && sy <= chView + 10;
  };
  ctx.font = MAP_FONT(10);
  // selection pulse (paint-only): amber rounded-rect breathing around the
  // clicked node's box — inflates and fades over MAP_PULSE_MS, tick()
  // drops it at end of life. Never touches layout or picking.
  if (mapPulse) {
    const prc = mapRects.find(r => r.i === mapPulse.i);
    if (prc) {
      const t = (performance.now() - mapPulse.t0) / MAP_PULSE_MS;
      if (t >= 0 && t <= 1) {
        const inf = (4 + 10 * t) / mapZ;
        ctx.globalAlpha = 0.9 * (1 - t);
        ctx.strokeStyle = "#ffb347";
        ctx.lineWidth = 3 / mapZ;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(prc.x - inf, prc.y - inf,
                                         prc.w + 2 * inf, prc.h + 2 * inf, 6);
        else ctx.rect(prc.x - inf, prc.y - inf,
                      prc.w + 2 * inf, prc.h + 2 * inf);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }
  }
  L.chips.forEach(ch => {
    const g = MGLYPH[ch.ty] || MGLYPH.call;
    const sw = ch.w * mapZ, sh = ch.h * mapZ;
    const a = m2s(ch.x + ch.w / 2, ch.y + ch.h / 2);
    if (!inView(ch.x, ch.y)) return;
    const p2 = place2(a.x, a.y, sw, sh);
    if (p2 === null) return;
    const dyW = p2.dy / mapZ, dxW = p2.dx / mapZ;  // screen px -> world px
    // zoom fade (InkKnobs #7): badges dissolve below z~0.45, solid by 0.70 -
    // at overview zoom they were unreadable smudges doubling the wire count
    const zf = Math.max(0, Math.min(1, (mapZ - 0.45) / 0.25));
    if (zf <= 0) return;
    ctx.globalAlpha = dim(ch.s, ch.t) * zf;
    ctx.fillStyle = "rgba(8,12,16,.85)";
    ctx.strokeStyle = g.c;
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(ch.x + dxW, ch.y + dyW, ch.w, ch.h, 4);
    else ctx.rect(ch.x + dxW, ch.y + dyW, ch.w, ch.h);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = g.c;
    ctx.font = "bold " + MAP_FONT(10);
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("\u00d7" + ch.n, ch.x + dxW + ch.w / 2, ch.y + dyW + ch.h / 2 + 0.5);
    ctx.font = MAP_FONT(10);
  });
  // terminators last so arrowheads/dots sit on the box edges (section 9);
  // ink-tier gated with the wires themselves - invisible wires wear no
  // arrowheads
  ctx.setLineDash([]);
  if (mapInkOn) L.wires.forEach(w => {
    const g = MGLYPH[w.ty] || MGLYPH.call;
    ctx.globalAlpha = dim(w.sf, w.df);
    // port dot: the wire leaves from a NAMED row — pin the origin (the
    // arrowhead marks the destination; the dot marks where it starts).
    // Bus stubs start at the junction dot — no port dot there.
    ctx.setLineDash([]);
    if (!w.stub) {
      ctx.beginPath();
      ctx.arc(w.pts[0][0], w.pts[0][1], 2.2, 0, Math.PI * 2);
      ctx.fillStyle = g.c;
      ctx.fill();
    }
    if (w.noArr) return;   // bus member: the junction dot is the terminus
    // arrowhead points along the FINAL segment's cardinal direction —
    // horizontal entries get side arrows, drops get up/down arrows
    const pv = w.pts[w.pts.length - 2];
    const adx = w.tx - pv[0], ady = w.ty - pv[1];
    const horiz = Math.abs(adx) > Math.abs(ady);
    if (g.term === "tri" || g.term === "hollow") {
      ctx.beginPath();
      if (horiz) {
        const s = adx > 0 ? 1 : -1;
        ctx.moveTo(w.tx, w.ty);
        ctx.lineTo(w.tx - s * 8, w.ty - 4.5);
        ctx.lineTo(w.tx - s * 8, w.ty + 4.5);
      } else {
        const off = ady > 0 ? -8 : 8;   // arriving from above -> points down
        ctx.moveTo(w.tx, w.ty);
        ctx.lineTo(w.tx - 4.5, w.ty + off);
        ctx.lineTo(w.tx + 4.5, w.ty + off);
      }
      ctx.closePath();
      if (g.term === "tri") { ctx.fillStyle = g.c; ctx.fill(); }
      else { ctx.strokeStyle = g.c; ctx.lineWidth = 1.5; ctx.stroke(); }
    } else if (g.term === "dot") {
      ctx.beginPath();
      ctx.arc(w.tx, w.ty, 3, 0, Math.PI * 2);
      ctx.fillStyle = g.c;
      ctx.fill();
    }
  });
  // bus junction dots (Blueprint reroute nodes): drawn after wires so the
  // converging lanes visually terminate ON the marker
  (L.buses || []).forEach(bs => {
    const g = MGLYPH[bs.ty] || MGLYPH.call;
    ctx.globalAlpha = 0.95;
    ctx.beginPath();
    ctx.arc(bs.x, bs.y, Math.max(3.2, 2.5 / mapZ), 0, Math.PI * 2);
    ctx.fillStyle = g.c;
    ctx.fill();
    ctx.strokeStyle = "#0a0e12";
    ctx.lineWidth = 1;
    ctx.stroke();
  });
  ctx.globalAlpha = 1;
  // screen-space furniture: map-local vars chip [F10] + footer
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  mapVarsChipRect = { x: 8, y: 26, w: 46, h: 16 };
  ctx.fillStyle = mapVarsOn ? "rgba(29,233,182,.25)" : "rgba(8,12,16,.85)";
  ctx.strokeStyle = mapVarsOn ? "#1de9b6" : "#263238";
  ctx.lineWidth = 1;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(8, 26, 46, 16, 4);
  else ctx.rect(8, 26, 46, 16);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = mapVarsOn ? "#1de9b6" : "#78909c";
  ctx.font = MAP_FONT(10);
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText("vars", 31, 34.5);
  ctx.fillStyle = "#546e7a";
  ctx.textAlign = "left"; ctx.textBaseline = "bottom";
  let foot = "\u25b2 in  \u25bc out  \u2014 call  \u2013 signal  \u00b7 var";
  const mr = DATA.meta || {};
  if (mr.sig_unresolved)
    foot += "  |  sig " + (mr.sig_resolved || 0) + " ok / " +
            mr.sig_unresolved + " unres";
  ctx.fillText(foot, 8, chView - 6);
}
// keep the window inside the world: pan/zoom can never strand the layout
// off-screen (drag = pan must stay recoverable at every zoom)
function mapClampView() {
  if (!mapLayout) return;
  const cwView = mapPane.clientWidth || 440, chView = mapPane.clientHeight || innerHeight;
  // window clamp; world smaller than the window => negative bounds, and the
  // position snaps to the centered midpoint (far-out orientability: content
  // floats mid-pane with equal dead space, never corner-stranded)
  const clampC = (p, world, view) => {
    const lo = Math.min(0, world - view / mapZ), hi = Math.max(0, world - view / mapZ);
    p = Math.max(lo, Math.min(hi, p));
    return hi === 0 && lo < 0 ? (lo + hi) / 2 : p;
  };
  mapPX = clampC(mapPX, mapLayout.worldW || MAP_WORLD_W, cwView);
  mapPY = clampC(mapPY, mapLayout.worldH || 0, chView);
}
// rAF dirty-flag single draw (section 5 [F7]): every caller coalesces here
function drawMapPane() {
  if (!mapVisible || mapDirty) return;
  mapDirty = true;
  requestAnimationFrame(() => {
    mapDirty = false;
    try { mapRender(); }
    catch (err) { console.warn("map render failed:", err); }
  });
}
document.getElementById("bMap").onclick = () => setMapVisible(!mapVisible);
// mapVisible drives the whole split: pane + divider visibility, info-panel
// shift, 3D region size. Single entry point — boot and #bMap both use it.
function setMapVisible(v) {
  mapVisible = v;
  document.getElementById("bMap").classList.toggle("on", v);
  mapPane.classList.toggle("collapsed", !v);
  divider.classList.toggle("collapsed", !v);
  document.body.classList.toggle("mapOpen", v);
  // keep the node info panel clear of the pane instead of underneath it
  info.classList.toggle("mapShift", v);
  resize3D();
  if (v) { sizeMapPane(); drawMapPane(); }
  else { mapTipHide(); mapOvCloseOne(); mapCenterReq = -1; mapPulse = null; }
}
// ---- divider drag: resize the split (rAF-throttled), never orbits the 3D ---
// the divider is its own element — OrbitControls listens on the canvas only,
// so a drag here cannot start a camera move by construction
const divider = document.getElementById("divider");
let divRaf = 0;
divider.addEventListener("pointerdown", e => {
  if (!mapVisible) return;
  divider.setPointerCapture(e.pointerId);
  divider.classList.add("drag");
  e.preventDefault();
});
divider.addEventListener("pointermove", e => {
  if (!divider.classList.contains("drag")) return;
  paneW = Math.round(Math.max(PANE_MIN, Math.min(paneMax(), innerWidth - e.clientX)));
  applyPaneW();
  if (!divRaf) divRaf = requestAnimationFrame(() => {
    divRaf = 0;
    resize3D();
    sizeMapPane();
    drawMapPane();   // rAF dirty-flag coalesces paints across drag frames
  });
});
divider.addEventListener("pointerup", () => {
  if (!divider.classList.contains("drag")) return;
  divider.classList.remove("drag");
  try { localStorage.setItem(PANE_KEY, String(paneW)); } catch {}
});
divider.addEventListener("pointercancel", () => divider.classList.remove("drag"));
// click a node rect = the hub-label jump: re-seed focus around that file
const mapToWorld = e => {
  const b = mapPane.getBoundingClientRect();
  return { x: (e.clientX - b.left) / mapZ + mapPX, y: (e.clientY - b.top) / mapZ + mapPY };
};
mapPane.addEventListener("wheel", e => {
  if (!mapVisible) return;
  e.preventDefault();
  mapClosePick();   // canvas input closes the picker [F11]
  const b = mapPane.getBoundingClientRect();
  const cx = e.clientX - b.left, cy = e.clientY - b.top;
  const wx = cx / mapZ + mapPX, wy = cy / mapZ + mapPY;
  mapZ = Math.max(0.2, Math.min(3, mapZ * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
  mapInkEval();   // hysteresis re-arm at the new zoom (paint-only tier)
  mapPX = wx - cx / mapZ;
  mapPY = wy - cy / mapZ;
  mapClampView();
  drawMapPane();
}, { passive: false });
mapPane.addEventListener("pointerdown", e => {
  if (!mapVisible) return;
  mapClosePick();   // canvas input closes the picker [F11]
  mapDrag = { x: e.clientX, y: e.clientY, px: mapPX, py: mapPY, moved: false };
  mapPane.setPointerCapture(e.pointerId);
});
mapPane.addEventListener("pointerup", () => {
  if (mapDrag) { mapDragged = mapDrag.moved; mapDrag = null; }
});
mapPane.addEventListener("click", e => {
  if (mapDragged) { mapDragged = false; return; }   // it was a pan, not a pick
  clearTimeout(mapRefocusTimer);
  // 0. screen-space furniture first: vars chip [F10]
  const mb = mapPane.getBoundingClientRect();
  const mpx = e.clientX - mb.left, mpy = e.clientY - mb.top;
  const cr = mapVarsChipRect;
  if (cr && mpx >= cr.x && mpx <= cr.x + cr.w && mpy >= cr.y && mpy <= cr.y + cr.h) {
    mapVarsOn = !mapVarsOn;
    drawMapPane();
    return;
  }
  const w = mapToWorld(e);
  // 1. bundle chip -> pinned enumeration list (section 7)
  const ci = mapChipAt(w.x, w.y);
  if (ci >= 0) { mapOpenList(ci); return; }
  // named wire vs boxes (L2): a wire within the 6px screen tolerance wins
  // over box-header refocus - wires visibly ride box borders (the failing
  // aim sat exactly ON a box's bottom edge) - unless the click genuinely
  // lands inside the header band (title strip = top NH px; a roster-less
  // box is all header). Row/picker zones yield to the wire too: the
  // tolerance is screen-fine and wires terminate at row pins on borders.
  const wi = mapWireAt(w.x, w.y);
  if (wi >= 0 && mapLayout) {
    let inHeader = false;
    for (let k = mapRects.length - 1; k >= 0; k--) {
      const rc = mapRects[k];
      if (w.x < rc.x || w.x > rc.x + rc.w || w.y < rc.y || w.y > rc.y + rc.h) continue;
      inHeader = w.y < rc.y + ((rc.rows.length || rc.more) ? NH : rc.h);
      break;
    }
    if (!inHeader) {
      const wr = mapLayout.wires[wi];
      if (wr.ty === "var") showInfo(wr.df);   // member target is not a fn
      else mapShowFn(wr.df, wr.dfn);
      return;
    }
  }
  // 2. boxes: roster row (L3) / "+N more" (picker) / header (click refocus).
  // Tested before wires: boxes paint on top of them. Rects iterate
  // topmost-drawn first so an overlap resolves to the box the user sees.
  for (let k = mapRects.length - 1; k >= 0; k--) {
    const rc = mapRects[k];
    if (w.x < rc.x || w.x > rc.x + rc.w || w.y < rc.y || w.y > rc.y + rc.h) continue;
    if (rc.rows.length) {
      if (rc.more && w.y >= rc.more.y0 && w.y < rc.more.y1) { mapOpenPicker(rc); return; }
      for (const row of rc.rows) {
        if (w.y >= row.y0 && w.y < row.y1) {
          // L3 [F12]: local dim only, rows frozen, NO re-seed
          mapFrozenIx = mapFrozenIx === rc.i ? -1 : rc.i;
          drawMapPane();
          return;
        }
      }
    }
    // Header click = refocus (220ms so a dblclick can cancel into an
    // expand toggle). A wire within tolerance wins over the header
    // unless the point is genuinely inside the header band: wires route
    // through box bodies, and an L2 target must not be stolen by a box
    // it merely passes under.
    if (w.y >= rc.y + NH) continue;
    mapRefocusTimer = setTimeout(() => {
      pushFocusState(); showInfo(rc.i); focusSeeds.clear(); focusSeeds.add(rc.i);
      applyVisibility(); focus(rc.i);
    }, 220);
    return;
  }
  // void: unpin the list, close the picker, drop the freeze
  if (mapListEl.style.display === "block" || mapFrozenIx >= 0) mapOvCloseOne();
});
mapPane.addEventListener("dblclick", e => {
  if (!mapVisible) return;
  clearTimeout(mapRefocusTimer);
  const w = mapToWorld(e);
  for (const rc of mapRects) {
    if (w.x >= rc.x && w.x <= rc.x + rc.w && w.y >= rc.y && w.y <= rc.y + rc.h) {
      const open = mapLayout && mapLayout.expandedSet.has(rc.i);
      mapExpandUser.set(rc.i, !open);
      drawMapPane();
      return;
    }
  }
});
mapPane.addEventListener("pointermove", e => {
  if (!mapVisible) return;
  if (mapDrag) {
    const dx = e.clientX - mapDrag.x, dy = e.clientY - mapDrag.y;
    if (Math.hypot(dx, dy) > 4) mapDrag.moved = true;
    mapPX = mapDrag.px - dx / mapZ;
    mapPY = mapDrag.py - dy / mapZ;
    mapClampView();
    drawMapPane();
    return;
  }
  const w = mapToWorld(e);
  const ci = mapChipAt(w.x, w.y);
  const wi = ci < 0 && mapFrozenIx < 0 ? mapWireAt(w.x, w.y) : -1;
  if (mapHover !== wi) { mapHover = wi; drawMapPane(); }
  mapHoverChip = ci;
  mapPane.style.cursor = ci >= 0 || wi >= 0 || mapRects.some(rc =>
    w.x >= rc.x && w.x <= rc.x + rc.w && w.y >= rc.y && w.y <= rc.y + rc.h)
    ? "pointer" : "default";
  if (wi >= 0) {   // map-local tooltip (L1)
    mapTipEl.textContent = mapTipText(mapLayout.wires[wi]);
    mapTipEl.style.display = "block";
    const b = mapPane.getBoundingClientRect();
    mapTipEl.style.left = Math.max(4, Math.min(e.clientX - b.left + 14, mapPane.clientWidth - 290)) + "px";
    mapTipEl.style.top = Math.max(4, Math.min(e.clientY - b.top + 10,
      (b.height || innerHeight) - 100)) + "px";
  } else mapTipHide();
});
addEventListener("resize", () => { if (mapVisible) { sizeMapPane(); drawMapPane(); } });
// test/debug surface: named-wire map introspection (harness contract)
const mapInfo = () => {
  if (!mapLayout) return null;
  const w0 = mapLayout.wires[0];
  let probe = null;
  if (w0) {   // midpoint of wire 0 in screen space (click-target probe)
    const m = w0.pts[Math.floor(w0.pts.length / 2)];
    probe = { sx: (m[0] - mapPX) * mapZ, sy: (m[1] - mapPY) * mapZ };
  }
  return {
    E: mapLayout.E,
    boxIxs: mapRects.map(r => r.i),
    wires: mapLayout.wires.length,
    chips: mapLayout.chips.length,
    rosterRows: mapLayout.rosterRows,
    expanded: mapLayout.expandedSet.size,
    // corridor trunk consolidation: admitted corridor polylines vs the
    // count actually stroked (riders con onto hub trunks / L-C leaders).
    // trunkGroups = umbrella (hub buses + L-C row-hop groups) so the gate
    // reads the whole trunk system, not one layer.
    spineTotal: mapLayout.spines.length,
    spinesDrawn: mapLayout.spines.filter(sp => !sp.con && sp.pts.length).length,
    trunkGroups: mapLayout.trunkTotal || mapLayout.trunkGroups || 0,
    hubTrunks: mapLayout.hubTrunks !== undefined ? mapLayout.hubTrunks
      : (mapLayout.audit ? mapLayout.audit.hubTrunks : 0) || 0,
    drawnPolys: mapLayout.underlays.length +
      mapLayout.spines.filter(sp => !sp.con && sp.pts.length).length +
      mapLayout.wires.length,
    probeWire: probe,
  };
};
document.getElementById("bGround").onclick = e => {
  showGround = !showGround;
  groundGrid.visible = showGround;
  e.target.classList.toggle("on", showGround);
};
const searchEl = document.getElementById("search");
const depthEl = document.getElementById("depth");
const cbFnEl = document.getElementById("cbFn");
fnMode = cbFnEl.checked;   // checkbox is the truth; sync the flag at boot
const cbSpinEl = document.getElementById("cbSpin");
cbSpinEl.addEventListener("change", () => { spinEnabled = cbSpinEl.checked; });
const bDeadEl = document.getElementById("bDead");
searchEl.oninput = e => {
  query = e.target.value.toLowerCase();
  // a fn-name hit implies satellite interest: auto-enable the fn layer
  if (query.length >= 2 && !fnMode &&
      fnNames.some(nm => nm.toLowerCase().includes(query))) {
    fnMode = true;
    document.getElementById("cbFn").checked = true;
  }
  applyVisibility();
  // search-focus must land framed, not stranded in the overview: the ring,
  // the budget fan and the info panel all read at ball distance. Same law
  // as the click path (focus() frames the compact ball).
  frameQueryCamera();
};
depthEl.oninput = e => { depth = +e.target.value; document.getElementById("depthVal").textContent = depth; applyVisibility(); };
document.getElementById("spread").oninput = e => {
  const v = +e.target.value / 100;
  document.getElementById("spreadVal").textContent = v.toFixed(1);
  applySpread(v);
};
document.querySelectorAll("#dirRow .seg").forEach(b => {
  b.onclick = () => {
    dirMode = +b.dataset.d;
    document.querySelectorAll("#dirRow .seg").forEach(x =>
      x.classList.toggle("on", x === b));
    applyVisibility();
  };
});
document.getElementById("cbFn").onchange = e => { fnMode = e.target.checked; applyVisibility(); };
// cluster supernode collapse: toggle owns collapsed + the fn layer (the fn
// wires reference individual member files, meaningless once members merge;
// forced off and restored across the round-trip). applyVisibility runs the
// collapse pass (alphaTgt/dpos/supernode matrices), then the containment
// rebuild picks up the " · N files" labels.
document.getElementById("bCollapse").onclick = e => {
  collapsed = !collapsed;
  e.target.classList.toggle("on", collapsed);
  if (collapsed) { fnWasOn = fnMode; fnMode = false; cbFnEl.checked = false; }
  else { fnMode = fnWasOn; cbFnEl.checked = fnWasOn; fnWasOn = false; }
  applyVisibility();
  buildContainment();
};
function clearFocus() {
  // one scope for Esc / right-click / crumb ✕: drop the focus, the query,
  // the back-stack and the info panel together
  focusSeeds.clear(); query = "";
  focusStack = [];
  document.getElementById("search").value = "";
  info.style.display = "none";
  applyVisibility();
  frameGraph();   // the camera followed the focus in; it follows the reset out
}
addEventListener("keydown", e => {
  if (e.key === "Escape" && wireTipEl.style.display !== "none") { hideWireTip(); return; }
  if (e.key === "Escape" && mapOvCloseOne()) return;   // map overlays own ESC first
  if (e.key === "Escape" && (focusSeeds.size || query)) clearFocus();
  else if (e.key === "Backspace" && e.target !== searchEl &&
    focusSeeds.size && focusStack.length) {
    e.preventDefault();
    popFocus();
  }
});
// right-click (not a drag) exits node focus
renderer.domElement.addEventListener("contextmenu", e => {
  e.preventDefault();
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (focusSeeds.size || query || info.style.display !== "none") clearFocus();
});
// reset owns EVERY piece of UI state — one click must return the app to
// its boot state with nothing half-reset (vars and classes in lockstep)
function resetAll() {
  activeClusters.clear(); activeDirs.clear();
  deadOnly = false; cycOnly = false; query = ""; focusSeeds.clear(); focusStack = [];
  dirMode = 0; showSignals = true; showVar = false; fnMode = false; depth = 1;
  mutOnly = false;
  showInst = false; showCalls = true; showTests = false; showGhost = false;
  groupsMode = false;   // coloring level is view state — reset to fine clusters
  collapsed = false; fnWasOn = false;   // supernode collapse off — resetAll's button wipe clears its .on
  searchEl.value = ""; depthEl.value = 1;
  document.getElementById("depthVal").textContent = "1";
document.getElementById("spread").value = 100;
document.getElementById("spreadVal").textContent = "1.0";
applySpread(1);
  cbFnEl.checked = false;
  info.style.display = "none";
  camTween = null;
  camera.position.set(0, 0, 1400); controls.target.set(0, 0, 0);
  document.querySelectorAll(".chip, button").forEach(x => x.classList.remove("on"));
  document.getElementById("bCalls").classList.add("on");
  document.getElementById("bSignals").classList.add("on");
  document.getElementById("bMut").classList.remove("on");
  document.querySelector("#dirRow .seg").classList.add("on");
  // ground is a viewport pref, not filter state - it survives the reset
  if (showGround) document.getElementById("bGround").classList.add("on");
  // the map pane is a viewport pref too — keep its button in lockstep
  if (mapVisible) document.getElementById("bMap").classList.add("on");
  // ...but its content state resets with everything else
  mapExpandUser.clear(); mapFrozenIx = -1; mapHover = -1; mapVarsOn = false;
  mapOvCloseOne();
  frameGraph();
  buildLegend();
  buildContainment(); applyVisibility();
}
document.getElementById("bReset").onclick = resetAll;

const info = document.getElementById("info");
let panelCopyText = "";   // res:// target of whatever the info panel shows
function copyPanelPath() {
  const t = panelCopyText;
  if (!t) return;
  const done = () => {
    const b = document.getElementById("iCopy");
    b.textContent = "✓";
    setTimeout(() => { b.textContent = "⧉"; }, 900);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done, done);
  } else {
    // file:// pages may lack the async clipboard API
    const ta = document.createElement("textarea");
    ta.value = t; document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (err) {}
    ta.remove(); done();
  }
}
document.getElementById("iCopy").onclick = copyPanelPath;

// render one directed section of the connections panel; entries beyond 24
// collapse into an explicit "+N more hidden" footer (never silently)
function renderSection(kindId, ulId, entries, degArr, degWord, onJump) {
  const arr = [...entries.values()].sort((a, b) => b.w - a.w);
  document.getElementById(kindId).textContent =
    kindId === "kUses" ? `USES (${arr.length})` : `USED BY (${arr.length})`;
  const ul = document.getElementById(ulId);
  ul.innerHTML = "";
  const shown = arr.slice(0, 24);
  shown.forEach(({ j, rel, w, vis }) => {
    const li = document.createElement("li");
    li.textContent = `${nodes[j].label} · ${rel} ×${w} · ${degArr[j]} ${degWord}`;
    if (!vis) li.style.opacity = 0.45;
    li.onclick = () => onJump(j);
    ul.appendChild(li);
  });
  if (arr.length > shown.length) {
    const li = document.createElement("li");
    li.className = "more";
    li.textContent = `+${arr.length - shown.length} more hidden`;
    ul.appendChild(li);
  }
}
function showInfo(i) {
  const n = nodes[i];
  panelCopyText = "res://" + n.path;
  info.style.display = "block";
  document.getElementById("iTitle").textContent = n.label;
  document.getElementById("iSub").textContent = n.dir || "/";
  const tags = document.getElementById("iTags");
  tags.innerHTML = "";
  const mk = (txt, color) => {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = txt;
    t.style.background = color + "22"; t.style.color = color;
    tags.appendChild(t);
  };
  mk(n.ext, "#80cbc4");
  if (n.cls) mk(n.cls, "#ce93d8");
  if (n.cluster >= 0) mk("cluster c" + n.cluster, `#${new THREE.Color().setHSL(hue(n.cluster),0.72,lightOf(n.cluster)).getHexString()}`);
  if (n.dl || n.dead > 0) mk(n.dl ? "likely dead" : "maybe dead", "#ef5350");
  // directed halves: USES = edges this file sends, USED BY = edges it receives
  const outs = new Map(), ins = new Map();
  links.forEach(l => {
    // list every relationship type regardless of the view toggles;
    // entries whose type is currently toggled off render dimmed
    let j, rel, tgt;
    if (l.s === i) { j = l.t; rel = l.ty === "inst" ? "contains" : l.ty === "attach" ? "attaches" : l.ty; tgt = outs; }
    else if (l.t === i) { j = l.s; rel = l.ty === "inst" ? "part of" : l.ty === "attach" ? "used by" : l.ty; tgt = ins; }
    else return;
    const key = j + "|" + rel;
    const cur = tgt.get(key) || { j, rel, w: 0, vis: false };
    cur.w += l.w;
    if (typeVisible(l.ty)) cur.vis = true;
    tgt.set(key, cur);
  });
  const jump = j => { pushFocusState(); showInfo(j); focusSeeds.clear(); focusSeeds.add(j); applyVisibility(); focus(j); };
  renderSection("kUses", "iUses", outs, outDeg, "downstream", jump);
  renderSection("kUsedBy", "iUsedBy", ins, inDeg, "upstream", jump);
}
function focus(i) {
  // tween the camera around node i (400ms ease-out). Distance frames the
  // COMPACT BALL, not a fixed 320: the ball's radius scales with the
  // neighborhood, and a fixed distance parks the camera INSIDE it — the
  // hub ring and the lit fan end up clipped off-screen. focus() callers
  // all run applyVisibility() first, so compactTgt is the landed layout.
  const to = new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]);
  const dir = new THREE.Vector3(camera.position.x - to.x,
    camera.position.y - to.y, camera.position.z - to.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  let r = 0;
  if (compactTgt && compactIdx && compactIdx.length) {
    const rad = sphR(i);
    for (let q = 0; q < compactIdx.length; q++) {
      const j = compactIdx[q];
      const d = Math.hypot(compactTgt[j*3] - to.x, compactTgt[j*3+1] - to.y,
        compactTgt[j*3+2] - to.z) + rad;
      if (d > r) r = d;
    }
  }
  tweenCamTo(to, to.clone().addScaledVector(dir, Math.max(320, r * 2.1)));
}

function showFnInfo(k) {
  const fm = fnMeta[k];
  info.style.display = "block";
  document.getElementById("iTitle").textContent = fm.name + "()";
  document.getElementById("iSub").textContent = nodes[fm.file].path;
  panelCopyText = "res://" + nodes[fm.file].path + "::" + fm.name;
  const tags = document.getElementById("iTags");
  tags.innerHTML = "";
  // IO surface: signature line + writes/mutates chips (fn-IO feature)
  const io = (DATA.fio || {})[nodes[fm.file].path + "::" + fm.name];
  if (io) {
    if (io.sig) {
      const sig = document.createElement("div");
      sig.style.cssText = "font:11px/1.5 monospace;color:#cfd8dc;margin:2px 0 6px;word-break:break-all";
      sig.textContent = io.sig + (io.ret ? " -> " + io.ret : "");
      tags.appendChild(sig);
    }
    const chip = (txt, bg) => {
      const s = document.createElement("span");
      s.style.cssText = `display:inline-block;margin:0 4px 4px 0;padding:2px 7px;border-radius:6px;font-size:10.5px;color:#eceff1;background:${bg}`;
      s.textContent = txt;
      return s;
    };
    if (io.w.length) tags.appendChild(chip("✎ writes: " + io.w.join(", "), "rgba(0,105,92,.55)"));
    if (io.mp.length) tags.appendChild(chip("⇄ mutates: " + io.mp.join(", "), "rgba(180,100,20,.45)"));
    if (!io.w.length && !io.mp.length) tags.appendChild(chip("pure — no state writes", "rgba(55,71,79,.7)"));
  }
  // jumping to a caller/callee focuses its file and keeps the fn layer on
  const jumpFn = j => {
    if (!fnMode) { fnMode = true; document.getElementById("cbFn").checked = true; }
    pushFocusState();
    focusSeeds.clear(); focusSeeds.add(j);
    showInfo(j); applyVisibility(); focus(j);
  };
  const renderFn = (kindId, ulId, label, entries) => {
    document.getElementById(kindId).textContent = `${label} (${entries.length})`;
    const ul = document.getElementById(ulId);
    ul.innerHTML = "";
    // no 24-cap: the ul's max-height + overflow-y scroll carries any
    // length (map-spec-v2 section 8 BUGFIX)
    entries.forEach(e => {
      const li = document.createElement("li");
      const j = kindId === "kUses" ? e[2] : e[0];
      li.textContent = nodes[j].label + " :: " + (kindId === "kUses" ? e[3] : e[1]);
      li.onclick = () => jumpFn(j);
      ul.appendChild(li);
    });
  };
  const outs = [], ins = [], seen = new Set();
  fedges.forEach(e => {
    if (e[0] === fm.file && e[1] === fm.name) {
      const key = "→" + e[2] + "::" + e[3];
      if (!seen.has(key)) { seen.add(key); outs.push(e); }
    }
    if (e[2] === fm.file && e[3] === fm.name) {
      const key = "←" + e[0] + "::" + e[1];
      if (!seen.has(key)) { seen.add(key); ins.push(e); }
    }
  });
  renderFn("kUses", "iUses", "CALLS", outs);
  renderFn("kUsedBy", "iUsedBy", "CALLED BY", ins);
}

// which sphere a function belongs to
let fnStalk = null;
function fnStalkUpdate(fi, p) {
  if (!fnStalk) {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6), 3));
    fnStalk = new THREE.Line(g, new THREE.LineBasicMaterial(
      { color: 0xffffff, transparent: true, opacity: 0.85, depthTest: false }));
    fnStalk.renderOrder = 5;
    scene.add(fnStalk);
  }
  const a = fnStalk.geometry.attributes.position.array;
  a[0] = p[0]; a[1] = p[1]; a[2] = p[2];
  a[3] = pos[fi*3]; a[4] = pos[fi*3+1]; a[5] = pos[fi*3+2];
  fnStalk.geometry.attributes.position.needsUpdate = true;
  fnStalk.visible = true;
}
function fnStalkHide() { if (fnStalk) fnStalk.visible = false; }

// hover greyout: steal from Cosmograph (cosmos config.ts highlightedPointIndices
// / linkGreyoutOpacity 0.1) — dim everything outside the hovered node's 1-hop
// neighborhood instead of waiting for a click. Grey, not hidden: structure
// stays on screen, the eye gets an instant "what relates to this".
// greyout also dims the DOM label layers (hub pills, cluster names, focus
// labels): labels at full ink floating over a greyed scene read as
// un-greyed content
const greyLabelEls = ["hubs", "clabs", "flabs"].map(id => document.getElementById(id));
function greyLabelsDim(on) {
  greyLabelEls.forEach(el => { el.style.opacity = on ? 0.25 : ""; });
}
function hoverGrey(i) {
  if (i === hoverGreyIdx) return;
  if (pointerDown || focusActive || deadOnly || query) {
    if (hoverGreyIdx >= 0) { hoverGreyIdx = -1; applyVisibility(); greyLabelsDim(false); }
    return;
  }
  if (i >= 0 && alphaTgt[i] <= 0.5) i = -1;   // can't grey around a ghost
  if (i < 0) {
    if (hoverGreyIdx >= 0) { hoverGreyIdx = -1; applyVisibility(); greyLabelsDim(false); }
    return;
  }
  // full baseline first (restores any previous grey), then dim to 0.12
  hoverGreyIdx = -1;
  applyVisibility();
  greyLabelsDim(true);
  hoverGreyIdx = i;
  const lit = new Set([i]);
  (adj[i] || []).forEach(j => lit.add(j));
  for (let j = 0; j < N; j++)
    if (!lit.has(j) && alphaTgt[j] > 0.12) alphaTgt[j] = 0.12;
  links.forEach((l, k) => {
    if (alphaTgt[l.s] > 0.5 && alphaTgt[l.t] > 0.5) return;
    const b = bucketOf[k], tgt = bucketColIB[b].array;
    // grey = 12% of the edge's own color; collapsed/black links stay black.
    // Buffers are SLOT-laid (applyVisibility writes at slotOf[i]*6 / hwSlot):
    // indexing by link index k*6 dimmed whatever edge owned that slot.
    if (hwSlot[k] >= 0) {
      for (let o6 = hwSlot[k]; o6 < hwSlot[k] + 96; o6 += 6) {
        tgt[o6] *= 0.12; tgt[o6+1] *= 0.12; tgt[o6+2] *= 0.12;
        tgt[o6+3] *= 0.12; tgt[o6+4] *= 0.12; tgt[o6+5] *= 0.12;
      }
    } else {
      const o6 = slotOf[k] * 6;
      tgt[o6] *= 0.12; tgt[o6+1] *= 0.12; tgt[o6+2] *= 0.12;
      tgt[o6+3] *= 0.12; tgt[o6+4] *= 0.12; tgt[o6+5] *= 0.12;
    }
    bucketColIB[b].needsUpdate = true;
  });
}

renderer.domElement.addEventListener("pointermove", e => {
  // raycast NDC + screen-space picks are CANVAS-relative: with the map pane
  // owning the right edge, the canvas is no longer the whole window
  const cr = renderer.domElement.getBoundingClientRect();
  const ndcX = (e.clientX - cr.left) / cr.width, ndcY = (e.clientY - cr.top) / cr.height;
  mouse.x = ndcX*2-1; mouse.y = -ndcY*2+1;
  raycaster.setFromCamera(mouse, camera);
  const targets = fnMesh ? [fileMesh, fnMesh] : [fileMesh];
  const hits = raycaster.intersectObjects(targets);
  hovered = -1; hoveredFn = -1;
  // skip invisible nodes: filtered-out tests/tools keep raycast geometry,
  // but hovering a ghost must not pop a tooltip. Among visible hits, pick
  // by SCREEN-SPACE accuracy (cursor distance vs projected radius), not
  // depth — at high spread a foreground sphere's rim otherwise steals the
  // pick from the node the user is actually pointing at (occlusion).
  const px = ndcX * cr.width, py = ndcY * cr.height;
  let bestPx = 18;   // cursor forgiveness radius in pixels
  for (const h of hits) {
    if (h.object === fnMesh) {
      const fm = fnMeta[h.instanceId];
      if (!fm || alphaTgt[fm.file] <= 0.5 || (fm.agg && !fm.count)) continue;
      const v = _pickV.set(fm.p[0], fm.p[1], fm.p[2]).project(camera);
      const sx = (v.x*0.5+0.5)*cr.width, sy = (-v.y*0.5+0.5)*cr.height;
      const dist = Math.hypot(sx-px, sy-py);
      if (dist < bestPx) { bestPx = dist; hoveredFn = h.instanceId; hovered = -1; }
    } else if (h.object === fileMesh && alphaTgt[h.instanceId] > 0.5) {
      const v = _pickV.set(pos[h.instanceId*3], pos[h.instanceId*3+1], pos[h.instanceId*3+2]).project(camera);
      const sx = (v.x*0.5+0.5)*cr.width, sy = (-v.y*0.5+0.5)*cr.height;
      const dist = Math.hypot(sx-px, sy-py);
      if (dist < bestPx) { bestPx = dist; hovered = h.instanceId; hoveredFn = -1; }
    }
  }
  if (hoveredFn >= 0) fnStalkUpdate(fnMeta[hoveredFn].file, fnMeta[hoveredFn].p);
  else fnStalkHide();
  hoverGrey(hoveredFn >= 0 ? fnMeta[hoveredFn].file : hovered);
  let txt = null;
  if (hoveredFn >= 0) {
    const fm = fnMeta[hoveredFn];
    txt = fm.count ? nodes[fm.file].path + " :: " + fm.count + " calls"
                   : nodes[fm.file].path + " :: " + fm.name;
  } else if (hovered >= 0) {
    txt = nodes[hovered].path;
    const s = edgeSummary(hovered);
    if (s) txt += "\n" + s;
    const path = pathToSeed(hovered);
    if (path) {
      const shown = path.length <= 5 ? path.join(" → ")
        : path.slice(0, 2).join(" → ") + " → …(" + (path.length - 3) + ")→ " + path[path.length - 1];
      txt += "\n→ seed: " + shown;
    }
  }
  // node-hover reveal: refresh the edge pass when the hovered node changes
  // during focus (same contract as wire hover -> applyVisibility)
  if (focusActive && hovered !== hoverVisNode) {
    hoverVisNode = hovered;
    applyVisibility();
  }
  // wire hover: no node under the cursor -> raycast the edge buckets and
  // name the strongest named wire on that file pair ('A::sfn -> B::dfn').
  // Enabled during focus too: the hub edge budget ghosts most wires, so
  // hover is the on-demand reveal — the hovered wire re-lights (budget
  // bypass) AND shows its tooltip. Suppressed while dragging. Filtered/
  // ghost edges never match.
  if (!txt && !pointerDown) {
    const m = pickWireMeta(e);
    const hli = m && m.kind === "link" ? m.li : -1;
    if (focusActive && hli !== hoverEdgeLi) {
      hoverEdgeLi = hli;
      applyVisibility();   // re-runs the budget pass with the hover bypass
    }
    if (m) {
      if (m.kind === "link") {
        const pair = strongPair(links[m.li]);
        if (pair.length) {
          txt = nodes[pair[0][1]].label + "::" + pair[0][2] +
            " → " + nodes[pair[0][3]].label + "::" + pair[0][4];
        }
      } else if (m.kind === "trunk") {
        txt = "🚌 bus " + nodes[m.sf].label + " → " + nodes[m.tf].label +
          "  (" + (m.mates || []).length + " wires)";
      } else if (m.kind === "jleg" || m.kind === "sub" || m.kind === "jof") {
        const pp = m.k ? String(m.k).split("|") : null;
        const mates = riderWiresOf(m.fi, pp ? +pp[2] : -1);
        const srcs = [...new Set(mates.map(w =>
          fnMeta[fnMeta[w.a].file === m.fi ? w.a : w.b].name))];
        const dests = [...new Set(mates.map(w =>
          fnMeta[fnMeta[w.a].file === m.fi ? w.b : w.a].file))];
        txt = (m.kind === "jleg" ? "🚌 junction leg · " : "🚌 bus fan · ") +
          nodes[m.fi].label + " · " +
          srcs.slice(0, 3).join(", ") + (srcs.length > 3 ? "…" : "") +
          "  →  " + dests.slice(0, 2).map(fi2 => nodes[fi2].label).join(", ") +
          (dests.length > 2 ? " +" + (dests.length - 2) : "") +
          "  · " + mates.length + " wires";
      } else if (m.kind === "station") {
        const mates = riderWiresOf(m.fi, -1);
        const dests = [...new Set(mates.map(w =>
          fnMeta[fnMeta[w.a].file === m.fi ? w.b : w.a].file))];
        txt = "🚌 bus · " + nodes[m.fi].label + " · " + mates.length + " wires → " +
          dests.slice(0, 3).map(fi2 => nodes[fi2].label).join(", ") +
          (dests.length > 3 ? " +" + (dests.length - 3) : "");
      } else {
        const a = fnMeta[m.a], b = fnMeta[m.b];
        txt = nodes[a.file].label + "::" + a.name + "() → " +
              nodes[b.file].label + "::" + b.name + "()" +
              (m.ln >= 0 ? "  @L" + m.ln : "");
      }
    }
  }
  if (txt) {
    tip.style.display = "block";
    tip.style.left = (e.clientX+14)+"px"; tip.style.top = (e.clientY+14)+"px";
    tip.textContent = txt;
    renderer.domElement.style.cursor = "pointer";
  } else { tip.style.display = "none"; renderer.domElement.style.cursor = "grab"; }
});
// drag-vs-click: OrbitControls uses pointer drags; a release over a node
// after rotating the camera must not select it
let downX = 0, downY = 0;
// auto-spin interaction gating (read by tick)
let pointerDown = false, overCanvas = false;
renderer.domElement.addEventListener("pointerdown", e => {
  downX = e.clientX; downY = e.clientY;
  pointerDown = true;
  fnClosePick();   // canvas input closes the fn picker (map picker parity)
  camTween = null;   // user grab beats the tween
  if (hovered < 0 && hoveredFn < 0) renderer.domElement.style.cursor = "grabbing";
});
renderer.domElement.addEventListener("pointerup", () => {
  pointerDown = false;
  renderer.domElement.style.cursor = "grab";   // pointermove corrects to pointer over a node
});
renderer.domElement.addEventListener("pointerenter", () => { overCanvas = true; });
renderer.domElement.addEventListener("pointerleave", () => { overCanvas = false; pointerDown = false; });
// multi-root camera: frame the centroid of all focus seeds at a distance
// set by their spread (single seed falls back to the tight focus)
function focusSeedsCamera() {
  const arr = [...focusSeeds];
  if (arr.length === 1) { focus(arr[0]); return; }
  const c = new THREE.Vector3();
  arr.forEach(i => c.add(new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2])));
  c.divideScalar(arr.length);
  let r = 120;
  arr.forEach(i => r = Math.max(r, c.distanceTo(
    new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]))));
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y,
    camera.position.z - controls.target.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  tweenCamTo(c, c.clone().addScaledVector(dir, Math.min(900, 240 + r * 2)));
}
// frame the query match set (level-0 nodes of the search BFS): the search
// path fills `query`, not focusSeeds, so focusSeedsCamera can't see it
function frameQueryCamera() {
  if (!query) { frameGraph(); return; }
  const arr = [];
  for (let i = 0; i < N; i++)
    if (level[i] === 0 && !supMem[i] && alphaTgt[i] > 0.5) arr.push(i);
  if (!arr.length) return;
  if (arr.length === 1) { focus(arr[0]); return; }
  // multi-match with an active fn focus: the bus tier and its pickable
  // trunks live around the focused hub's compact ball — frame THAT ball
  // (focus() already does), not a centroid of all seeds. Fn-name matches
  // can sit hundreds of world-units outside the ball (degree-0 owners),
  // and after an Escape interlude the in-flight pos[] mix pushes the
  // centroid off the ball entirely: geometry off-screen, picking dead.
  if (focusFileIdx >= 0 && arr.includes(focusFileIdx)) { focus(focusFileIdx); return; }
  const c = new THREE.Vector3();
  arr.forEach(i => c.add(new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2])));
  c.divideScalar(arr.length);
  let r = 120;
  arr.forEach(i => r = Math.max(r, c.distanceTo(
    new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]))));
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y,
    camera.position.z - controls.target.z);
  if (dir.lengthSq() < 1) dir.set(0.42, 0.5, 0.76);
  dir.normalize();
  tweenCamTo(c, c.clone().addScaledVector(dir, Math.min(900, 240 + r * 2)));
}
// wires & buses pick in the CAPTURE phase: fn-box labels (.flab) and the
// canvas itself sit above the wires' pixels — without this, clicking a wire
// near the hub opens the fn instead. No wire nearby -> event passes through.
document.addEventListener("pointerdown", e => {
  downX = e.clientX; downY = e.clientY;   // fresh drag-guard origin anywhere
  hideWireTip();   // any new press dismisses the tip (drag, right-click);
}, true);          // a wire click re-shows it right after
document.addEventListener("click", e => {
  if (!focusActive || !fnLines) return;
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  const wHit = pickWireMeta(e);
  if (!wHit) return;
  // fn-box faces are small precise targets and wires TERMINATE at them —
  // the box wins outright. Only file spheres defer to nearer wires: the
  // hub sphere's projected disk covers lifted bus arcs (hover raycast is
  // sphere-only and can't see the arc in front), so depth decides there.
  if (hoveredFn >= 0) return;
  if (hovered >= 0) {
    const np = [pos[hovered * 3], pos[hovered * 3 + 1], pos[hovered * 3 + 2]];
    const nz = new THREE.Vector3(np[0], np[1], np[2]).project(camera).z;
    if (pickWireZ >= nz) return;
  }
  e.stopPropagation();   // the label/canvas click handlers stay out
  showWireTip(wHit, e.clientX, e.clientY);
}, true);
renderer.domElement.addEventListener("click", e => {
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (hoveredFn >= 0) {
    // aggregate box ('n×') has no single fn behind it: open the picker over
    // that file's roster instead of showFnInfo with an empty fn name
    const fm = fnMeta[hoveredFn];
    if (fm.count) openFnPicker(fm.file, e.clientX, e.clientY);
    else showFnInfo(hoveredFn);
    return;
  }
  if (hovered >= 0) {
    if (e.shiftKey && focusSeeds.size) {
      // shift-click stacks focus roots (click a selected root to drop it);
      // dropping the LAST root is an implicit "clear" — same scope as Esc
      if (focusSeeds.has(hovered)) {
        focusSeeds.delete(hovered);
        if (!focusSeeds.size) { clearFocus(); return; }
      } else { pushFocusState(); focusSeeds.add(hovered); }
      applyVisibility();
      focusSeedsCamera();
      showInfo(hovered);
      mapCenterOn(hovered);
    } else {
      pushFocusState();
      focusSeeds.clear(); focusSeeds.add(hovered);
      applyVisibility();
      focus(hovered);
      showInfo(hovered);
      mapCenterOn(hovered);
    }
  }
});
function resize3D() {
  const w = glW(), h = innerHeight;
  camera.aspect = w/h; camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  bucketMat.forEach(m => m.resolution.set(w, h));
  // fat-line overlays live in screen px too — stale resolution = wrong width
  if (fnLines) fnLines.material.resolution.set(w, h);
  if (fnQuiet) fnQuiet.material.resolution.set(w, h);
  if (focusArcs) focusArcs.mat.resolution.set(w, h);
}
addEventListener("resize", resize3D);
// LOD zoom threshold: crossing it reveals/hides intra-cluster edges at
// overview (filters never re-layout — this only recomputes edge colors)
controls.addEventListener("change", () => {
  const c = camera.position.distanceTo(controls.target) < lodDist;
  if (c !== lodClose) {
    lodClose = c;
    if (!focusSeeds.size && !query) applyVisibility();
  }
});

// apply the overview palette + LOD once at boot (initial buffer fill is
// full-color; this demotes it to the overview state without waiting for
// user interaction)
buildContainment();
applyVisibility();
frameGraph();
renderer.domElement.style.cursor = "grab";
// the map pane ships open — apply the split (canvas size, overlay clamp,
// info shift) once everything it touches exists
setMapVisible(true);
// debug handle last: everything it captures is initialized by here
window.__dbg = { pos, nodes, links, fedges, syncEdgePos, renderer, camera, THREE, sizes, degree,
  meta: DATA.meta, controls, get spinEnabled() { return spinEnabled; }, get hubCap() { return hubCapNow; },
  fns: DATA.fns || {},
  alpha: alphaArr, alphaTgt, hoverScale, hot, bucketMat, bucketOf, hwSlot, bucketPosIB, bucketColIB, slotOf,
  adjOut, adjIn, adj, outDeg, inDeg, get dirMode() { return dirMode; }, focusSeeds, level,
  get budgetLit() { return budgetLit; }, get hoverEdgeLi() { return hoverEdgeLi; },
  get fnLines() { return fnLines; }, get hubRing() { return hubRing; },
  get fnArrows() { return fnArrows; }, get compactBallR() { return compactBallR; },
  get fnQuiet() { return fnQuiet; }, get fnTrunkN() { return fnTrunkN; },
  get fnQuietTrunkN() { return fnQuietTrunkN; },
  get fnBus() { return fnBus; }, get fnBusPx() { return fnBusRi; },
  get busPts() { return busPts; }, get fnJDot() { return fnJDot; },
  get fnJDotR() { return fnJDotR; }, get fnArrowR() { return fnArrowR; },
  get camera() { return camera; },
  get fnTrunkW() { return fnTrunkW; }, get fnJstubN() { return fnJstubN; },
  get fnQuietTrunkW() { return fnQuietTrunkW; },
  get fnStations() { return fnStationsArr; },
  get fnLod() { return fnLodV ? Object.assign({}, fnLodV) : null; }, get fnJclear() { return fnJclearV; },
  get lodServe() { return _lodServe; },   // serveAll master gate (chain-integrity census)
  get lodPxOf() { return _lod ? _lod.pxOf : null; },   // per-file box ref-px (probe hook)
  get alphaTgt() { return alphaTgt; },   // lit-set oracle (tier unification pin)
  get jDotArrays() { return { of: fnJDotOf, st: fnJDotSt, legs: fnJDotLegs, key: fnJDotKey }; },
  get stubExits() { return stubExits; },  // EXPLAINED EXIT dissolve points
  get anchorBoostArr() { return anchorBoost; },  // corridor-boost px per fi (probe hook)
  get chainGates() { return [...chainGate.entries()]; },  // per-chain first failing gate (sighting #10)
  get taperDbg() {   // EXPLAINED EXIT probe: per-leg gate state this frame
    const out = [];
    if (_lod && legTermOn) {
      for (const s of busPts) {
        if (typeof s.k !== "string" || s.k.charCodeAt(0) !== 76) continue;
        if (out.some(o => o.k === s.k)) continue;
        const p = s.k.split("|");
        out.push({ k: s.k, legOK: _lod.legOK(s.k), termOn: legTermOn.get(s.k),
                   ctx: focusFileIdx >= 0 && alphaTgt[+p[1]] > 0.5 });
      }
    }
    return out; },
  get legAnchorPx() {
    // rendered L| chains' own-file ANCHOR size: {k, fi, px, boxPx, boosted}
    // for chains actually serving THIS frame. px = max(fn-box px, live
    // sprite px incl. the corridor anchorBoost lift) — the corridor law
    // anchors legs on the SPRITE (size, not brightness), so the pin is
    // min(px) >= ANCHOR_PX. busPts keeps culled chains as inventory
    // (render cull = instance scale 0.0001): filter by served scale,
    // presence is NOT serve state.
    if (!busPts || !fnBus) return [];
    const m = fnBus.instanceMatrix.array, seen = new Map();
    for (let i = 0; i < busPts.length; i++) {
      const k = busPts[i].k;
      if (typeof k !== "string" || k.charCodeAt(0) !== 76) continue;
      if (seen.has(k)) continue;
      const r = Math.hypot(m[i * 16], m[i * 16 + 1], m[i * 16 + 2]);
      seen.set(k, r > 0.001);
    }
    const mat = new THREE.Matrix4();
    const tanH = Math.tan(camera.fov * Math.PI / 360);
    const out = [];
    for (const [k, served] of seen) {
      if (!served) continue;
      const fi = +k.split("|")[1];
      fileMesh.getMatrixAt(fi, mat);
      const dist = camera.position.distanceTo(
        new THREE.Vector3(pos[fi * 3], pos[fi * 3 + 1], pos[fi * 3 + 2]));
      const sprPx = mat.elements[0] * 900 / (tanH * dist);   // diameter, REF-px
      const boxPx = _lod ? _lod.pxOf(fi) : null;
      out.push({ k, fi, px: Math.max(sprPx, boxPx || 0), boxPx, boosted: anchorBoost[fi] > 0,
                 alpha: alphaTgt[fi] });
    }
    return out;
  },
  get corridorCensus() {
    // corridor-complete law census for independent harness verification:
    // every busPts chain (leg "L|fi|st|li" / trunk "sf>tf") with endpoint
    // world coords, serve state (instance scale; culled chains stay in the
    // inventory at scale 0.0001) and the anchor each SERVED end registers
    // to — nearest station within 55wu (station fan termini spread up to
    // ~45wu on the tangent line), else nearest fn box within 55wu, else
    // null = unattached. Stations carry attached serving-chain counts per
    // side. Node positions: d.pos[i*3..]; box screen px: d.lodPxOf(fi).
    if (!busPts) return null;
    const m = fnBus ? fnBus.instanceMatrix.array : null;
    const seen = new Map(), chains = [];
    for (let i = 0; i < busPts.length; i++) {
      const s = busPts[i];
      if (typeof s.k !== "string") continue;
      let e = seen.get(s.k);
      if (!e) {
        e = { k: s.k, kind: s.k.charCodeAt(0) === 76 ? "leg" : "trunk",
              a: [s.a[0], s.a[1], s.a[2]], b: [s.b[0], s.b[1], s.b[2]],
              served: false, anchorA: null, anchorB: null };
        seen.set(s.k, e); chains.push(e);
      }
      if (m) { const r = Math.hypot(m[i * 16], m[i * 16 + 1], m[i * 16 + 2]); if (r > 0.001) e.served = true; }
    }
    const stations = (fnStationsArr || []).map(S =>
      ({ fi: S.fi, id: S.id, p: [S.p[0], S.p[1], S.p[2]], trunks: 0, legs: 0 }));
    // anchor matching is SCREEN-SPACE first (station 14px, box 12px, junction
    // 12px) with world-unit fallbacks (55/55/20wu): depth foreshortening puts
    // fan termini >20wu from their junction yet 1.8px apart on screen — the
    // census must match what the eye matches, or harness pins report phantom
    // unattached ends. All iteration is over fixed arrays (deterministic).
    const proj = p => {
      const v = new THREE.Vector3(p[0], p[1], p[2]).project(camera);
      if (!isFinite(v.x) || !isFinite(v.y) || v.z >= 1) return null;
      const r = renderer.domElement.getBoundingClientRect();
      return { x: (v.x + 1) / 2 * r.width, y: (1 - v.y) / 2 * r.height };
    };
    const pickAnchor = (p, cands) => {
      // cands: [{kind, idx, p, pxTol, wuTol}] — best qualifying by screen px,
      // else by world distance. Candidate list order is fixed (deterministic).
      const sp = proj(p);
      let best = null, bestS = Infinity, bestW = Infinity;
      for (const c of cands) {
        const wu = Math.hypot(c.p[0] - p[0], c.p[1] - p[1], c.p[2] - p[2]);
        if (wu > c.wuTol * 1.2) continue;   // hard world ceiling either way
        let s = Infinity;
        if (sp) { const q = proj(c.p);
          if (q) s = Math.hypot(q.x - sp.x, q.y - sp.y); }
        const okS = s <= c.pxTol, okW = wu <= c.wuTol;
        if (!okS && !okW) continue;
        if (okS && s < bestS) { best = c; bestS = s; }
        else if (!okS && wu < bestW) { best = c; bestW = wu; }
      }
      return best;
    };
    const stCands = stations.map((st, si) => ({ kind: "station", idx: si, p: st.p, pxTol: 14, wuTol: 55 }));
    const boxCands = [];
    for (let bi = 0; bi < fnMeta.length; bi++) {
      const mb = fnMeta[bi];
      if (mb) boxCands.push({ kind: "box", idx: mb.file, p: mb.p, pxTol: 12, wuTol: 55 });
    }
    // junction bollards (fnJDot instances): interior corridor heads — a leg
    // end landing on one is attached (part of the full-path unit), though
    // the UNIT termini still owe a file anchor elsewhere
    const juncCands = [];
    if (fnJDot) {
      const jm = fnJDot.instanceMatrix.array;
      for (let ji = 0; ji < fnJDot.count; ji++)
        juncCands.push({ kind: "junc", idx: ji, p: [jm[ji * 16 + 12], jm[ji * 16 + 13], jm[ji * 16 + 14]], pxTol: 12, wuTol: 20 });
    }
    // leads-home fallback (rubric Amendment 4): an end that misses the tight
    // windows still attaches to the nearest IN-VIEW station within 300px —
    // fan-spread termini 18-40px out are visually leads-home, not floating
    // ink, and the harness pin reads unattached==0. Marked leadsHome with px
    // so the distinction stays queryable.
    const leadsHome = (p, which, c) => {
      const sp = proj(p);
      if (!sp) return false;
      let best = null, bd = 300;
      for (let si = 0; si < stations.length; si++) {
        const q = proj(stations[si].p);
        if (!q) continue;
        const d = Math.hypot(q.x - sp.x, q.y - sp.y);
        if (d < bd) { bd = d; best = si; }
      }
      if (best === null) return false;
      if (c.kind === "leg") stations[best].legs++;   // trunk feed owned by the post-loop pass
      c["anchor" + which.toUpperCase()] = { type: "station", st: best, leadsHome: Math.round(bd * 10) / 10 };
      return true;
    };
    const attach = (c, which) => {
      const p = c[which];
      for (const set of [stCands, boxCands, juncCands]) {
        const a = pickAnchor(p, set);
        if (!a) continue;
        const rec = { type: a.kind };
        if (a.kind === "station") {
          rec.st = a.idx;
          if (c.kind === "leg") stations[a.idx].legs++;   // legs claim where they land; trunks feed post-loop
        }
        else if (a.kind === "box") rec.fi = a.idx;
        else rec.j = a.idx;
        c["anchor" + which.toUpperCase()] = rec;
        return;
      }
      leadsHome(p, which, c);
    };
    for (const c of chains) {
      if (!c.served) continue;
      attach(c, "a");
      attach(c, "b");
    }
    // per-station trunkNearPx: min screen px to ANY served trunk endpoint,
    // and trunk FEED counts at the leads-home window (Amendment 4 bar
    // <= 300px): a station is trunk-fed when a served trunk endpoint lands
    // within 300px — fan spread puts termini 15-40px past the station dot,
    // still leads-home, not floating ink. One-sidedness = legs>0 &&
    // trunks==0 (fan without its trunk = the cut-bridge class); legs==0
    // stations are the no-fan ramp-bridged class (the file's own box anchors
    // via fnLines ramps, which the census does not carry).
    for (const st of stations) st.trunkNearPx = null;
    for (const c of chains) {
      if (!c.served || c.kind !== "trunk") continue;
      const fed = new Set();
      for (const p of [c.a, c.b]) {
        const sp = proj(p);
        if (!sp) continue;
        for (let si = 0; si < stations.length; si++) {
          const q = proj(stations[si].p);
          if (!q) continue;
          const d = Math.hypot(q.x - sp.x, q.y - sp.y);
          if (stations[si].trunkNearPx === null || d < stations[si].trunkNearPx)
            stations[si].trunkNearPx = Math.round(d * 10) / 10;
          if (d <= 300) fed.add(si);
        }
      }
      for (const si of fed) stations[si].trunks++;
    }
    return { chains, stations };
  },
  get legendOpen() { return legendOpen; }, get busPtsMeta() { return busPtsMeta; },
  get fnJclip() { return fnJclip; }, get fnLegN() { return fnLegN; },
  // probe hook: world -> screen px through the live camera + canvas rect
  projectPoint(x, y, z) {
    const v = new THREE.Vector3(x, y, z).project(camera);
    const r = renderer.domElement.getBoundingClientRect();
    return { x: r.left + (v.x + 1) / 2 * r.width,
             y: r.top + (1 - v.y) / 2 * r.height, z: v.z };
  },
  pickWireMeta, wireDesc, showWireTip, hideWireTip,
  get litSet() { return compactIdx; }, get compactScale() { return compactScale; },
  get overlaps() { return compactOverlaps; },
  get camTween() { return camTween; }, get focusStack() { return focusStack; },
  get focusFileIdx() { return focusFileIdx; }, get compactAnim() { return !!compactAnim; },
  get posSavedLive() { return posSaved !== null; },
  posAt: i => [pos[i*3], pos[i*3+1], pos[i*3+2]],
  compactTgtAt: i => compactTgt ? [compactTgt[i*3], compactTgt[i*3+1], compactTgt[i*3+2]] : null,
  get fileMesh() { return fileMesh; }, get fnMesh() { return fnMesh; },
  get controls() { return controls; },
  get fnMeta() { return fnMeta; }, get fnStalks() { return fnStalks; },
  get hovered() { return hovered; },
  get groundGrid() { return groundGrid; },
  get groupsMode() { return groupsMode; }, groups,
  get spread() { return spread; }, get deadOnly() { return deadOnly; },
  get collapsed() { return collapsed; }, dpos, supCollapsed, refreshCollapse,
  colArr,
  get fnStalk() { return fnStalk; },
  syncFileMesh,
  mapPane: { canvas: mapPane, draw: drawMapPane },
  mwires, mapInfo, get mapVars() { return mapVarsOn; }, mapExpandUser,
  get mapZ() { return mapZ; }, get mapPX() { return mapPX; },
  get mapPY() { return mapPY; }, mapClampView,
  get mapInkOn() { return mapInkOn; },
  get mapCenterReq() { return mapCenterReq; }, get mapPulse() { return mapPulse; },
  get paneW() { return paneW; }, setMapVisible, divider,
  get glW() { return glW(); },
  get bucketMesh() { return bucketMesh; }, raycaster, linkFiltered, typeVisible,
  nodeFiltered, fnMode, supMem, strongPair,
  get mapLayout() { return mapLayout; },
  get mapRects() { return mapRects; },
  get routeAudit() { return mapLayout && mapLayout.audit; },
  sphR,
  get focusArcRef() { return focusArcs; },
  get rfwProbe() { return { fa: !!focusArcs, focusActive, budgetN: budgetLit ? budgetLit.size : null, fi: focusFileIdx }; } };
tick();
</script>
</body>
</html>
"""



_VENDOR = Path(__file__).resolve().parent / "vendor" / "three-0.160.0"
_ADDONS = {   # keys the template imports; keep in sync with its import lines
    "three/addons/controls/OrbitControls.js": "controls/OrbitControls.js",
    "three/addons/lines/LineSegments2.js": "lines/LineSegments2.js",
    "three/addons/lines/LineSegmentsGeometry.js": "lines/LineSegmentsGeometry.js",
    "three/addons/lines/LineMaterial.js": "lines/LineMaterial.js",
}


def _data_uri(js: str) -> str:
    js = js.replace("\r\n", "\n")   # byte-stable embed across LF/CRLF checkouts
    return "data:text/javascript;base64," + base64.b64encode(js.encode("utf-8")).decode("ascii")


def _importmap() -> str:
    """Zero-network artifact: three + the addons the template imports are
    vendored (pinned 0.160.0, sha-pinned in vendor/) and embedded as data:
    URIs at build time. data: modules cannot resolve RELATIVE specifiers, so
    the addons' relative imports are rewritten to their importmap keys."""
    core = (_VENDOR / "three.module.js").read_text(encoding="utf-8")
    imports = {"three": _data_uri(core)}
    for key, rel in _ADDONS.items():
        src = (_VENDOR / rel).read_text(encoding="utf-8")
        src = re.sub(r"from\s+'\.\./(controls|lines)/([A-Za-z0-9_.]+)'",
                     r"from 'three/addons/\1/\2'", src)
        imports[key] = _data_uri(src)
    return json.dumps({"imports": imports}, separators=(",", ":"))


def generate(out: str | Path | None = None) -> Path:
    out = Path(out) if out else Path(__file__).resolve().parent / "graph.html"
    data = _build_data()
    html = (_TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
                    .replace("__IMPORTMAP__", _importmap()))
    out.write_text(html, encoding="utf-8")
    return out

if __name__ == "__main__":
    path = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(path)
