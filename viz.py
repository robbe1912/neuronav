"""neuronav viz: self-contained force-directed 3D graph of the indexed repo.

Generates `graph.html` (single file, three.js from CDN). Nodes = indexed
files colored by semantic cluster; red-mixed nodes contain dead-code
candidates. Edges = aggregated structural links (call edges, scene
instancing, scene→script attachment).

Usage:  python viz.py            # writes graph.html next to this file
        python viz.py out.html   # custom output path
"""

from __future__ import annotations

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
    fedges: list[list] = []
    for src_key, dsts in g.edges.items():
        if src_key.endswith("::tscn"):  # pseudo source, fn would be "tscn"
            continue
        s_path, s_fn = src_key.split("::", 1)
        if s_path not in idx:
            continue
        src_fs = g.files.get(s_path)
        s_line = src_fs.funcs[s_fn].line if src_fs and s_fn in src_fs.funcs else 0
        for dst_key in dsts:
            if (
                dst_key.endswith("::tscn")
                or "::SIGNAL:" in dst_key
                or "::VAR:" in dst_key  # var pseudo-node, not a real fn
            ):
                continue
            d_path, d_fn = dst_key.split("::", 1)
            if d_path not in idx or d_path == s_path:
                continue
            fedges.append([idx[s_path], s_fn, idx[d_path], d_fn, s_line])

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
    try:
        pos_baked = _layout(
            len(nodes), links, sims, [nd["cluster"] for nd in nodes],
            ckeys=ckeys, cmat=cmat,
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
                perp = _np.cross(ch, [0.0, 0.0, 1.0])
                pl = float(_np.linalg.norm(perp))
                if pl < 0.001:
                    perp = _np.array([1.0, 0.0, 0.0])
                else:
                    perp = perp / pl
                ctrl = ctrl + perp * min(chl * 0.2, 80.0)
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
        "sims": sims,
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
            "depth": _strata_depths(len(nodes), links),
            # top inter-cluster corridors, labeled at their arc midpoints
            "crosstalk": crosstalk,
        },
    }
    if hot is not None:
        data["hot"] = hot
    if groups2:
        data["groups"] = groups2
    return data


_TRACE: list = []   # debug: (step, cluster centroids snapshot) every 50 steps


def _strata_depths(n: int, links: list) -> list:
    """Per-node call depth for the strata layout (mermaid reading order).

    Builds the directed call graph from links ({"s","t"} dicts or [s,t,...]
    rows), condenses strongly-connected components (cycles exist — an SCC
    shares one depth), then longest-path layering over the condensation DAG
    in topo order from its in-degree-0 roots: every cross-SCC edge s->t
    gets depth[t] >= depth[s] + 1, entries (in-degree 0) land at depth 0.
    Pure stdlib so tests can run it without the index stack.
    """
    adj: list = [[] for _ in range(n)]
    for l in links:
        s, t = (l["s"], l["t"]) if isinstance(l, dict) else (l[0], l[1])
        adj[s].append(t)

    # Tarjan SCC, iterative (repo call chains can outrun the recursion limit)
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
    return [cdepth[comp[i]] for i in range(n)]


