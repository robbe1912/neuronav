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
import sys
from collections import defaultdict
from pathlib import Path

import nav
import graph


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
    except Exception:
        sims = []

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
    # settle, identical output across regenerations). Falls back to the old
    # in-browser sim only if the offline pass somehow fails.
    pos_baked = None
    try:
        pos_baked = _layout(
            len(nodes), links, sims, [nd["cluster"] for nd in nodes],
            ckeys=ckeys, cmat=cmat,
        )
    except Exception:
        pos_baked = None

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
                # deterministic perpendicular bow (12% of chord length) so
                # every highway reads as a curve
                ch = P[l["t"]] - P[l["s"]]
                chl = float(_np.linalg.norm(ch))
                perp = _np.cross(ch, [0.0, 0.0, 1.0])
                pl = float(_np.linalg.norm(perp))
                if pl < 0.001:
                    perp = _np.array([1.0, 0.0, 0.0])
                else:
                    perp = perp / pl
                ctrl = ctrl + perp * (chl * 0.2)
                pts = []
                for k in range(17):
                    u = k / 16.0
                    p = (1-u)*(1-u)*P[l["s"]] + 2*(1-u)*u*ctrl + u*u*P[l["t"]]
                    pts.append([round(float(x), 1) for x in p])
                hw.append([li, pts])
        except Exception:
            hw = []

    return {
        "nodes": nodes,
        "links": links,
        "fedges": fedges,
        "sims": sims,
        "hw": hw,
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
        },
    }


_TRACE: list = []   # debug: (step, cluster centroids snapshot) every 50 steps


