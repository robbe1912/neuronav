"""neuronav viz layout: pure strata/layout math for the bake (issue #86).

The five functions moved VERBATIM from viz.py (phase-2 V1): directed
adjacency + iterative Tarjan SCC, condensation depth layering, and the
deterministic offline force layout that bakes node positions into DATA.
Module deps stay os/sys only (numpy is imported inside _layout) --
tests/test_strata.py imports this module directly.
"""

import os
import sys

# Row-block size for the chunked pairwise relax passes in _layout
# (issue #294): every N x N (and N x N x 3) dense temporary below the
# 2048-node force cut became a block-striped loop, so the relax working
# set is O(_RELAX_BLOCK x N) instead of O(N^2) — measured process peak
# at 6k nodes: 1833 MB -> 452 MB (the remainder is the pre-sim kcoef
# build, which the grid path never reads). The striping is EXACT:
# blocks only change allocation granularity, never elementwise
# arithmetic or the argwhere-then-move pair order, so positions stay
# byte-identical to the dense form at any block size (pinned by
# tests/test_strata.py §8 and the 6k corpus phase bisect).
_RELAX_BLOCK = 512


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
              ckeys=None, cmat=None, depths=None, hot=None):
    """Deterministic offline force layout. Returns (positions, rest
    radii): both freeze into DATA (pos / restR, #368) — the radii are
    the rendered-radius law the relax enforced, served so the browser
    draws them verbatim instead of restating the constants.

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
    # integration steps, so repulsion needs more authority to open the graph.
    # Dense-path-only materialization (issue #309): the N×N pairwise matrix
    # is read solely by the dense repulsion branch below (n <= 2048); the
    # grid-binned sparse path recomputes the coefficient scalarly per pair
    # (KC * ds[i] * ds[j] * hh[ci, cj]), so above the scale cut the matrix
    # is ~137 MB of dead weight at 6k nodes and is not built at all
    if n <= 2048:
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
    if n <= 2048:
        kcoef = (kcoef * hh[np.ix_(cinv, cinv)]).astype(np.float32)

    alpha = 1.0
    # scale path runs 300 steps (spec §4 row 6 fallback): the sim is
    # near-equilibrium by step ~150 and 700 kNN steps would still cost
    # minutes at 6k nodes; the dense path keeps its QA'd 700 verbatim
    for step in range(700 if n <= 2048 else 300):
        alpha *= 0.997
        # pairwise repulsion. Dense N^2 below the scale cut — byte-stable on
        # every existing corpus (self-index, game-target all sit under it). Above it
        # (#13 spec §4 row 6): the dense product is ~1.2 GB of temporaries
        # per step at 6k nodes and turns a bake into hours, so repulsion is
        # grid-binned and each node feels only its 32 nearest neighbours —
        # terms beyond the 2.5e6 cutoff were already force-zero, and the cap
        # only ever engages at engine scale. Same seeded rng, same force law,
        # deterministic pair selection (stable sorts, no hash iteration).
        if n <= 2048:
            diff = pos[None, :, :] - pos[:, None, :]    # D[i,j] = pos[j]-pos[i]
            d2 = (diff * diff).sum(-1)
            d2 += 1.0
            m = np.minimum(kcoef / (d2 * d2), 400.0 / d2, dtype=np.float32)
            m[d2 > 2.5e6] = 0.0
            np.fill_diagonal(m, 0.0)
            vel -= np.einsum("ij,ijk->ik", m, diff, dtype=np.float32)
        else:
            off = np.array([[i, j, k] for i in (-1, 0, 1)
                            for j in (-1, 0, 1) for k in (-1, 0, 1)],
                           dtype=np.int64)
            lo = pos.min(0)
            span = np.maximum(pos.max(0) - lo, 1.0)
            # cell size targeting ~40 occupants over the 27-cell
            # neighbourhood: just above the 32-NN cut so the selection
            # rarely discards candidates
            cell = max(60.0, min(800.0,
                        (40.0 * float(span.prod()) / (27.0 * n)) ** (1.0 / 3.0)))
            gi = ((pos - lo) / cell).astype(np.int64)
            dims = gi.max(0) + 3
            d0 = int(dims[0])
            d1 = int(dims[1])
            gkey = gi[:, 0] + gi[:, 1] * d0 + gi[:, 2] * (d0 * d1)
            order = np.argsort(gkey, kind="stable")
            skey = gkey[order]
            KC = np.float32(110000.0 / (dbar * dbar))
            for c0 in range(0, n, 512):
                chunk = np.arange(c0, min(c0 + 512, n))
                gx = gi[chunk, 0][:, None] + off[:, 0]
                gy = gi[chunk, 1][:, None] + off[:, 1]
                gz = gi[chunk, 2][:, None] + off[:, 2]
                ok = ((gx >= 0) & (gx < d0) & (gy >= 0) & (gy < int(dims[1]))
                      & (gz >= 0) & (gz < int(dims[2])))
                nk = np.where(ok, gx + gy * d0 + gz * (d0 * d1), -1)
                st = np.searchsorted(skey, nk, "left")
                en = np.searchsorted(skey, nk, "right")
                cntm = np.where(ok, en - st, 0)
                cnt = cntm.sum(1)
                tot = int(cnt.sum())
                if not tot:
                    continue
                owner = np.repeat(chunk, cnt)
                starts = np.concatenate(([0], np.cumsum(cntm.ravel())[:-1]))
                within = np.arange(tot) - np.repeat(starts, cntm.ravel())
                cand = order[np.repeat(st.ravel(), cntm.ravel()) + within]
                diff = pos[cand] - pos[owner]
                pd2 = (diff * diff).sum(1)
                pd2 += 1.0
                pd2[cand == owner] = np.float32(1e18)
                sel = np.lexsort((cand, pd2, owner))
                owner, cand = owner[sel], cand[sel]
                diff, pd2 = diff[sel], pd2[sel]
                grp_start = np.concatenate(([True], owner[1:] != owner[:-1]))
                gs = np.maximum.accumulate(np.where(grp_start,
                                    np.arange(tot), 0))
                keep = (np.arange(tot) - gs) < 32
                owner, cand, diff, pd2 = owner[keep], cand[keep], diff[keep], pd2[keep]
                kc = KC * ds[owner] * ds[cand] * hh[cinv[owner], cinv[cand]]
                m = np.minimum(kc / (pd2 * pd2), np.float32(400.0) / pd2)
                m[pd2 > 2.5e6] = 0.0
                f = m[:, None] * diff
                for ax in range(3):
                    vel[:, ax] -= np.bincount(owner, weights=f[:, ax], minlength=n)
                    vel[:, ax] += np.bincount(cand, weights=f[:, ax], minlength=n)
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
    # overlapping pair apart along its axis (min center distance = 2.0x
    # the rendered radii sum) until clean, then spread + re-center.
    deg = np.zeros(n, dtype=np.float32)
    for l in links:
        s_, t_ = (l["s"], l["t"]) if isinstance(l, dict) else (l[0], l[1])
        w_ = (l.get("w", 1) if isinstance(l, dict) else (l[2] if len(l) > 2 else 1))
        deg[s_] += w_
        deg[t_] += w_
    # THE rendered-radius law (#368): min(12, 4.5+sqrt(deg)) * churn boost
    # * 1.1 — the ONE definition. The relax must use it or hot files (up
    # to +35% radius) overlap neighbors, and the browser draws it as
    # served: rad rides DATA.restR and vizjs/core.py copies it into
    # `sizes` verbatim (dynamic multipliers — spread, satellites — stay
    # renderer-side). Parity no longer rests on a mirrored constant.
    churn = (np.asarray(hot, dtype=np.float32) if hot is not None
             else np.zeros(n, dtype=np.float32))
    rad = (np.minimum(12.0, 4.5 + np.sqrt(deg) * 1.0)
           * (1.0 + 0.35 * churn) * 1.1).astype(np.float32)
    # chunked pairwise relax (issue #294): min_d, the depenetrate d0,
    # the exact-scale dist and the strata dy/req/dxz0 used to be dense
    # N x N (x2/x3) temporaries — the ~1.8 GB peak at 6k nodes landed
    # exactly on the engine-scale repos the grid-binned force path
    # serves. Row blocks (see _RELAX_BLOCK) hold the working set at
    # O(BLOCK x N) while every elementwise op and the deterministic
    # argwhere-then-move pair sequence stay identical to the dense form:
    # byte-stable positions, only allocation is chunked.
    def _mind_block(a0: int, a1: int):
        return (rad[a0:a1, None] + rad[None, :]) * 2.0

    def _req_block(a0: int, a1: int):
        # required XZ distance per pair: the 3D min distance minus the
        # frozen Y gap (dy >= min_d pairs need nothing). req is invariant
        # across the sweeps (Y frozen, rad constant), so recomputing rows
        # per block reproduces the precomputed matrix element-for-element.
        dy = pos[a0:a1, None, 1] - pos[None, :, 1]
        md = _mind_block(a0, a1)
        return np.sqrt(np.maximum(md * md - dy * dy, 0.0)).astype(np.float32)

    # dilation fallback: pure pair-pushing oscillates in dense cores (a
    # correction that fixes one pair re-violates its neighbors). If a burst
    # of iterations doesn't converge, inflate the layout slightly and retry
    # — geometric relaxation plus dilation always terminates.
    dbg = os.environ.get("NEURONAV_DEBUG_RELAX")
    def depenetrate() -> int:
        """Separate near-concentric pairs deterministically (see comment)."""
        fused: list = []
        for a0 in range(0, n, _RELAX_BLOCK):
            a1 = min(a0 + _RELAX_BLOCK, n)
            d0 = pos[a0:a1, None, :] - pos[None, :, :]
            dd0 = np.sqrt((d0 * d0).sum(-1))
            dd0[np.arange(a1 - a0), np.arange(a0, a1)] = np.inf
            loc = np.argwhere(dd0 < 5.0)
            loc[:, 0] += a0  # argwhere rows are block-local; make global
            fused.extend(loc)
        moved = 0
        for a, b in fused:
            if a >= b:
                continue
            h = (int(a) * 2654435761 + int(b) * 40503) % 9973
            ang = h / 9973.0 * 6.2831853
            axis = np.array([np.cos(ang), 0.35 * np.sin(ang * 1.7), np.sin(ang)], dtype=np.float32)
            axis /= max(float(np.sqrt((axis * axis).sum())), 1e-3)
            sep = float((rad[a] + rad[b]) * 2.0) * 1.2
            mid = (pos[a] + pos[b]) * 0.5
            pos[a] = mid - axis * (sep * 0.5)
            pos[b] = mid + axis * (sep * 0.5)
            moved += 1
        return moved
    # the force sim can leave two files at nearly identical positions (same
    # gravity basin), where every push direction cancels and they stay
    # concentric forever - which would poison the exact scale pass below
    # (s = min_d/dist explodes on a dist~0 pair). Depenetrate up front only:
    # the separation moves pairs to >= 2.0x the radii sum, and nothing moves
    # positions again until the scale pass, so no new fusions can form
    # (a second call would always return 0).
    n0 = depenetrate()
    if dbg and n0:
        print(f"[depen] fixed {n0} fused pairs", file=sys.stderr)
    s = 0.0
    for a0 in range(0, n, _RELAX_BLOCK):
        a1 = min(a0 + _RELAX_BLOCK, n)
        dist = np.sqrt(((pos[a0:a1, None, :] - pos[None, :, :]) ** 2).sum(-1))
        dist[np.arange(a1 - a0), np.arange(a0, a1)] = np.inf
        s = max(s, float((_mind_block(a0, a1) / dist).max()))
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
        # XZ twin of the 3D relax, Y (strata axis) frozen; same exact-scale
        # finisher: dxz scales linearly, s = max(req/dxz) clears all pairs
        # while the frozen Y gaps keep their contribution to the 3D distance
        # strata Y reassignment stacks same-depth nodes into one layer:
        # pairs the OLD Y kept apart vertically can now sit at dxz~0 with
        # dy~0 (jitter) - depenetrate them in XZ (same trick as the 3D
        # pass) before the exact XZ scale, or s explodes on a dxz~0 pair
        for sweep in range(30):
            fused: list = []
            for a0 in range(0, n, _RELAX_BLOCK):
                a1 = min(a0 + _RELAX_BLOCK, n)
                dxz0 = pos[a0:a1, None, [0, 2]] - pos[None, :, [0, 2]]
                dd0 = np.sqrt((dxz0 * dxz0).sum(-1))
                dd0[np.arange(a1 - a0), np.arange(a0, a1)] = np.inf
                req = _req_block(a0, a1)
                loc = np.argwhere((dd0 < 8.0) & (req > 0.0))
                loc[:, 0] += a0  # argwhere rows are block-local; make global
                fused.extend(loc)
            if not fused:
                break
            for a, b in fused:
                if a >= b:
                    continue
                h = (int(a) * 2654435761 + int(b) * 40503) % 9973
                ang = h / 9973.0 * 6.2831853
                axis = np.array([np.cos(ang), np.sin(ang)], dtype=np.float32)
                axis /= max(float(np.sqrt((axis * axis).sum())), 1e-3)
                dyab = pos[a, 1] - pos[b, 1]
                mdab = (rad[a] + rad[b]) * 2.0
                sep = float(np.sqrt(np.maximum(
                    mdab * mdab - dyab * dyab, np.float32(0.0)))) * 1.2
                mid = (pos[a, [0, 2]] + pos[b, [0, 2]]) * 0.5
                pos[a, [0, 2]] = mid - axis * (sep * 0.5)
                pos[b, [0, 2]] = mid + axis * (sep * 0.5)
        # exact XZ scale: dxz scales linearly, s = max(req/dxz) clears all
        # pairs. Y scales by the SAME factor (around 0): anisotropic
        # XZ-only inflation turned the galaxy into a pancake - uniform
        # scaling keeps the sphere-to-scene ratio the eye was calibrated on.
        s = 0.0
        for a0 in range(0, n, _RELAX_BLOCK):
            a1 = min(a0 + _RELAX_BLOCK, n)
            dxz = pos[a0:a1, None, [0, 2]] - pos[None, :, [0, 2]]
            dxzd = np.sqrt((dxz * dxz).sum(-1))
            dxzd[np.arange(a1 - a0), np.arange(a0, a1)] = np.inf
            s = max(s, float((_req_block(a0, a1) / dxzd).max()))
        if s > 1.0:
            if dbg:
                print(f"[relaxXZ] exact scale pass: s={s:.3f}", file=sys.stderr)
            pos *= s * 1.01
        else:
            pos *= 1.01
    pos *= 1.45   # extra global breathing room — the frame adapts
    pos -= pos.mean(0)
    # rest radii ride DATA.restR (#368): rounded to 4 decimals — the same
    # float32 values the relax used (sub-1e-4wu noise, far below any
    # visual or harness tolerance), kept compact + byte-stable in JSON.
    return ([[round(float(x), 1) for x in p] for p in pos],
            [round(float(x), 4) for x in rad])