def _layout(n: int, links: list, sims: list, cluster_ids: list,
            ckeys: list = None, cmat: list = None) -> list:
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
        # repulsion evict them from their own cluster (Blood_showcase drifted
        # 310 units from Blood into the sparse pocket next to Gameplay).
        # Degree-scaled anchor keeps hubs home; average nodes barely move.
        csum = np.zeros((nc, 3), dtype=np.float32)
        np.add.at(csum, cinv, pos)
        cen = csum / ccount[:, None]
        if step % 50 == 0:
            _TRACE.append((step, cen.copy()))
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
    rad = (np.minimum(10.0, 3.5 + np.sqrt(deg) * 1.0) * 1.1).astype(np.float32)
    min_d = (rad[:, None] + rad[None, :]) * 1.7
    for _ in range(140):
        diff = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((diff * diff).sum(-1))
        np.fill_diagonal(dist, np.inf)
        need = min_d - dist
        if need.max() <= 0:
            break
        dirs = diff / np.maximum(dist, 1e-3)[..., None]
        # clip need: the diagonal is inf-dist -> -inf need -> 0*-inf = NaN in
        # the product below (picked away by where, but warns and poisons)
        corr = np.where((need > 0)[..., None], dirs * (np.clip(need, 0.0, None) * 0.5)[..., None], 0.0)
        pos += corr.sum(0) * 0.9
    # strata: mermaid reading order — height = call depth from entry files.
    # The force sim and the 3D pass above settle XZ; Y is discarded and
    # re-layered monotonically from _strata_depths (entries on top, callees
    # below) over ~0.55x the old Y half-range, then an XZ-only relaxation
    # with Y frozen restores the non-overlap guarantee.
    depths = _strata_depths(n, links)
    maxd = max(depths, default=0)
    if maxd > 0:
        half = 0.55 * float(pos[:, 1].max() - pos[:, 1].min()) / 2.0
        spacing = 2.0 * half / maxd
        pos[:, 1] = half - np.asarray(depths, dtype=np.float32) * spacing
        # deterministic jitter within a layer (<= 0.15 spacing): breaks exact
        # Y ties so same-layer pairs keep a stable separation axis below
        pos[:, 1] += (rng.uniform(-1.0, 1.0, n) * (0.15 * spacing)).astype(np.float32)
        # required XZ distance so the 3D distance still clears min_d given
        # the now-frozen Y gap (dy >= min_d pairs need nothing)
        dy = pos[:, None, 1] - pos[None, :, 1]
        req = np.sqrt(np.maximum(min_d * min_d - dy * dy, 0.0)).astype(np.float32)
        for _ in range(140):
            dxz = pos[:, None, [0, 2]] - pos[None, :, [0, 2]]
            dxzd = np.sqrt((dxz * dxz).sum(-1))
            np.fill_diagonal(dxzd, np.inf)
            need = req - dxzd
            if need.max() <= 0:
                break
            dirs = dxz / np.maximum(dxzd, 1e-3)[..., None]
            corr = np.where((need > 0)[..., None], dirs * (np.clip(need, 0.0, None) * 0.5)[..., None], 0.0)
            pos[:, [0, 2]] += corr.sum(0) * 0.9
    pos *= 1.45   # extra global breathing room — the frame adapts
    pos -= pos.mean(0)
    return [[round(float(x), 1) for x in p] for p in pos]


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>neuronav — code graph</title>
<style>
  html, body { margin:0; height:100%; background:#000; overflow:hidden;
    font: 13px/1.45 "Segoe UI", system-ui, sans-serif; color:#cfd8dc; }
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
  #edgeLegend { display:flex; flex-wrap:wrap; gap:3px 10px; margin-top:6px;
    font-size:10px; color:#78909c; }
  .eKey { display:flex; align-items:center; gap:4px; }
  .eKey i { width:14px; border-top:2px solid #ffffff55; display:inline-block; }
  .eHint { color:#546e7a; }
  #crumb { position:fixed; top:12px; left:50%; transform:translateX(-50%);
    z-index:10; display:none; align-items:center; gap:6px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633;
    border-radius:14px; padding:4px 7px 4px 14px; font-size:11px; color:#b0bec5; }
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
<input id="depth" type="range" min="1" max="3" value="2">
<span id="depthVal">2</span>
<span style="margin-left:10px">spread</span>
<input id="spread" type="range" min="60" max="260" value="100" title="stretch the whole layout apart (scales from the centroid)">
<span id="spreadVal">1.0</span>
    <label class="cb"><input type="checkbox" id="cbFn"> functions</label>
    <label class="cb"><input type="checkbox" id="cbSpin" checked> spin</label>
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
    <button id="bGround" title="fixed ground grid under the graph (orientation aid)">ground</button>
    <button id="bGroups" title="recolor by coarse supergroups (two-level navigation)">groups</button>
    <button id="bDead" title="show only files flagged dead: at least 40% of their funcs are dead candidates">dead only</button>
    <button id="bReset">reset</button>
  </div>
</div>
<div id="crumb"></div>
<div id="info">
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
<div id="hubs"></div>
<div id="elabs"></div>
<div id="xtlabs"></div>
<div id="flabs"></div>
<div id="clabs"></div>

<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
} }
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
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x000000, 0.00022); // heavier fog washed out cluster hues at overview distance
const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 1, 20000);
camera.position.set(0, 0, 1400);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
// idle auto-spin (the cbSpin checkbox toggles it); tick() pauses it
// while the user drags or inspects a fn box
controls.autoRotate = true;
controls.autoRotateSpeed = 0.35;
let spinEnabled = true;

// cluster hue: golden angle spread; dead files tinted toward red.
// lightness bands break golden-angle hue collisions across long cid runs
const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
const lightOf = c => 0.52 + 0.09 * (Math.floor(c / 13) % 3);
const colorOf = n => {
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
  sizes[i] = Math.min(10, 3.5 + Math.sqrt(degree[i]) * 1.0) * (hot ? 1 + 0.35 * hot[i] : 1);
});
if (hot) document.getElementById("caption").textContent += " · size also encodes 90-day churn";

// spread control: uniformly re-scale the baked layout around its centroid.
// basePos holds the baked coordinates; pos is the live (scaled) copy that
// every renderer reads, so a slider change just re-derives pos + re-syncs.
const basePos = Float32Array.from(pos);
let spread = 1;
// centroid of the baked layout — the affine center for spread transforms
let baseCx = 0, baseCy = 0, baseCz = 0;
for (let i = 0; i < N; i++) { baseCx += basePos[i*3]; baseCy += basePos[i*3+1]; baseCz += basePos[i*3+2]; }
baseCx /= N; baseCy /= N; baseCz /= N;
function applySpread(s) {
  if (s === spread) return;   // no-op: never fight camera tweens / mid-flight syncs
  spread = s;
  let cx = 0, cy = 0, cz = 0;
  for (let i = 0; i < N; i++) { cx += basePos[i*3]; cy += basePos[i*3+1]; cz += basePos[i*3+2]; }
  cx /= N; cy /= N; cz /= N;
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
  // fog must weaken as the galaxy expands or far clusters sink into black
  scene.fog.density = 0.00022 / spread;
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

// true 3D node geometry (billboard sprites read flat on screen): files =
// shaded spheres, functions = boxes sitting ON the call wires, variables
// later = tetrahedra. Per-instance color carries the cluster hue; hidden
// nodes collapse to scale 0 (zero rasterized fragments); dimmed nodes
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
      // camera reframe (reframing was the "systems completely change" bug)
      // focus-context nodes shrink with their alpha instead of staying
      // full-size black occluders
      _dummy.scale.setScalar(sizes[i] * 1.1 * Math.sqrt(spread) * (deadOnly && nodes[i].dead > 0 ? 1.7 : 1) * hoverScale[i] * (0.45 + 0.55 * a));
    }
    _dummy.updateMatrix();
    fileMesh.setMatrixAt(i, _dummy.matrix);
    // dim = darken (scale keeps silhouette, color carries the focus gradient);
    // hovered nodes also lift slightly in brightness alongside the scale ease
    const lift = a * (1 + 0.35 * (hoverScale[i] - 1)) * (i === fnOwner ? 1.9 : 1);
    _col.setRGB(colArr[i*3] * lift, colArr[i*3+1] * lift, colArr[i*3+2] * lift);
    fileMesh.setColorAt(i, _col);
  }
  fileMesh.instanceMatrix.needsUpdate = true;
  if (fileMesh.instanceColor) fileMesh.instanceColor.needsUpdate = true;
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
// overview opacity is flat 0.35 across buckets: width already encodes
// weight — the old descending ops made heavy edges DIMMER than trivial ones
const BUCKETS = [
  { max: 1, width: 1.3, op: 0.35 },          // w <= 1
  { max: 4, width: 2.2, op: 0.35 },          // 2..4
  { max: Infinity, width: 3.5, op: 0.35 },   // >= 5
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
    mat.resolution.set(innerWidth, innerHeight);
    const mesh = new LineSegments2(geo, mat);
    mesh.frustumCulled = false;   // instance positions mutate per frame
    scene.add(mesh);
    bucketPosIB.push(geo.attributes.instanceStart.data);
    bucketColIB.push(geo.attributes.instanceColorStart.data);
    bucketMat.push(mat);
    bucketMesh.push(mesh);   // dash distances are computed on the LineSegments2
  });
}
// focus-mode dash-flow (direction cue): world-units/s of dashOffset travel,
// re-read each tick so window.__dbg.flowSpeed can tune it live
const FLOW_SPEED = 2.5;
let edgeFlowOn = false;   // set by applyVisibility, read by tick's dash pass
let lastTickT = performance.now();

