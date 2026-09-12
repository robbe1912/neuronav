# bake/overlays — pure per-job transforms for viz._build_data (issue #86
# phase-2 V8). Moved verbatim from viz.py; every nav/graph/chroma edge
# stays in the viz.py orchestrator — data arrives as arguments.

import json

from bake.budget import _cap_rows

def _highways(pos_baked, nodes, links):
    """J13: long inter-cluster links as bundled bezier arc polylines."""
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
    return hw


def _cap_highways(hw, links):
    """J14: hw arc byte budget — heaviest links first, ties by index."""
    # hw arc budget (spec §4 row 10): bezier control polylines are ~400 B
    # each and scale with long inter-cluster links. Below the cap nothing
    # changes; above it arcs of the heaviest links survive first (ties by
    # link index), re-emitted in ascending link order like the uncapped
    # path.
    hw_dropped = 0
    _HW_BYTE_CAP = 1_500_000
    if len(json.dumps(hw, separators=(",", ":"))) > _HW_BYTE_CAP:
        kept_hw, _ = _cap_rows(
            range(len(hw)),
            prio_key=lambda i: (-links[hw[i][0]]["w"], hw[i][0]),
            cost_of=lambda i: len(json.dumps(hw[i], separators=(",", ":"))) + 1,
            cap=_HW_BYTE_CAP,
        )
        hw_dropped = len(hw) - len(kept_hw)
        hw = [hw[i] for i in sorted(kept_hw)]
    return hw, hw_dropped


def _crosstalk_top(links, nodes):
    """J17: top inter-cluster corridors (count desc, then cid asc)."""
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
    return crosstalk