def _layout(n: int, links: list, sims: list, cluster_ids: list,
            ckeys: list = None, cmat: list = None) -> list:
    """Deterministic offline force layout; positions are frozen into DATA.

    Mirrors the constants the in-browser sim was QA'd against, plus the
    redesign rules: hub-weighted repulsion (hubs earn breathing room),
    weak single-call springs cut from layout only (they still render),
    semantic springs after a structure-first phase, cluster gravity,
    velocity clamp against transient spikes. Seeded RNG => the same DATA
    always produces the same picture.
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

    # hub-weighted repulsion coefficients: sqrt-degree product normalised by
    # the mean, so an average pair repels like the old uniform 52000 while
    # hub-hub pairs push much harder (hubs stop drowning in the core)
    dbar = float(deg.mean()) + 1.0
    ds = np.sqrt(deg + 1.0).astype(np.float32)
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
        # cluster gravity toward per-cluster centroid
        csum = np.zeros((nc, 3), dtype=np.float32)
        np.add.at(csum, cinv, pos)
        cen = csum / ccount[:, None]
        if step % 50 == 0:
            _TRACE.append((step, cen.copy()))
        vel += (cen[cinv] - pos) * 0.006
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
  #toggles { margin-top:10px; display:flex; gap:6px; }
  button { flex:1; background:#0b1116; color:#b0bec5; border:1px solid #263238;
    border-radius:6px; padding:5px 0; cursor:pointer; font-size:11px; }
  button.on { color:#1de9b6; border-color:#1de9b688; }
  #info { position:fixed; top:12px; right:12px; z-index:10; width:290px;
    background:rgba(10,14,18,.88); border:1px solid #1de9b633; border-radius:10px;
    padding:12px; display:none; backdrop-filter: blur(4px); }
  #info h2 { font-size:13px; margin:0 0 4px; color:#fff; word-break:break-all; }
  #info .sub { font-size:11px; color:#78909c; margin-bottom:8px; }
  #info .tag { display:inline-block; font-size:10px; padding:1px 7px;
    border-radius:8px; margin:0 4px 6px 0; }
  #info ul { list-style:none; margin:6px 0 0; padding:0; max-height:34vh;
    overflow-y:auto; }
  #info li { padding:3px 6px; border-radius:5px; cursor:pointer; font-size:11.5px; }
  #info li:hover { background:#1de9b61a; color:#1de9b6; }
  .kind { color:#546e7a; font-size:10px; text-transform:uppercase;
    letter-spacing:1px; margin-top:8px; }
  #tip { position:fixed; z-index:20; pointer-events:none; display:none;
    background:#000d; border:1px solid #1de9b644; color:#eee; font-size:11px;
    padding:4px 8px; border-radius:6px; white-space:pre-line; }
  #edgeLegend { display:flex; flex-wrap:wrap; gap:3px 10px; margin-top:6px;
    font-size:10px; color:#78909c; }
  .eKey { display:flex; align-items:center; gap:4px; }
  .eKey i { width:14px; border-top:2px solid #ffffff55; display:inline-block; }
  #crumb { position:fixed; top:12px; left:50%; transform:translateX(-50%);
    z-index:10; display:none; align-items:center; gap:6px;
    background:rgba(10,14,18,.82); border:1px solid #1de9b633;
    border-radius:14px; padding:4px 7px 4px 14px; font-size:11px; color:#b0bec5; }
  #crumb b { color:#1de9b6; font-weight:600; }
  #crumb .x { cursor:pointer; color:#78909c; padding:0 6px; border-radius:50%;
    line-height:1.3; }
  #crumb .x:hover { color:#fff; background:#ffffff14; }
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
    font-size:9.5px; padding:0 5px; border-radius:5px;
    background:rgba(8,12,16,.75); pointer-events:none; }
  #clabs { position:fixed; inset:0; z-index:3; pointer-events:none;
    overflow:hidden; }
  .clab { position:absolute; left:0; top:0; display:none; white-space:nowrap;
    font-size:12px; font-weight:700; letter-spacing:1.5px; opacity:.95;
    text-shadow:0 0 6px #000, 0 1px 3px #000, 0 0 12px #000; }
</style>
</head>
<body>
<div id="panel">
  <h1>neuronav</h1>
  <div id="stats"></div>
  <div id="edgeLegend"></div>
  <input id="search" placeholder="search file / class…">
  <div id="depthRow">
    <span>depth</span>
    <input id="depth" type="range" min="1" max="3" value="2">
    <span id="depthVal">2</span>
    <label class="cb"><input type="checkbox" id="cbFn"> functions</label>
  </div>
  <div id="legend"></div>
 <div id="dirs"></div>
  <div id="toggles">
    <button id="bCalls" class="on">calls</button>
    <button id="bInst">contains</button>
 <button id="bDead">dead</button>
 <button id="bSpin">spin</button>
 <button id="bReset">reset</button>
  </div>
</div>
<div id="crumb"></div>
<div id="info">
  <h2 id="iTitle"></h2>
  <div class="sub" id="iSub"></div>
  <div id="iTags"></div>
  <div class="kind">connections</div>
  <ul id="iLinks"></ul>
</div>
<div id="tip"></div>
<div id="hubs"></div>
<div id="elabs"></div>
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
const nodes = DATA.nodes, links = DATA.links, fedges = DATA.fedges || [], sims = DATA.sims || [], hw = DATA.hw || [];
const N = nodes.length;
// physics: one spring per file pair (typed duplicates would triple forces);
// render/filter iterate all typed links
const seenPair = new Set();
const physLinks = links.filter(l => {
  const k = l.s < l.t ? l.s + "_" + l.t : l.t + "_" + l.s;
  if (seenPair.has(k)) return false;
  seenPair.add(k);
  return true;
});
// undirected adjacency for focus BFS
const adj = Array.from({ length: N }, () => []);
links.forEach(l => { adj[l.s].push(l.t); adj[l.t].push(l.s); });
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
// gentle idle rotation: OFF by default, opt-in via the spin toggle
controls.autoRotate = false;
controls.autoRotateSpeed = 0.35;

// cluster hue: golden angle spread; dead files tinted toward red
const hue = c => c < 0 ? 0.08 : (c * 0.61803398875 + 0.55) % 1;
const colorOf = n => {
  const col = new THREE.Color().setHSL(hue(n.cluster), 0.72, 0.58);
  if (n.dead > 0) col.lerp(new THREE.Color(0.95, 0.12, 0.12), n.dead >= 1 ? 0.85 : 0.68);
  return col;
};

const pos = new Float32Array(N * 3);
const colArr = new Float32Array(N * 3);
const sizes = new Float32Array(N);
const degree = new Float32Array(N);
// frozen baked layout: positions were settled offline in Python (seeded,
// deterministic) — the browser only renders; no live global sim
const frozenPos = Array.isArray(DATA.pos) ? DATA.pos : null;
links.forEach(l => { degree[l.s] += l.w; degree[l.t] += l.w; });
nodes.forEach((n, i) => {
  if (frozenPos) {
    pos[i*3] = frozenPos[i][0]; pos[i*3+1] = frozenPos[i][1]; pos[i*3+2] = frozenPos[i][2];
  } else {
    pos[i*3] = (Math.random()-0.5) * 1600;
    pos[i*3+1] = (Math.random()-0.5) * 900;
    pos[i*3+2] = (Math.random()-0.5) * 1600;
  }
  const c = colorOf(n);
  colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
  sizes[i] = Math.min(18, 6 + Math.sqrt(degree[i]) * 1.8);
});

const pGeo = new THREE.BufferGeometry();
const alphaArr = new Float32Array(N).fill(1);
pGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
pGeo.setAttribute("color", new THREE.BufferAttribute(colArr, 3));
pGeo.setAttribute("psize", new THREE.BufferAttribute(sizes, 1));
pGeo.setAttribute("aalpha", new THREE.BufferAttribute(alphaArr, 1));
const pMat = new THREE.ShaderMaterial({
  transparent: true, depthWrite: false, vertexColors: true,
  vertexShader: `
    attribute float psize; attribute float aalpha;
    varying vec3 vColor; varying float vA;
    void main(){ vColor = color; vA = aalpha;
      vec4 mv = modelViewMatrix * vec4(position,1.0);
      // hidden nodes (vA==0) get zero size: GL discards them entirely
      gl_PointSize = vA < 0.01 ? 0.0 : max(psize * (900.0 / -mv.z), 5.0);
      gl_Position = projectionMatrix * mv; }`,
  fragmentShader: `
    varying vec3 vColor; varying float vA;
    void main(){ float d = length(gl_PointCoord - 0.5);
      // crisp disc: solid center, tight 2px anti-aliased rim — no fuzzy glow
      float a = (1.0 - smoothstep(0.36, 0.47, d)) * vA;
      if (a < 0.01) discard;
      gl_FragColor = vec4(vColor, a); }`,
});
scene.add(new THREE.Points(pGeo, pMat));

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
const BUCKETS = [
  { max: 1, width: 1.3, op: 0.42 },          // w <= 1
  { max: 4, width: 2.2, op: 0.34 },          // 2..4
  { max: Infinity, width: 3.5, op: 0.26 },   // >= 5
];
const bucketOf = new Int8Array(MAXL);
const slotOf = new Int32Array(MAXL);
const hwSlot = new Int32Array(MAXL).fill(-1);
const bucketPosIB = [], bucketColIB = [], bucketMat = [];
{
  const counts = [0, 0, 0];
  links.forEach((l, i) => {
    const b = BUCKETS.findIndex(x => l.w <= x.max);
    bucketOf[i] = b; slotOf[i] = counts[b]++;
  });
  // highways: each bezier arc is 16 static segments (32 vertices, 96 floats)
  // appended after the straight links inside its bucket. hwSlot[i] is the
  // vertex-float base of link i's arc, or -1 for straight links.
  const hwCounts = [0, 0, 0];
  hw.forEach(([li, pts]) => {
    const b = bucketOf[li];
    hwSlot[li] = (counts[b] * 2 + hwCounts[b] * 32) * 3;
    hwCounts[b] += 32;
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
  });
}
// typed strands between the same file pair run parallel instead of
// overlapping: each gets a slot offset perpendicular to the strand
const pairKey = (s, t) => s < t ? s + "_" + t : t + "_" + s;
const pairLinks = new Map();
links.forEach((l, i) => {
  const k = pairKey(l.s, l.t);
  if (!pairLinks.has(k)) pairLinks.set(k, []);
  pairLinks.get(k).push(i);
});
function syncEdgePos() {
  links.forEach((l, i) => {
    if (hwSlot[i] >= 0) return;   // highway arcs are baked, never resynced
    const s = l.s * 3, t = l.t * 3;
    // hidden endpoints (tests/tools, dir filter): collapse to a degenerate
    // zero-length segment — true render disable, no stray pixels at all
    if (alphaArr[l.s] < 0.01 || alphaArr[l.t] < 0.01) {
      const a0 = bucketPosIB[bucketOf[i]].array, o0 = slotOf[i] * 6;
      a0[o0] = pos[s]; a0[o0+1] = pos[s+1]; a0[o0+2] = pos[s+2];
      a0[o0+3] = pos[s]; a0[o0+4] = pos[s+1]; a0[o0+5] = pos[s+2];
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
    a[o]   = pos[s] + ox; a[o+1] = pos[s+1] + oy; a[o+2] = pos[s+2];
    a[o+3] = pos[t] + ox; a[o+4] = pos[t+1] + oy; a[o+5] = pos[t+2];
  });
  bucketPosIB.forEach(ib => { ib.needsUpdate = true; });
}
syncEdgePos();
// write the baked bezier highway arcs into their buckets (static — frozen
// layout means these never move, so this happens once at module scope,
// NOT inside tick()'s running block which frozen layouts never execute)
hw.forEach(([li, pts]) => {
  const arr = bucketPosIB[bucketOf[li]].array, base = hwSlot[li];
  for (let k = 0; k < 16; k++) {
    const o = base + k * 6, p = pts[k], q = pts[k + 1];
    arr[o] = p[0]; arr[o+1] = p[1]; arr[o+2] = p[2];
    arr[o+3] = q[0]; arr[o+4] = q[1]; arr[o+5] = q[2];
  }
});
bucketPosIB.forEach(ib => { ib.needsUpdate = true; });
// base colors: pure type hue (no endpoint blend) scaled by weight-as-
// brightness; width granularity stops at the bucket boundaries
const eColBase = new Float32Array(MAXL * 6);
links.forEach((l, i) => {
  const tc = TYPE_COLORS[l.ty] || TYPE_COLORS.var;
  const wb = 0.45 + Math.min(1, l.w / 6) * 0.55;
  const o = i * 6;
  eColBase[o]   = tc.r * wb; eColBase[o+1] = tc.g * wb; eColBase[o+2] = tc.b * wb;
  eColBase[o+3] = tc.r * wb; eColBase[o+4] = tc.g * wb; eColBase[o+5] = tc.b * wb;
});
// initial fill at full brightness (mirrors the pre-toggle startup state
// where no applyVisibility pass has run yet)
links.forEach((l, i) => {
  const dst = bucketColIB[bucketOf[i]].array;
  const src = eColBase.subarray(i*6, i*6+6);
  if (hwSlot[i] >= 0) {
    // highway: same color across all 16 segments
    for (let v = 0; v < 32; v++) dst.set(src, hwSlot[i] + v * 6);
  } else {
    dst.set(src, slotOf[i] * 6);
  }
});
bucketColIB.forEach(ib => { ib.needsUpdate = true; });

// ---- force layout ------------------------------------------------------------
const vel = new Float32Array(N * 3);
let alpha = 1.0, settled = 0, frameNo = 0;
function step() {
  alpha *= 0.995;
  // pairwise repulsion (grid-bucketed would be faster; N is small)
  for (let i = 0; i < N; i++) for (let j = i+1; j < N; j++) {
    let dx = pos[j*3]-pos[i*3], dy = pos[j*3+1]-pos[i*3+1], dz = pos[j*3+2]-pos[i*3+2];
    let d2 = dx*dx + dy*dy + dz*dz + 1.0;
    if (d2 > 2500000) continue;
    // clamp: a near-coincident pair would otherwise fire a 1/d^3 spike that
    // catapults nodes far outside the graph (stochastic layout explosion)
    let f = Math.min(52000 / d2, 400);
    dx *= f/d2; dy *= f/d2; dz *= f/d2;
    vel[i*3] -= dx; vel[i*3+1] -= dy; vel[i*3+2] -= dz;
    vel[j*3] += dx; vel[j*3+1] += dy; vel[j*3+2] += dz;
  }
  // springs — connected systems must actually sit close together
  physLinks.forEach(l => {
    let dx = pos[l.t*3]-pos[l.s*3], dy = pos[l.t*3+1]-pos[l.s*3+1], dz = pos[l.t*3+2]-pos[l.s*3+2];
    let d = Math.sqrt(dx*dx+dy*dy+dz*dz) + 0.01;
    // heavy/multiple-typed connections pull tight; composition looser
    const compo = l.ty === "inst" || l.ty === "attach";
    const rest = compo ? 230 : Math.max(65, 165 / (1 + l.w * 0.5));
    const f = (d - rest) / d * 0.02 * Math.min(3, 1 + l.w * 0.3);
    dx *= f; dy *= f; dz *= f;
    vel[l.s*3] += dx; vel[l.s*3+1] += dy; vel[l.s*3+2] += dz;
    vel[l.t*3] -= dx; vel[l.t*3+1] -= dy; vel[l.t*3+2] -= dz;
  });
  // semantic springs — pull embedding-similar files together once the
  // structural layout has rough shape (frame >= 120). Layout-only: sims
  // are not part of links/adj/degree, so search, hubs, panel are unaffected.
  if (frameNo >= 120) sims.forEach(p => {
    let dx = pos[p[0]*3]-pos[p[1]*3], dy = pos[p[0]*3+1]-pos[p[1]*3+1], dz = pos[p[0]*3+2]-pos[p[1]*3+2];
    let d = Math.sqrt(dx*dx+dy*dy+dz*dz) + 0.01;
    const rest = 620 * (1 - p[2]);
    const f = (d - rest) / d * 0.006;
    dx *= f; dy *= f; dz *= f;
    vel[p[0]*3] -= dx; vel[p[0]*3+1] -= dy; vel[p[0]*3+2] -= dz;
    vel[p[1]*3] += dx; vel[p[1]*3+1] += dy; vel[p[1]*3+2] += dz;
  });
  // cluster gravity: pull members toward their cluster centroid (keeps
  // subsystems visibly separated even when weak inter-cluster links chain)
  const ccx = {}, ccy = {}, ccz = {}, ccn = {};
  for (let i = 0; i < N; i++) {
    const c = nodes[i].cluster;
    ccx[c] = (ccx[c]||0) + pos[i*3]; ccy[c] = (ccy[c]||0) + pos[i*3+1]; ccz[c] = (ccz[c]||0) + pos[i*3+2]; ccn[c] = (ccn[c]||0) + 1;
  }
  for (const c in ccx) { ccx[c] /= ccn[c]; ccy[c] /= ccn[c]; ccz[c] /= ccn[c]; }
  for (let i = 0; i < N; i++) {
    const c = nodes[i].cluster;
    vel[i*3]   += (ccx[c] - pos[i*3])   * 0.006;
    vel[i*3+1] += (ccy[c] - pos[i*3+1]) * 0.006;
    vel[i*3+2] += (ccz[c] - pos[i*3+2]) * 0.006;
  }
  // integrate + center gravity + damping
  for (let i = 0; i < N; i++) {
    vel[i*3] -= pos[i*3] * 0.0012; vel[i*3+1] -= pos[i*3+1] * 0.0012; vel[i*3+2] -= pos[i*3+2] * 0.0012;
    // clamp transient velocity: cluster gravity bunches members into dense
    // clumps whose combined repulsion would otherwise slingshot nodes far
    // outside the graph before the 900-tick settle freezes the layout
    const v2 = vel[i*3]*vel[i*3] + vel[i*3+1]*vel[i*3+1] + vel[i*3+2]*vel[i*3+2];
    if (v2 > 2500) {
      const s = 50 / Math.sqrt(v2);
      vel[i*3] *= s; vel[i*3+1] *= s; vel[i*3+2] *= s;
    }
    pos[i*3] += vel[i*3] * alpha; pos[i*3+1] += vel[i*3+1] * alpha; pos[i*3+2] += vel[i*3+2] * alpha;
    vel[i*3] *= 0.86; vel[i*3+1] *= 0.86; vel[i*3+2] *= 0.86;
  }
}
let running = !frozenPos;
window.__dbg = { pos, nodes, links, syncEdgePos, renderer: null, camera: null, THREE, alpha: alphaArr, bucketMat, bucketOf, hwSlot, bucketPosIB, bucketColIB, slotOf };
function tick() {
  if (window.__dbg) { window.__dbg.renderer = renderer; window.__dbg.camera = camera; }
  if (running) {
    frameNo++;
    for (let s = 0; s < 2; s++) step();
    syncEdgePos();
    syncFnPos();
    pGeo.attributes.position.needsUpdate = true;
    if (++settled > 900) { running = false; frameGraph(); }
  }
  controls.update();
  updateHubs();
  updateClusterLabs();
  updateEdgeLabels();
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
  camera.position.copy(b.center).addScaledVector(dir, Math.max(420, b.radius * 1.8));
  controls.target.copy(b.center);
  // LOD threshold tracks the framing distance so overview stays overview
  // regardless of graph size
  lodDist = Math.max(420, b.radius * 1.8) * 0.66;
}

// ---- UI ---------------------------------------------------------------------
const stats = document.getElementById("stats");
const m = DATA.meta;
stats.textContent = `${m.files} files · ${m.edges} links · ${m.clusters} clusters · dead ${m.deadLikely}+${m.deadReview}`;
const edgeLegend = document.getElementById("edgeLegend");
[["call", TYPE_COLORS.call], ["signal", TYPE_COLORS.signal],
 ["contains", TYPE_COLORS.inst], ["var", TYPE_COLORS.var]].forEach(([name, c]) => {
  const k = document.createElement("span");
  k.className = "eKey";
  const sw = document.createElement("i");
  sw.style.borderTopColor = "#" + c.getHexString();
  k.appendChild(sw);
  k.appendChild(document.createTextNode(name));
  edgeLegend.appendChild(k);
});

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
  nodes.forEach((n, i) => {
    if (n.cluster >= 0 && nodeVisible(n)) (byC[n.cluster] = byC[n.cluster] || []).push(i);
  });
  Object.entries(byC).sort((a, b) => b[1].length - a[1].length).slice(0, 14)
    .forEach(([cid, members]) => {
      let cx = 0, cy = 0, cz = 0;
      members.forEach(i => { cx += pos[i*3]; cy += pos[i*3+1]; cz += pos[i*3+2]; });
      cx /= members.length; cy /= members.length; cz /= members.length;
      let r = 60;
      members.forEach(i => {
        r = Math.max(r, Math.hypot(pos[i*3]-cx, pos[i*3+1]-cy, pos[i*3+2]-cz));
      });
      r *= 1.12;
      const segs = 72, pts = [];
      for (let s = 0; s <= segs; s++) {
        const a = s / segs * Math.PI * 2;
        pts.push(new THREE.Vector3(cx + Math.cos(a)*r, cy, cz + Math.sin(a)*r));
      }
      const col = new THREE.Color().setHSL(hue(+cid), 0.72, 0.58);
      const ring = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
        new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.14,
          blending: THREE.AdditiveBlending, depthWrite: false }));
      scene.add(ring); cRings.push(ring);
      const el = document.createElement("div");
      el.className = "clab";
      el.style.color = "#" + col.getHexString();
      el.textContent = (cNames[cid] || "c" + cid) + " · " + members.length;
      clabsEl.appendChild(el);
      cLabs.push({ cx, cy, cz, el });
    });
}
function updateClusterLabs() {
  const w = innerWidth, h = innerHeight;
  // hub pills win collisions; cluster names try rows around the centroid
  const hubRects = [...document.querySelectorAll("#hubs .hub")]
    .filter(el => el.style.display !== "none")
    .map(el => el.getBoundingClientRect());
  const sep = (a, b) =>
    a.right < b.left - 4 || b.right < a.left - 4 ||
    a.bottom < b.top - 4 || b.bottom < a.top - 4;
  const taken = [];
  for (const c of cLabs) {
    if (clabsEl.style.display === "none") { c.el.style.display = "none"; continue; }
    hubV.set(c.cx, c.cy, c.cz).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.05 || Math.abs(hubV.y) > 1.05) {
      c.el.style.display = "none"; continue;
    }
    c.el.style.display = "block";
    const px = (hubV.x*0.5+0.5)*w, py = (-hubV.y*0.5+0.5)*h;
    let placed = false;
    for (const dy of [0, -34, 34, -64, 64]) {
      c.el.style.transform = "translate(" + px.toFixed(1) + "px," + (py+dy).toFixed(1) + "px) translate(-50%,-50%)";
      const r = c.el.getBoundingClientRect();
      if (hubRects.every(hr => sep(r, hr)) && taken.every(t => sep(r, t))) {
        taken.push(r); placed = true; break;
      }
    }
    if (!placed) c.el.style.display = "none";
  }
}

const tip = document.getElementById("tip");
const crumb = document.getElementById("crumb");
const esc = s => String(s).replace(/[&<>"]/g,
  ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" })[ch]);
const raycaster = new THREE.Raycaster();
raycaster.params.Points.threshold = 14;
const mouse = new THREE.Vector2();
let hovered = -1, hoveredFn = -1, selected = -1;
let activeCluster = null, deadOnly = false, query = "";
let showInst = false, showCalls = true, focusSeed = -1, depth = 2, fnMode = false;
// tests/tools hidden by default (chip toggles them in); dir filter row works
// like the cluster chips — both only ever filter, never re-layout
let showTests = false; const activeDirs = new Set();   // multi-select dir filter
const isTestNode = n => n.path.startsWith("tests/") || n.path.startsWith("tools/") ||
  n.path.slice(n.path.lastIndexOf("/") + 1).startsWith("test_");
// overview LOD: intra-cluster edges stay hidden until the camera closes in
// (zoom threshold maintained by the controls 'change' listener below)
let lodClose = false, lodDist = 1e9;
const level = new Int16Array(N).fill(-1);

// BFS from search seeds (path/class matches + clicked seed) up to `depth`
function computeLevels() {
  level.fill(-1);
  const seeds = [];
  if (query) nodes.forEach((n, i) => {
    if (n.path.toLowerCase().includes(query) || n.cls.toLowerCase().includes(query)) seeds.push(i);
  });
  if (focusSeed >= 0 && level[focusSeed] < 0) { level[focusSeed] = 0; seeds.push(focusSeed); }
  for (let qi = 0; qi < seeds.length; qi++) {
    const u = seeds[qi];
    if (level[u] >= depth) continue;
    for (const v of adj[u]) if (level[v] < 0) { level[v] = level[u] + 1; seeds.push(v); }
  }
  return seeds.length > 0;
}

function nodeVisible(n) {
  if (deadOnly && n.dead <= 0) return false;
  if (activeCluster !== null && n.cluster !== activeCluster) return false;
  if (!showTests && isTestNode(n)) return false;
  if (activeDirs.size && !activeDirs.has(n.dir)) return false;
  return true;
}
function typeVisible(ty) {
  if (ty === "call") return showCalls;
  if (ty === "inst" || ty === "attach") return showInst;
  return true;
}
function applyVisibility() {
  const focusing = computeLevels();
  // edges are a quiet layer at overview (per-bucket caps) and open up when
  // a focus set is lit
  bucketMat.forEach((mat, bi) => { mat.opacity = focusing ? 0.75 : BUCKETS[bi].op; });
  for (let i = 0; i < N; i++) {
    let a;
    if (!nodeVisible(nodes[i])) a = 0.0;   // size-0 gate in shader = true disable
    else if (focusing) a = level[i] < 0 ? 0.02 : (level[i] === 0 ? 1 : Math.max(0.16, 0.7 - level[i] * 0.18));
    else a = 1;
    alphaArr[i] = a;
    if (a > 0.5) {
      const c = colorOf(nodes[i]);
      colArr[i*3] = c.r; colArr[i*3+1] = c.g; colArr[i*3+2] = c.b;
    }
  }
  pGeo.attributes.color.needsUpdate = true;
  pGeo.attributes.aalpha.needsUpdate = true;
  // dim edges: hidden endpoints, filtered types, or focus distance
  // (dimmed eColBase written straight into each bucket's instanced colors).
  // Overview palette: edge-type hues are demoted to weight-tinted gray so
  // cluster colors carry the overview; full type colors return on focus.
    const grayMix = focusing ? 0 : 0.92;
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
    if (sFiltered || tFiltered) k = 0.0;
    else if (!typeVisible(l.ty) || alphaArr[l.s] <= 0.5 || alphaArr[l.t] <= 0.5) k = 0.012;
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
      if (sameC) { if (k > 0.045) k = 0.045; }
      else if (el3 > 200 && hwSlot[i] < 0) k = 0.02;
    }
    const b = bucketOf[i], o6 = i * 6;
    const tgt = bucketColIB[b].array;
    if (k === 0) {
      // filtered-out edge (tests/tools hidden, dir filter): pure black.
      // The gray-mix formula below would otherwise leak (1-grayMix)=8% of
      // the base color, visible under additive blending when many killed
      // edges converge on a hub.
      if (hwSlot[i] >= 0) tgt.fill(0, hwSlot[i], hwSlot[i] + 192);
      else tgt.fill(0, slotOf[i] * 6, slotOf[i] * 6 + 6);
      touched[b] = true;
      return;
    }
    if (hwSlot[i] >= 0) {
      // highway: replicate the (possibly grayed/dimmed) color across all
      // 16 segments
      const base = hwSlot[i];
      if (grayMix > 0) {
        const g = (0.10 + 0.18 * Math.min(1, l.w / 8)) * k;
        for (let v = 0; v < 32; v++) {
          const s6 = base + v * 6;
          for (let c = 0; c < 6; c++) tgt[s6+c] = eColBase[o6+c] * (1 - grayMix) + g * grayMix;
        }
      } else {
        for (let v = 0; v < 32; v++) {
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
  if (focusing) {
    let lit = 0;
    for (let i = 0; i < N; i++) if (level[i] >= 0 && nodeVisible(nodes[i])) lit++;
    const label = focusSeed >= 0 ? esc(nodes[focusSeed].label) : "“" + esc(query) + "”";
    crumb.innerHTML = "focus: <b>" + label + "</b> · depth " + depth + " · " + lit +
      " files lit<span class='x' title='clear focus (Esc)'>✕</span>";
    crumb.style.display = "flex";
    crumb.querySelector(".x").onclick = clearFocus;
  } else crumb.style.display = "none";
  // cluster identity is an overview cue — hide the name labels on focus
  clabsEl.style.display = focusing ? "none" : "block";
  rebuildFnLayer(focusing);
  rebuildHubs();
  rebuildEdgeLabels(focusing);
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
    if (alphaArr[l.s] < 0.5 || alphaArr[l.t] < 0.5) { el.style.display = "none"; continue; }
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

// ---- hub labels: top-degree visible files, projected to screen each frame ---
const HUB_N = 20;
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
    el.onclick = () => { showInfo(i); focusSeed = i; applyVisibility(); focus(i); };
    hubsEl.appendChild(el);
    return { i, el };
  });
}
function updateHubs() {
  const w = innerWidth, h = innerHeight;
  // greedy placement against real measured boxes; transforms only touch
  // these few absolutely-positioned nodes, so rect reads stay cheap.
  // vertical rows first (keeps label near its node), then sideways nudges
  const fixed = [];
  const free = (a, b) => a.right < b.left - 4 || b.right < a.left - 4 ||
    a.bottom < b.top - 4 || b.bottom < a.top - 4;
  for (const { i, el } of hubs) {
    if (alphaArr[i] < 0.5) { el.style.display = "none"; continue; }
    hubV.set(pos[i*3], pos[i*3+1], pos[i*3+2]).project(camera);
    if (hubV.z > 1 || Math.abs(hubV.x) > 1.02 || Math.abs(hubV.y) > 1.02) {
      el.style.display = "none"; continue;
    }
    // clamp inside the viewport but clear of the left info panel
    const x = Math.max(310, Math.min(w - 30,
      (hubV.x * 0.5 + 0.5) * w)), y = Math.max(16, Math.min(h - 26, (-hubV.y * 0.5 + 0.5) * h));
    el.style.display = "block";
    let r = null;
    outer:
    for (const dy of [-19, 17, -42, 41, -65, 65, -88, 88]) {
      for (const dx of [0, 100, -100, 200, -200, 320, -320]) {
        el.style.transform = "translate(" + (x + dx).toFixed(1) + "px," +
          (y + dy).toFixed(1) + "px) translate(-50%,0)";
        r = el.getBoundingClientRect();
        if (fixed.every(f => free(r, f))) break outer;
      }
    }
    fixed.push(r);
  }
}
rebuildHubs();

// ---- function-level layer (files inside the current focus) -------------------
let fnPoints = null, fnLines = null, fnSpokes = null, fnMeta = [];
const fnOffset = name => {
  let h = 2166136261;
  for (let c = 0; c < name.length; c++) { h ^= name.charCodeAt(c); h = Math.imul(h, 16777619); }
  const a = (h >>> 0) / 4294967296 * Math.PI * 2;
  const b = ((h >>> 8) % 997) / 997 * Math.PI;
  const r = 34;
  return [Math.cos(a) * Math.sin(b) * r, Math.cos(b) * r * 0.5, Math.sin(a) * Math.sin(b) * r];
};
function rebuildFnLayer(focusing) {
  if (fnPoints) { scene.remove(fnPoints); fnPoints.geometry.dispose(); fnPoints = null; }
  if (fnLines) { scene.remove(fnLines); fnLines.geometry.dispose(); fnLines = null; }
  if (fnSpokes) { scene.remove(fnSpokes); fnSpokes.geometry.dispose(); fnSpokes = null; }
  fnMeta = [];
  if (!fnMode || !focusing) return;
  const fIdx = new Map(), fpos = [], fcol = [], foffs = [], eidx = [];
  const nodeOf = (fi, name) => {
    const k = fi + "::" + name;
    let ix = fIdx.get(k);
    if (ix === undefined) {
      ix = fnMeta.length; fIdx.set(k, ix);
      const off = fnOffset(name);
      fnMeta.push({ file: fi, name, off });
      fpos.push(pos[fi*3] + off[0], pos[fi*3+1] + off[1], pos[fi*3+2] + off[2]);
      fcol.push(colArr[fi*3], colArr[fi*3+1], colArr[fi*3+2]);
      foffs.push(off[0], off[1], off[2]);
    }
    return ix;
  };
  fedges.forEach(e => {
    const sf = e[0], df = e[2];
    if (level[sf] < 0 || level[df] < 0) return;
    // hidden files (tests/tools filter, dir filter): their function nodes
    // and edges must not render in the fn layer either
    if (alphaArr[sf] <= 0.5 || alphaArr[df] <= 0.5) return;
    const a = nodeOf(sf, e[1]), b = nodeOf(df, e[3]);
    eidx.push(a, b);
  });
  if (!fnMeta.length) return;
  const g1 = new THREE.BufferGeometry();
  g1.setAttribute("position", new THREE.BufferAttribute(new Float32Array(fpos), 3));
  g1.setAttribute("color", new THREE.BufferAttribute(new Float32Array(fcol), 3));
  g1.setAttribute("psize", new THREE.BufferAttribute(new Float32Array(fnMeta.length).fill(4.2), 1));
  g1.setAttribute("aalpha", new THREE.BufferAttribute(new Float32Array(fnMeta.length).fill(1), 1));
  fnPoints = new THREE.Points(g1, pMat);
  scene.add(fnPoints);
  // spokes: faint tie from each function satellite to its file node —
  // without them the satellites read as unconnected noise
  const sp = [];
  for (let i = 0; i < fnMeta.length; i++) {
    const fi = fnMeta[i].file * 3, j = i * 3;
    sp.push(pos[fi], pos[fi+1], pos[fi+2], fpos[j], fpos[j+1], fpos[j+2]);
  }
  const g3 = new THREE.BufferGeometry();
  g3.setAttribute("position", new THREE.BufferAttribute(new Float32Array(sp), 3));
  fnSpokes = new THREE.LineSegments(g3, new THREE.LineBasicMaterial({
    color: 0x445566, transparent: true, opacity: 0.22, depthWrite: false }));
  scene.add(fnSpokes);
  const ep = [];
  for (let i = 0; i < eidx.length; i += 2) {
    const a = eidx[i] * 3, b = eidx[i+1] * 3;
    ep.push(fpos[a], fpos[a+1], fpos[a+2], fpos[b], fpos[b+1], fpos[b+2]);
  }
  const g2 = new THREE.BufferGeometry();
  g2.setAttribute("position", new THREE.BufferAttribute(new Float32Array(ep), 3));
  fnLines = new THREE.LineSegments(g2, new THREE.LineBasicMaterial({
    vertexColors: true, transparent: true, opacity: 0.45,
    blending: THREE.AdditiveBlending, depthWrite: false }));
  scene.add(fnLines);
}
function syncFnPos() {
  if (!fnPoints) return;
  const p = fnPoints.geometry.attributes.position.array;
  const s = fnSpokes ? fnSpokes.geometry.attributes.position.array : null;
  for (let i = 0; i < fnMeta.length; i++) {
    const fi = fnMeta[i].file * 3, o = fnMeta[i].off, j = i * 3;
    p[j] = pos[fi] + o[0]; p[j+1] = pos[fi+1] + o[1]; p[j+2] = pos[fi+2] + o[2];
    if (s) { s[j] = pos[fi]; s[j+1] = pos[fi+1]; s[j+2] = pos[fi+2]; s[j+3] = p[j]; s[j+4] = p[j+1]; s[j+5] = p[j+2]; }
  }
  fnPoints.geometry.attributes.position.needsUpdate = true;
  if (s) fnSpokes.geometry.attributes.position.needsUpdate = true;
}

const legend = document.getElementById("legend");
const topClusters = Object.entries(
  nodes.reduce((acc, n) => { if (n.cluster >= 0) acc[n.cluster] = (acc[n.cluster]||0)+1; return acc; }, {})
).sort((a,b) => b[1]-a[1]).slice(0, 14);
topClusters.forEach(([cid, count]) => {
  const c = new THREE.Color().setHSL(hue(+cid), 0.72, 0.58);
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.style.background = `#${c.getHexString()}22`;
  chip.style.color = `#${c.getHexString()}`;
  chip.textContent = `${cNames[cid] || "c" + cid} · ${count}`;
  chip.onclick = () => {
    activeCluster = activeCluster === +cid ? null : +cid;
    document.querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    if (activeCluster !== null) chip.classList.add("on");
    applyVisibility();
  };
  legend.appendChild(chip);
});

// dir filter row: top-8 directories as chips + a tests chip (tests/tools
// files are hidden by default; toggling them in rebuilds containment)
const dirsEl = document.getElementById("dirs");
{
  const byDir = {};
  nodes.forEach(n => { if (!isTestNode(n)) byDir[n.dir] = (byDir[n.dir] || 0) + 1; });
  Object.entries(byDir).sort((a, b) => b[1] - a[1]).slice(0, 8).forEach(([dir, count]) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.color = "#b0bec5";
    chip.textContent = dir + " · " + count;
    chip.onclick = () => {
      // multi-select: chips stack, each toggles its dir independently
      if (activeDirs.has(dir)) { activeDirs.delete(dir); chip.classList.remove("on"); }
      else { activeDirs.add(dir); chip.classList.add("on"); }
      buildContainment(); applyVisibility();
    };
    dirsEl.appendChild(chip);
  });
  const tchip = document.createElement("span");
  tchip.className = "chip";
  tchip.style.color = "#ffb74d";
  tchip.textContent = "tests";
  tchip.onclick = () => {
    showTests = !showTests;
    tchip.classList.toggle("on", showTests);
    activeDirs.clear();
    document.querySelectorAll("#dirs .chip").forEach(x => { if (x !== tchip) x.classList.remove("on"); });
    buildContainment(); applyVisibility();
  };
  dirsEl.appendChild(tchip);
}

document.getElementById("bDead").onclick = e => {
  deadOnly = !deadOnly;
  e.target.classList.toggle("on", deadOnly);
  applyVisibility();
};
document.getElementById("bCalls").onclick = e => {
  showCalls = !showCalls;
  e.target.classList.toggle("on", showCalls);
  applyVisibility();
};
document.getElementById("bInst").onclick = e => {
  showInst = !showInst;
  e.target.classList.toggle("on", showInst);
  applyVisibility();
};
const searchEl = document.getElementById("search");
const depthEl = document.getElementById("depth");
searchEl.oninput = e => { query = e.target.value.toLowerCase(); applyVisibility(); };
depthEl.oninput = e => { depth = +e.target.value; document.getElementById("depthVal").textContent = depth; applyVisibility(); };
document.getElementById("cbFn").onchange = e => { fnMode = e.target.checked; applyVisibility(); };
function clearFocus() {
  focusSeed = -1; query = "";
  document.getElementById("search").value = "";
  applyVisibility();
}
addEventListener("keydown", e => {
  if (e.key === "Escape" && (focusSeed >= 0 || query)) clearFocus();
});
document.getElementById("bSpin").onclick = e => {
  controls.autoRotate = !controls.autoRotate;
  e.currentTarget.classList.toggle("on", controls.autoRotate);
};
// right-click (not a drag) exits node focus
renderer.domElement.addEventListener("contextmenu", e => {
  e.preventDefault();
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (focusSeed >= 0) clearFocus();
});
document.getElementById("bReset").onclick = () => {
  camera.position.set(0, 0, 1400); controls.target.set(0,0,0); frameGraph();
  activeCluster = null; deadOnly = false; query = ""; focusSeed = -1;
  activeDirs.clear(); showTests = false;
  document.getElementById("search").value = "";
  document.querySelectorAll(".chip, button").forEach(x => x.classList.remove("on"));
  document.getElementById("bCalls").classList.add("on");
  showCalls = true;
  buildContainment(); applyVisibility();
};

const info = document.getElementById("info");
function showInfo(i) {
  const n = nodes[i];
  selected = i;
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
  if (n.cluster >= 0) mk("cluster c" + n.cluster, `#${new THREE.Color().setHSL(hue(n.cluster),0.72,0.58).getHexString()}`);
  if (n.dl || n.dead > 0) mk(n.dl ? "likely dead" : "maybe dead", "#ef5350");
  const ul = document.getElementById("iLinks");
  ul.innerHTML = "";
  const nb = new Map();
  links.forEach(l => {
    // list every relationship type regardless of the view toggles;
    // entries whose type is currently toggled off render dimmed
    let j, rel;
    if (l.s === i) { j = l.t; rel = l.ty === "inst" ? "contains" : l.ty === "attach" ? "attaches" : l.ty; }
    else if (l.t === i) { j = l.s; rel = l.ty === "inst" ? "part of" : l.ty === "attach" ? "used by" : l.ty; }
    else return;
    const key = j + "|" + rel;
    const cur = nb.get(key) || { j, rel, w: 0, vis: false };
    cur.w += l.w;
    if (typeVisible(l.ty)) cur.vis = true;
    nb.set(key, cur);
  });
  [...nb.values()].sort((a,b) => b.w-a.w).slice(0, 24).forEach(({j, rel, w, vis}) => {
    const li = document.createElement("li");
    li.textContent = `${nodes[j].label} · ${rel} ×${w}`;
    if (!vis) li.style.opacity = 0.45;
    li.onclick = () => { showInfo(j); focusSeed = j; applyVisibility(); focus(j); };
    ul.appendChild(li);
  });
}
function focus(i) {
  controls.target.set(pos[i*3], pos[i*3+1], pos[i*3+2]);
  const d = 320;
  const dir = new THREE.Vector3(camera.position.x - controls.target.x,
    camera.position.y - controls.target.y, camera.position.z - controls.target.z).normalize();
  camera.position.copy(controls.target).addScaledVector(dir, d);
}

function showFnInfo(k) {
  const fm = fnMeta[k];
  info.style.display = "block";
  document.getElementById("iTitle").textContent = fm.name + "()";
  document.getElementById("iSub").textContent = nodes[fm.file].path;
  const tags = document.getElementById("iTags");
  tags.innerHTML = "";
  const ul = document.getElementById("iLinks");
  ul.innerHTML = "";
  const nb = new Map();
  fedges.forEach(e => {
    if (e[0] === fm.file && e[1] === fm.name) {
      const key = e[2] + "::" + e[3];
      nb.set("→ " + key, nodes[e[2]].label + " :: " + e[3]);
    }
    if (e[2] === fm.file && e[3] === fm.name) {
      const key = e[0] + "::" + e[1];
      nb.set("← " + key, nodes[e[0]].label + " :: " + e[1]);
    }
  });
  [...nb.values()].slice(0, 24).forEach(txt => {
    const li = document.createElement("li");
    li.textContent = txt;
    ul.appendChild(li);
  });
}

renderer.domElement.addEventListener("pointermove", e => {
  mouse.x = (e.clientX/innerWidth)*2-1; mouse.y = -(e.clientY/innerHeight)*2+1;
  raycaster.setFromCamera(mouse, camera);
  const targets = fnPoints ? [scene.children[0], fnPoints] : [scene.children[0]];
  const hits = raycaster.intersectObjects(targets);
  hovered = -1; hoveredFn = -1;
  // skip invisible nodes: filtered-out tests/tools keep raycast geometry,
  // but hovering a ghost must not pop a tooltip (walk to first visible hit)
  for (const h of hits) {
    if (h.object === fnPoints) {
      const fm = fnMeta[h.index];
      if (fm && alphaArr[fm.file] > 0.5) { hoveredFn = h.index; break; }
    } else if (alphaArr[h.index] > 0.5) { hovered = h.index; break; }
  }
  let txt = null;
  if (hoveredFn >= 0) {
    const fm = fnMeta[hoveredFn];
    txt = nodes[fm.file].path + " :: " + fm.name;
  } else if (hovered >= 0) {
    txt = nodes[hovered].path;
    const s = edgeSummary(hovered);
    if (s) txt += "\n" + s;
  }
  if (txt) {
    tip.style.display = "block";
    tip.style.left = (e.clientX+14)+"px"; tip.style.top = (e.clientY+14)+"px";
    tip.textContent = txt;
    renderer.domElement.style.cursor = "pointer";
  } else { tip.style.display = "none"; renderer.domElement.style.cursor = "default"; }
});
// drag-vs-click: OrbitControls uses pointer drags; a release over a node
// after rotating the camera must not select it
let downX = 0, downY = 0;
renderer.domElement.addEventListener("pointerdown", e => { downX = e.clientX; downY = e.clientY; });
renderer.domElement.addEventListener("click", e => {
  if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return;
  if (hoveredFn >= 0) { showFnInfo(hoveredFn); return; }
  if (hovered >= 0) {
    showInfo(hovered);
    focusSeed = hovered;           // clicked file becomes the focus root
    applyVisibility();
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
    if (focusSeed < 0 && !query) applyVisibility();
  }
});

// apply the overview palette + LOD once at boot (initial buffer fill is
// full-color; this demotes it to the overview state without waiting for
// user interaction)
buildContainment();
applyVisibility();
if (!running) frameGraph();
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