// typed strands between the same file pair run parallel instead of
// overlapping: each gets a slot offset perpendicular to the strand
// filter state shared by applyVisibility (colors) and syncEdgePos
// (geometry) — declared here because syncEdgePos runs at module init
let showTests = false;
const activeDirs = new Set();   // multi-select dir filter
const isHiddenPath = p => p.startsWith("tests/") || p.startsWith("tools/") ||
  p.slice(p.lastIndexOf("/") + 1).startsWith("test_");
const linkFiltered = l => (!showTests && (isHiddenPath(nodes[l.s].path) || isHiddenPath(nodes[l.t].path))) ||
  (activeDirs.size && (!activeDirs.has(nodes[l.s].dir) || !activeDirs.has(nodes[l.t].dir)));
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
          const sx = pos[l.s*3], sy = pos[l.s*3+1] + 0.05, sz = pos[l.s*3+2];
          arr[o] = sx; arr[o+1] = sy; arr[o+2] = sz;
          arr[o+3] = sx; arr[o+4] = sy; arr[o+5] = sz;
          continue;
        }
        const A = pts[v], B = pts[v + 1];
        if (!A || !B) return;
        let ax = baseCx + (A[0] - baseCx) * spread;
        let ay = baseCy + (A[1] - baseCy) * spread;
        let az = baseCz + (A[2] - baseCz) * spread;
        let bx = baseCx + (B[0] - baseCx) * spread;
        let by = baseCy + (B[1] - baseCy) * spread;
        let bz = baseCz + (B[2] - baseCz) * spread;
        // surface trim at the two node-attached ends (baked arc endpoints
        // sit on the node centers): pull the terminal vertex along its own
        // segment by the node's world radius + margin, same rule as
        // straight edges. Interior segments untouched; a degenerate
        // segment (tiny spread) keeps its points rather than feed
        // normalize(0).
        if (v === 0 || v === 15) {
          const vx = bx - ax, vy = by - ay, vz = bz - az;
          const vl = Math.sqrt(vx*vx + vy*vy + vz*vz);
          const tr = (v === 0 ? sizes[l.s] : sizes[l.t]) * 1.1 * Math.sqrt(spread) + 2;
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
      a0[o0] = pos[s]; a0[o0+1] = pos[s+1]; a0[o0+2] = pos[s+2];
      // tiny offset: an exactly-zero-length segment gives LineMaterial's
      // normalize(0) NaN screen quads (driver-dependent streaks)
      a0[o0+3] = pos[s]; a0[o0+4] = pos[s+1] + 0.05; a0[o0+5] = pos[s+2];
      bucketPosIB[bucketOf[i]].needsUpdate = true;
      return;
    }
    let ox = 0, oy = 0;
    const g = pairLinks.get(pairKey(l.s, l.t));
    if (g.length > 1) {
      const slot = g.indexOf(i) - (g.length - 1) / 2;
      // perpendicular to the strand in the xy-plane; +X fallback when the
      // strand is nearly parallel to Z (cross with Z degenerates)
      const dx = pos[t] - pos[s], dy = pos[t+1] - pos[s+1];
      let px = dy, py = -dx;
      const pl = Math.sqrt(px*px + py*py);
      if (pl < 0.001) { px = 1; py = 0; } else { px /= pl; py /= pl; }
      ox = px * slot * 9; oy = py * slot * 9;
    }
    const a = bucketPosIB[bucketOf[i]].array, o = slotOf[i] * 6;
    // surface trim: pull each endpoint out of its sphere (world radius =
    // sizes*1.1*sqrt(spread), +2 margin) so strands meet the surface, not
    // the node center. A strand shorter than both trims would invert and
    // feed normalize(0) — fall back to the same stub the ghost path uses.
    const ex = pos[t] - pos[s], ey = pos[t+1] - pos[s+1], ez = pos[t+2] - pos[s+2];
    const el = Math.sqrt(ex*ex + ey*ey + ez*ez);
    const trimS = sizes[l.s] * 1.1 * Math.sqrt(spread) + 2;
    const trimT = sizes[l.t] * 1.1 * Math.sqrt(spread) + 2;
    if (el - trimS - trimT <= 0.05) {
      a[o] = pos[s]; a[o+1] = pos[s+1]; a[o+2] = pos[s+2];
      a[o+3] = pos[s]; a[o+4] = pos[s+1] + 0.05; a[o+5] = pos[s+2];
      return;
    }
    const ndx = ex / el, ndy = ey / el, ndz = ez / el;
    a[o]   = pos[s] + ndx * trimS + ox; a[o+1] = pos[s+1] + ndy * trimS + oy; a[o+2] = pos[s+2] + ndz * trimS;
    a[o+3] = pos[t] - ndx * trimT + ox; a[o+4] = pos[t+1] - ndy * trimT + oy; a[o+5] = pos[t+2] - ndz * trimT;
  });
  bucketPosIB.forEach(ib => { ib.needsUpdate = true; });
  // dash support: lineDistance attributes must track every geometry
  // rewrite — LineMaterial's USE_DASH reads instanceDistanceStart/End
  bucketMesh.forEach(ms => ms.computeLineDistances());
}
syncEdgePos();
// write the baked bezier highway arcs into their buckets (static — frozen
// layout means these never move, so this happens once at module scope,
// (static data, written once at module scope)
hw.forEach(([li, pts]) => {
  const arr = bucketPosIB[bucketOf[li]].array, base = hwSlot[li];
  for (let k = 0; k < 16; k++) {
    const o = base + k * 6, p = pts[k], q = pts[k + 1];
    arr[o] = p[0]; arr[o+1] = p[1]; arr[o+2] = p[2];
    arr[o+3] = q[0]; arr[o+4] = q[1]; arr[o+5] = q[2];
  }
});
bucketPosIB.forEach(ib => { ib.needsUpdate = true; });
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
// initial fill at full brightness (mirrors the pre-toggle startup state
// where no applyVisibility pass has run yet)
links.forEach((l, i) => {
  const dst = bucketColIB[bucketOf[i]].array;
  const src = eColBase.subarray(i*6, i*6+6);
  if (hwSlot[i] >= 0) {
    // highway: same color across all 16 segments (96 floats)
    for (let v = 0; v < 16; v++) dst.set(src, hwSlot[i] + v * 6);
  } else {
    dst.set(src, slotOf[i] * 6);
  }
});
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
  const dt = Math.min(0.05, (nowT - lastTickT) / 1000);
  lastTickT = nowT;
  if (window.__dbg) { window.__dbg.renderer = renderer; window.__dbg.camera = camera; }
  // dash-flow while focusing: one shared offset walks every edge along its
  // s→t vertex order (caller→callee). Overview keeps dashed fully off —
  // USE_DASH leaves the shader, so nothing shimmers at rest.
  const flowSpd = (window.__dbg && window.__dbg.flowSpeed) || FLOW_SPEED;
  for (const em of bucketMat) {
    if (edgeFlowOn) {
      if (!em.dashed) { em.dashed = true; em.dashSize = 8; em.gapSize = 5; em.needsUpdate = true; }
      em.dashOffset -= dt * flowSpd;
    } else if (em.dashed) {
      em.dashed = false; em.dashOffset = 0; em.needsUpdate = true;
    }
  }
  // camera tween (focus / back-stack); a user drag cancels it
  if (camTween) {
    const u = Math.min(1, (performance.now() - camTween.t0) / camTween.dur);
    const e = 1 - Math.pow(1 - u, 3);
    controls.target.lerpVectors(camTween.fromT, camTween.toT, e);
    camera.position.lerpVectors(camTween.fromC, camTween.toC, e);
    if (u >= 1) camTween = null;
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
  camera.position.copy(b.center).addScaledVector(dir, Math.max(420, b.radius * 2.2));
  controls.target.copy(b.center);
  // LOD threshold tracks the framing distance so overview stays overview
  // regardless of graph size
  lodDist = Math.max(420, b.radius * 1.8) * 0.9;
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
updateEdgeLegend(false);

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
      el.textContent = nm + " · " + members.length;
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
  const w = innerWidth, h = innerHeight;
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
      return (hubRects.every(hr => sep(r, hr)) && taken.every(t => sep(r, t))) ? r : null;
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
let hovered = -1, hoveredFn = -1, selected = -1;
let deadOnly = false, query = "";
let mutOnly = false;   // fn layer: show only functions that write state
const activeClusters = new Set();   // multi-select cluster filter (legend chips)
let showInst = false, showCalls = true, showSignals = true, showVar = false, depth = 2, fnMode = false;
// focus roots: single click replaces, shift-click stacks (BFS is multi-seed)
const focusSeeds = new Set();
// direction mode: 0 = both, 1 = out (downstream impact), 2 = in (upstream deps)
let dirMode = 0;
// tests/tools hidden by default (chip toggles them in); dir filter row works
// like the cluster chips — both only ever filter, never re-layout
// (declarations live above syncEdgePos: the boot-time geometry writer
// reads them through linkFiltered)
const isTestNode = n => n.path.startsWith("tests/") || n.path.startsWith("tools/") ||
  n.path.slice(n.path.lastIndexOf("/") + 1).startsWith("test_");
// overview LOD: intra-cluster edges stay hidden until the camera closes in
// (zoom threshold maintained by the controls 'change' listener below)
let lodClose = false, lodDist = 1e9;
const level = new Int16Array(N).fill(-1);

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
function applyVisibility() {
  const focusing = computeLevels();
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
    else if (focusing) a = level[i] < 0 ? 0.0 : (level[i] === 0 ? 1 : Math.max(0.16, 0.7 - level[i] * 0.18));
    else a = 1;
    alphaTgt[i] = a;
    if (a > 0.5) {
      const c = colorOf(nodes[i]);
      colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
    }
  }
  syncFileMesh();
  // dim edges: hidden endpoints, filtered types, or focus distance
  // (dimmed eColBase written straight into each bucket's instanced colors).
  // Overview palette: edge-type hues are demoted to weight-tinted gray so
  // cluster colors carry the overview; full type colors return on focus.
    const grayMix = focusing ? 0 : 0.82;
  const touched = [false, false, false];
  links.forEach((l, i) => {
    let k;
    // dir-filtered endpoints (tests/tools hidden, active dir isolation):
    // edges are killed outright, not dimmed — additive blending makes even
    // 1% gray visible when dozens of test edges converge on a hub
    const sFiltered = (!showTests && isTestNode(nodes[l.s])) ||
      (activeDirs.size && !activeDirs.has(nodes[l.s].dir));
    const tFiltered = (!showTests && isTestNode(nodes[l.t])) ||
      (activeDirs.size && !activeDirs.has(nodes[l.t].dir));
    // ghost: both endpoints outside the focus — kill outright (the arc
    // collapse in the k===0 branch below removes its baked geometry too)
    const ghost = alphaTgt[l.s] < 0.05 && alphaTgt[l.t] < 0.05;
    if (sFiltered || tFiltered || ghost) k = 0.0;
    else if (!typeVisible(l.ty) || alphaTgt[l.s] <= 0.5 || alphaTgt[l.t] <= 0.5) k = 0.012;
    else if (fnMode && focusing && l.ty === "call" && level[l.s] >= 0 && level[l.t] >= 0) k = 0; // wire mode: fn wires replace the aggregate call line; geometry collapse (k===0 branch) handles invisibility — a ghost 0.04 double-draws under the additive fn wires
    else if (focusing) k = Math.max(0.34, 1 - 0.18 * Math.max(level[l.s], level[l.t]));
    else k = 1;
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
      if (sameC) { if (k > 0.15) k = 0.15; }
      else if (el3 > 200 && hwSlot[i] < 0) k = 0.08;
    }
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
  if (focusing) {
    let lit = 0;
    for (let i = 0; i < N; i++) if (level[i] >= 0 && nodeVisible(nodes[i])) lit++;
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
  const w = innerWidth, h = innerHeight;
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
  const overview = !focusSeeds.size && !query &&
    camera.position.distanceTo(controls.target) >= lodDist;
  if (!overview) {
    xtLabs.forEach(k => { k.el.style.display = "none"; });
    return;
  }
  const w = innerWidth, h = innerHeight;
  for (const k of xtLabs) {
    hubV.set(k.mid[0], k.mid[1], k.mid[2]).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.05 || Math.abs(hubV.y) > 1.05) {
      k.el.style.display = "none"; continue;
    }
    k.el.style.display = "block";
    k.el.style.transform = "translate(" + ((hubV.x*0.5+0.5)*w).toFixed(1) + "px," +
      ((-hubV.y*0.5+0.5)*h).toFixed(1) + "px) translate(-50%,-50%)";
  }
}

// ---- hub labels: top-degree visible files, projected to screen each frame ---
const HUB_N = 12;
const hubsEl = document.getElementById("hubs");
let hubs = [];
const hubV = new THREE.Vector3();
function rebuildHubs() {
  const vis = [];
  for (let i = 0; i < N; i++) if (nodeVisible(nodes[i])) vis.push(i);
  vis.sort((a, b) => degree[b] - degree[a]);
  hubsEl.innerHTML = "";
  hubs = vis.slice(0, HUB_N).map(i => {
    const el = document.createElement("div");
    el.className = "hub";
    el.textContent = nodes[i].label + " · " + Math.round(degree[i]);
    el.title = nodes[i].path;
    el.onpointerenter = () => { tip.style.display = "none"; };
    el.onclick = () => { pushFocusState(); showInfo(i); focusSeeds.clear(); focusSeeds.add(i); applyVisibility(); focus(i); };
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
function updateHubs() {
  const w = innerWidth, h = innerHeight;
  // greedy placement against real measured boxes; transforms only touch
  // these few absolutely-positioned nodes, so rect reads stay cheap.
  // vertical rows first (keeps label near its node), then sideways nudges
  const fixed = [];
  const free = (a, b) => a.right < b.left - 4 || b.right < a.left - 4 ||
    a.bottom < b.top - 4 || b.bottom < a.top - 4;
  for (const { i, el } of hubs) {
    if (alphaTgt[i] < 0.5) { el.style.display = "none"; hubOff.delete(i); continue; }
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
      if (fixed.every(f => free(r, f))) { fixed.push(r); continue; }
    }
    outer:
    for (const dy of [-19, 17, -42, 41, -65, 65, -88, 88]) {
      for (const dx of [0, 100, -100]) {
        el.style.transform = "translate(" + (x + dx).toFixed(1) + "px," +
          (y + dy).toFixed(1) + "px) translate(-50%,0)";
        r = el.getBoundingClientRect();
        if (fixed.every(f => free(r, f))) { hubOff.set(i, { dx, dy }); break outer; }
      }
    }
    fixed.push(r);
  }
}
rebuildHubs();

// ---- function-level layer (files inside the current focus) -------------------
let fnMesh = null, fnLines = null, fnMeta = [];

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
      const io = DATA.fio && DATA.fio[nodes[m.file].path + "::" + m.name];
      const el = document.createElement("div");
      el.className = "flab fn";
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
  const w = innerWidth, h = innerHeight;
  const clearOf = (a, b) => a.right < b.left - 2 || b.right < a.left - 2 ||
    a.bottom < b.top - 2 || b.bottom < a.top - 2;
  const hubRects = [...document.querySelectorAll(".hub")]
    .filter(e => e.style.display === "block").map(e => e.getBoundingClientRect());
  const taken = [];
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
    // fn satellites may overlap each other (they're small + attached to
    // distinct wire points) — they only avoid hubs + file labels
    if (f.kind === 1) { taken.push(f.el.getBoundingClientRect()); continue; }
    let r = f.el.getBoundingClientRect();
    if (hubRects.some(hr => !clearOf(r, hr)) || taken.some(t => !clearOf(r, t))) {
      let ok = false;
      for (const dy of [16, -14, 32, -30]) {
        f.el.style.transform = "translate(" + x.toFixed(1) + "px," + (y + dy).toFixed(1) + "px) translate(-50%,-100%)";
        r = f.el.getBoundingClientRect();
        if (hubRects.every(hr => clearOf(r, hr)) && taken.every(t => clearOf(r, t))) { ok = true; break; }
      }
      if (!ok) { f.el.style.display = "none"; continue; }
    }
    taken.push(r);
  }
}
rebuildFocusLabels(false);
function rebuildFnLayer(focusing) {
  if (fnMesh) { scene.remove(fnMesh); fnMesh.geometry.dispose(); fnMesh.dispose(); fnMesh = null; }
  if (fnLines) { scene.remove(fnLines); fnLines.geometry.dispose(); fnLines = null; }
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
  if (!visEdges.length) return;
  // pass 2: fn nodes live ON the wire between their two file nodes.
  // Unique fns on a wire get evenly spaced slots in [0.32, 0.68]; a fn shared
  // across wires is positioned on its first wire (random hash placement made
  // boxes overlap; per-call slot counting pushed shared boxes past the wire).
  const keySlot = new Map();   // fn key -> { a, b, slot, n }
  {
    const wireKeys = new Map();  // "a|b" -> [unique fn keys in edge order]
    for (const e of visEdges) {
      const wk = e[0] + "|" + e[2];
      let arr = wireKeys.get(wk);
      if (!arr) { arr = []; wireKeys.set(wk, arr); }
      for (const kk of [e[0] + "::" + e[1], e[2] + "::" + e[3]]) {
        if (!keySlot.has(kk) && !arr.includes(kk)) {
          keySlot.set(kk, { a: e[0], b: e[2], slot: arr.length, wk });
          arr.push(kk);
        }
      }
    }
    for (const [kk, s] of keySlot) {
      s.n = wireKeys.get(s.wk).length;
      s.t = 0.22 + (s.n > 1 ? (0.56 * s.slot) / (s.n - 1) : 0.28);
      // baked positions are rounded to integers, so wires A->B and B->C can
      // be near-collinear: along-wire jitter can't separate boxes there.
      // Offset perpendicular to the wire instead (±7 units — reads as
      // on-wire at box size 4, deterministic by fn name). The relaxation
      // pass below grows these offsets further when wires bunch up.
      let h = 2166136261;
      const nm = kk.slice(kk.indexOf("::") + 2);
      for (let c = 0; c < nm.length; c++) { h ^= nm.charCodeAt(c); h = Math.imul(h, 16777619); }
      let dx = pos[s.b*3] - pos[s.a*3], dy = pos[s.b*3+1] - pos[s.a*3+1], dz = pos[s.b*3+2] - pos[s.a*3+2];
      const len = Math.sqrt(dx*dx + dy*dy + dz*dz) || 1;
      s.dx = dx / len; s.dy = dy / len; s.dz = dz / len; s.len = len;
      // perp = dir x (0,1,0); fallback dir x (1,0,0) for vertical wires
      let px = -s.dz, py = 0, pz = s.dx;
      if (px*px + py*py + pz*pz < 1e-6) { px = s.dy; py = -s.dx; pz = 0; }
      const pl = Math.sqrt(px*px + py*py + pz*pz) || 1;
      const off = (((h >>> 0) % 1000) / 1000 - 0.5) * 14;
      s.ox = (px / pl) * off; s.oy = (py / pl) * off; s.oz = (pz / pl) * off;
      s.t = Math.min(0.78, Math.max(0.22, s.t + (((h >>> 9) % 1000) / 1000 - 0.5) * 0.04));
    }
  }
  const fIdx = new Map(), fpos = [], fcol = [], eidx = [];
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
      const s = keySlot.get(k);
      const px = pos[s.a*3] + (pos[s.b*3] - pos[s.a*3]) * s.t + (s.ox || 0);
      const py = pos[s.a*3+1] + (pos[s.b*3+1] - pos[s.a*3+1]) * s.t + (s.oy || 0);
      const pz = pos[s.a*3+2] + (pos[s.b*3+2] - pos[s.a*3+2]) * s.t + (s.oz || 0);
      fnMeta.push({ file: fi, name, p: [px, py, pz], s, dir: (name.charCodeAt(0) & 1) ? 1 : -1 });
      fpos.push(px, py, pz);
      fcol.push(colArr[fi*3], colArr[fi*3+1], colArr[fi*3+2]);
    }
    return ix;
  };
  visEdges.forEach(e => {
    const a = nodeOf(e, true), b = nodeOf(e, false);
    if (a < 0 || b < 0) return;   // filtered out by mutators-only
    eidx.push(a, b);
  });
  // relaxation: push overlapping fn boxes apart, then re-project each box
  // into its own wire corridor (t clamp [0.22,0.78], perp offset cap ±10).
  // The old nudge-only pass enforced 2-unit separation while a scale-4 box
  // spans ~3.5 — legal overlap; near-parallel wire clumps need a real
  // all-pairs relax. Deterministic order; O(n²) trivial at fn-layer sizes.
  const FN_SEP = 7, PERP_MAX = 10, T_MIN = 0.22, T_MAX = 0.78;
  const reproject = (m, ix) => {
    const s = m.s;
    let rx = m.p[0] - pos[s.a*3], ry = m.p[1] - pos[s.a*3+1], rz = m.p[2] - pos[s.a*3+2];
    let t = (rx*s.dx + ry*s.dy + rz*s.dz) / (s.len || 1);
    t = Math.min(T_MAX, Math.max(T_MIN, t));
    let bx = pos[s.a*3] + s.dx * s.len * t, by = pos[s.a*3+1] + s.dy * s.len * t, bz = pos[s.a*3+2] + s.dz * s.len * t;
    let ox = m.p[0] - bx, oy = m.p[1] - by, oz = m.p[2] - bz;
    const ol = Math.sqrt(ox*ox + oy*oy + oz*oz);
    if (ol > PERP_MAX) { const k = PERP_MAX / ol; ox *= k; oy *= k; oz *= k; }
    s.t = t; s.ox = ox; s.oy = oy; s.oz = oz;
    m.p[0] = bx + ox; m.p[1] = by + oy; m.p[2] = bz + oz;
    fpos[ix*3] = m.p[0]; fpos[ix*3+1] = m.p[1]; fpos[ix*3+2] = m.p[2];
  };
  // keep fn boxes out of the file spheres: wires start at sphere CENTERS,
  // so boxes near the wire ends sit inside big hub spheres (radius up to 11).
  // Radial push to r + box-margin; runs after reprojection each iteration and
  // once more at the end (sphere escape wins over the corridor clamp).
  const sphereClear = (prev) => {
    let moved = prev;
    for (let i = 0; i < fnMeta.length; i++) {
      const m = fnMeta[i];
      for (let f = 0; f < N; f++) {
        const r = sizes[f] * 1.1 + 3.4;   // rendered radius + box half-diagonal margin
        const dx = m.p[0] - pos[f*3], dy = m.p[1] - pos[f*3+1], dz = m.p[2] - pos[f*3+2];
        const d2 = dx*dx + dy*dy + dz*dz;
        if (d2 >= r*r) continue;
        const d = Math.sqrt(d2) || 0.01;
        const k = (r - d) / d;
        m.p[0] += dx*k; m.p[1] += dy*k; m.p[2] += dz*k;
        moved = true;
      }
      fpos[i*3] = m.p[0]; fpos[i*3+1] = m.p[1]; fpos[i*3+2] = m.p[2];
    }
    return moved;
  };
  for (let it = 0; it < 120; it++) {
    let moved = false;
    for (let a = 1; a < fnMeta.length; a++) {
      const pa = fnMeta[a].p;
      for (let b = 0; b < a; b++) {
        const pb = fnMeta[b].p;
        let dx = pa[0]-pb[0], dy = pa[1]-pb[1], dz = pa[2]-pb[2];
        const d2 = dx*dx + dy*dy + dz*dz;
        if (d2 >= FN_SEP*FN_SEP) continue;
        let d = Math.sqrt(d2);
        if (d < 1e-3) { dx = 1; dy = 0; dz = 0; d = 1; }
        const push = (FN_SEP - d) / d * 0.5;
        pa[0] += dx*push; pa[1] += dy*push; pa[2] += dz*push;
        pb[0] -= dx*push; pb[1] -= dy*push; pb[2] -= dz*push;
        moved = true;
      }
    }
    for (let i = 0; i < fnMeta.length; i++) reproject(fnMeta[i], i);
    moved = sphereClear(moved) || moved;
    if (!moved) break;
  }
  sphereClear(false);
  if (!fnMeta.length) return;
  // fn boxes: true 3D cubes on the wires, colored by owning cluster hue
  const fdummy = new THREE.Object3D();
  fnMesh = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshLambertMaterial(),
    fnMeta.length
  );
  fnMesh.frustumCulled = false;   // positions rebuilt on every focus change
  for (let i = 0; i < fnMeta.length; i++) {
    fdummy.position.set(fpos[i*3], fpos[i*3+1], fpos[i*3+2]);
    fdummy.scale.setScalar(4);
    fdummy.updateMatrix();
    fnMesh.setMatrixAt(i, fdummy.matrix);
    _col.setRGB(fcol[i*3], fcol[i*3+1], fcol[i*3+2]);
    fnMesh.setColorAt(i, _col);
  }
  fnMesh.instanceMatrix.needsUpdate = true;
  if (fnMesh.instanceColor) fnMesh.instanceColor.needsUpdate = true;
  scene.add(fnMesh);
  // wires connect box to box — collinear with the file-file line (whose
  // fat aggregate dims to a ghost in wire mode), so the lit segments
  // thread through the fn boxes: the boxes ARE on the connection
  const ep = [], ec = [];
  for (let i = 0; i < eidx.length; i += 2) {
    const a = eidx[i], b = eidx[i+1];
    ep.push(fpos[a*3], fpos[a*3+1], fpos[a*3+2], fpos[b*3], fpos[b*3+1], fpos[b*3+2]);
    ec.push(fcol[a*3], fcol[a*3+1], fcol[a*3+2], fcol[b*3], fcol[b*3+1], fcol[b*3+2]);
  }
  const g2 = new THREE.BufferGeometry();
  g2.setAttribute("position", new THREE.BufferAttribute(new Float32Array(ep), 3));
  g2.setAttribute("color", new THREE.BufferAttribute(new Float32Array(ec), 3));
  fnLines = new THREE.LineSegments(g2, new THREE.LineBasicMaterial({
    vertexColors: true, transparent: true, opacity: 0.8,
    blending: THREE.AdditiveBlending, depthWrite: false }));
  scene.add(fnLines);
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
document.getElementById("bGround").onclick = e => {
  showGround = !showGround;
  groundGrid.visible = showGround;
  e.target.classList.toggle("on", showGround);
};
const searchEl = document.getElementById("search");
const depthEl = document.getElementById("depth");
const cbFnEl = document.getElementById("cbFn");
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
function clearFocus() {
  // one scope for Esc / right-click / crumb ✕: drop the focus, the query,
  // the back-stack and the info panel together
  focusSeeds.clear(); query = "";
  focusStack = [];
  document.getElementById("search").value = "";
  info.style.display = "none";
  applyVisibility();
}
addEventListener("keydown", e => {
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
  deadOnly = false; query = ""; focusSeeds.clear(); focusStack = [];
  dirMode = 0; showSignals = true; showVar = false; fnMode = false; depth = 2;
  mutOnly = false;
  showInst = false; showCalls = true; showTests = false;
  groupsMode = false;   // coloring level is view state — reset to fine clusters
  searchEl.value = ""; depthEl.value = 2;
  document.getElementById("depthVal").textContent = "2";
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
  selected = i;
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
  // tween the camera to a tight orbit around node i (400ms ease-out)
  const to = new THREE.Vector3(pos[i*3], pos[i*3+1], pos[i*3+2]);
  const dir = new THREE.Vector3(camera.position.x - to.x,
    camera.position.y - to.y, camera.position.z - to.z).normalize();
  tweenCamTo(to, to.clone().addScaledVector(dir, 320));
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
    const shown = entries.slice(0, 24);
    shown.forEach(e => {
      const li = document.createElement("li");
      const j = kindId === "kUses" ? e[2] : e[0];
      li.textContent = nodes[j].label + " :: " + (kindId === "kUses" ? e[3] : e[1]);
      li.onclick = () => jumpFn(j);
      ul.appendChild(li);
    });
    if (entries.length > shown.length) {
      const li = document.createElement("li");
      li.className = "more";
      li.textContent = `+${entries.length - shown.length} more hidden`;
      ul.appendChild(li);
    }
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

// fn-ownership affordance: hovering a fn box draws a stalk from the box to
// its owning file — with two files close together, color alone can't say
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

renderer.domElement.addEventListener("pointermove", e => {
  mouse.x = (e.clientX/innerWidth)*2-1; mouse.y = -(e.clientY/innerHeight)*2+1;
  raycaster.setFromCamera(mouse, camera);
  const targets = fnMesh ? [fileMesh, fnMesh] : [fileMesh];
  const hits = raycaster.intersectObjects(targets);
  hovered = -1; hoveredFn = -1;
  // skip invisible nodes: filtered-out tests/tools keep raycast geometry,
  // but hovering a ghost must not pop a tooltip. Among visible hits, pick
  // by SCREEN-SPACE accuracy (cursor distance vs projected radius), not
  // depth — at high spread a foreground sphere's rim otherwise steals the
  // pick from the node the user is actually pointing at (occlusion).
  const px = e.clientX, py = e.clientY;
  let bestPx = 18;   // cursor forgiveness radius in pixels
  for (const h of hits) {
    if (h.object === fnMesh) {
      const fm = fnMeta[h.instanceId];
      if (!fm || alphaTgt[fm.file] <= 0.5) continue;
      const v = _pickV.set(fm.p[0], fm.p[1], fm.p[2]).project(camera);
      const sx = (v.x*0.5+0.5)*innerWidth, sy = (-v.y*0.5+0.5)*innerHeight;
      const dist = Math.hypot(sx-px, sy-py);
      if (dist < bestPx) { bestPx = dist; hoveredFn = h.instanceId; hovered = -1; }
    } else if (h.object === fileMesh && alphaTgt[h.instanceId] > 0.5) {
      const v = _pickV.set(pos[h.instanceId*3], pos[h.instanceId*3+1], pos[h.instanceId*3+2]).project(camera);
      const sx = (v.x*0.5+0.5)*innerWidth, sy = (-v.y*0.5+0.5)*innerHeight;
      const dist = Math.hypot(sx-px, sy-py);
      if (dist < bestPx) { bestPx = dist; hovered = h.instanceId; hoveredFn = -1; }
    }
  }
  if (hoveredFn >= 0) fnStalkUpdate(fnMeta[hoveredFn].file, fnMeta[hoveredFn].p);
  else fnStalkHide();
  let txt = null;
  if (hoveredFn >= 0) {
    const fm = fnMeta[hoveredFn];
    txt = nodes[fm.file].path + " :: " + fm.name;
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
renderer.domElement.addEventListener("click", e => {
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (hoveredFn >= 0) { showFnInfo(hoveredFn); return; }
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
    } else {
      pushFocusState();
      focusSeeds.clear(); focusSeeds.add(hovered);
      applyVisibility();
      focus(hovered);
      showInfo(hovered);
    }
  }
});
addEventListener("resize", () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  bucketMat.forEach(m => m.resolution.set(innerWidth, innerHeight));
});
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
// debug handle last: everything it captures is initialized by here
window.__dbg = { pos, nodes, links, fedges, syncEdgePos, renderer, camera, THREE, flowSpeed: FLOW_SPEED,
  meta: DATA.meta, controls, get spinEnabled() { return spinEnabled; },
  alpha: alphaArr, alphaTgt, hoverScale, hot, bucketMat, bucketOf, hwSlot, bucketPosIB, bucketColIB, slotOf,
  adjOut, adjIn, adj, outDeg, inDeg, get dirMode() { return dirMode; }, focusSeeds, level,
  get camTween() { return camTween; }, get focusStack() { return focusStack; },
  get fileMesh() { return fileMesh; }, get fnMesh() { return fnMesh; },
  get controls() { return controls; },
  get fnMeta() { return fnMeta; },
  get hovered() { return hovered; },
  get groundGrid() { return groundGrid; },
  get groupsMode() { return groupsMode; }, groups,
  get spread() { return spread; }, get deadOnly() { return deadOnly; },
  colArr,
  get fnStalk() { return fnStalk; },
  syncFileMesh };
tick();
</script>
</body>
</html>
"""


def generate(out: str | Path | None = None) -> Path:
    out = Path(out) if out else Path(__file__).resolve().parent / "graph.html"
    data = _build_data()
    html = _TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    path = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(path)
