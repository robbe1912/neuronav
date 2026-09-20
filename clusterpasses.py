"""Finalize pass pipeline for navstore.clusters() — split + label passes
carved verbatim out of clusters.py (issue #380; the engine / labeler /
crosstalk seams stayed there and clusters re-exports the pinned names).

Imported lazily via clusters.py, so module import stays cheap: top-level
deps are stdlib + extractors only; numpy/sklearn stay behind function
scope exactly as before the carve. The labeler seam (label_cluster /
LabelContext / _doc_tokens / _dedupe_label / _autoload_map) and the
path-seed helpers stayed in clusters.py — the three passes that need
them import them at call time, keeping this leaf free of clusters
imports at module scope.

Blob split (Qwen3-0.6B over-merge artifact, e.g. a 238-file mega-cluster):
  dir shortcut (>=60% one dir seg -> group by subdirectory) else
  AgglomerativeClustering(cosine, average, sim>=0.65); subclusters <3
  members merge into the nearest centroid (cosine>=0.55) or a Misc bucket;
  subclusters >60 members recurse once at sim 0.70 (max depth 2).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from extractors import (  # noqa: E402
    is_scene_path,
    is_script_path,
)

BLOB_MIN = 60          # clusters larger than this get split
PACK_SPLIT_SHARE = 0.25  # each of the top-2 stem packs must cover >= this
                         # for the surgical asset-pack split to fire
SMALL_MIN = 3          # subclusters smaller than this merge / go Misc
MERGE_SIM = 0.55       # centroid cosine needed to merge a small subcluster
SPLIT_SIM = 0.65       # default agglomerative similarity floor (dist 0.35)
RECURSE_SIM = 0.70     # second-pass floor for still-big subclusters (0.30)
MAX_DEPTH = 2
PART_SOFT_CAP = 66     # routing passes stop adding to a part at this size
PART_CAP = 70          # hard mega-blob cap the final enforce pass guarantees
CHUNK_MIN = 55         # deterministic chunk fill target when a part refuses every cut


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0]


def _vec(rows: dict[str, int], mat, path: str):
    i = rows.get(path)
    return None if i is None else mat[i]


def _centroid_path(paths: list[tuple[str, str]], rows: dict[str, int], mat) -> str:
    import numpy as np

    vecs = [mat[rows[p]] for p, _ in paths if p in rows]
    if not vecs:
        return paths[0][0]
    mean = np.mean(vecs, axis=0)
    mean /= max(float(np.linalg.norm(mean)), 1e-9)
    best, best_sim = paths[0][0], -2.0
    for p, _ in paths:
        v = _vec(rows, mat, p)
        if v is None:
            continue
        sim = float(np.dot(v, mean))
        if sim > best_sim or (sim == best_sim and p < best):
            best, best_sim = p, sim
    return best


def _merge_small(subs: list[dict], rows: dict[str, int], mat) -> list[dict]:
    import numpy as np

    big = [s for s in subs if s["size"] >= SMALL_MIN]
    small = sorted(
        (s for s in subs if s["size"] < SMALL_MIN),
        key=lambda s: s["paths"][0][0],
    )

    def centroid(s: dict):
        vecs = [mat[rows[p]] for p, _ in s["paths"] if p in rows]
        return None if not vecs else np.mean(vecs, axis=0)

    misc: list[tuple[str, str]] = []
    for s in small:
        c = centroid(s)
        best, best_sim = None, -2.0
        for b in big:
            bc = centroid(b)
            if bc is None or c is None:
                continue
            cos = float(np.dot(c, bc) / (np.linalg.norm(c) * np.linalg.norm(bc) + 1e-12))
            if cos > best_sim:
                best, best_sim = b, cos
        if best is not None and best_sim >= MERGE_SIM:
            best["paths"] = sorted(best["paths"] + s["paths"])
            best["size"] = len(best["paths"])
        else:
            misc.extend(s["paths"])
    if misc:
        big.append({"paths": sorted(misc), "size": len(misc)})
    return big


def _pack_key(stem: str) -> str | None:
    """Stem pack id Pfx_Seg (VFX_WindBlow_B -> "vfx_wind"): prefix token +
    first camel hump of the second token. None when the stem has no
    Pfx_Seg shape."""
    toks = stem.split("_")
    if len(toks) < 2 or not toks[1]:
        return None
    humps = re.findall(r"[A-Z][a-z]*", toks[1])
    seg = (humps[0] if humps else toks[1]).lower()
    if len(seg) < 3:
        return None
    return f"{toks[0].lower()}_{seg}"


def _split_pack_cluster(cluster: dict, rows: dict[str, int], mat) -> list[dict]:
    """Surgical asset-pack split (lead-approved): subdivide ONLY
    scene-dominant clusters whose member stems map to >=2 distinct Pfx_Seg
    packs each holding >= PACK_SPLIT_SHARE of the cluster (a two-element
    mixed community in a flat asset folder). Global resolution is
    untouched. Named pack groups smaller than SMALL_MIN merge into the
    nearest surviving pack centroid (>= MERGE_SIM) else join the no-pack
    leftover bucket."""
    import numpy as np

    paths = cluster["paths"]
    n = len(paths)
    scene = [p for p, _ in paths if is_scene_path(p)]
    if len(scene) * 2 <= n or n < 2 * SMALL_MIN:
        return [cluster]
    packs: Counter = Counter()
    for p, _ in paths:
        k = _pack_key(_stem(p))
        if k:
            packs[k] += 1
    top2 = sorted(packs.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    if (
        len(top2) < 2
        or top2[0][1] / n < PACK_SPLIT_SHARE
        or top2[1][1] / n < PACK_SPLIT_SHARE
    ):
        return [cluster]
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for m in paths:
        k = _pack_key(_stem(m[0]))
        groups[k or "_"].append(m)

    def centroid(mem: list[tuple[str, str]]):
        vecs = [mat[rows[p]] for p, _ in mem if p in rows]
        return None if not vecs else np.mean(vecs, axis=0)

    named = {k: mem for k, mem in groups.items() if k != "_"}
    leftovers = list(groups.get("_", []))
    for k in sorted(named):
        mem = named[k]
        if len(mem) >= SMALL_MIN:
            continue
        best, best_sim = None, -2.0
        c = centroid(mem)
        for k2 in sorted(named):
            if k2 == k or not named[k2]:
                continue
            c2 = centroid(named[k2])
            if c is None or c2 is None:
                continue
            cos = float(np.dot(c, c2) / (np.linalg.norm(c) * np.linalg.norm(c2) + 1e-12))
            if cos > best_sim:
                best, best_sim = k2, cos
        if best is not None and best_sim >= MERGE_SIM:
            named[best] = sorted(named[best] + mem)
            named[k] = []
    out = [
        {"paths": sorted(mem), "size": len(mem)}
        for k, mem in sorted(named.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if mem
    ]
    if leftovers:
        out.append({"paths": sorted(leftovers), "size": len(leftovers)})
    out.sort(key=lambda c: (-c["size"], c["paths"][0][0]))
    return out


def _family_unit(path: str, k: str | None, unit_of: dict) -> list[str]:
    """Pack-routing move set: the scene itself, its non-scene unit members
    (scripts), and only SAME-pack scenes. VFX scenes often weld into one
    unit via a shared script — moving the whole unit would drag other
    packs' scenes back and forth between sweeps."""
    return [
        q
        for q in unit_of.get(path, [path])
        if q == path or not is_scene_path(q) or (_pack_key(_stem(q)) or "") == (k or "")
    ]


def _split_units(
    cluster: dict, rows: dict, mat, unit_of: dict, sim: float, depth: int
) -> list[dict]:
    """Unit-aware blob split: welded scene+script units are NEVER divided —
    clustering runs on unit centroids (mean of member embeddings)."""
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    paths = [p for p, _ in cluster["paths"]]
    cls_of = {p: c for p, c in cluster["paths"]}
    groups: list[list[str]] = []
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        u = [q for q in unit_of.get(p, [p]) if q in rows]
        groups.append(sorted(u))
        seen.update(u)
    if len(groups) < 2:
        return [cluster]

    def _dir_of(grp: list[str]) -> str:
        gd = [q for q in grp if is_script_path(q)]
        head = (gd or grp)[0]
        d = head.rsplit("/", 1)[0] if "/" in head else ""
        return d

    dirs = Counter(_dir_of(g) for g in groups)
    top_dir, top_n = sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if top_n / len(groups) >= 0.6:
        by_dir: dict[str, list[str]] = defaultdict(list)
        for g in groups:
            by_dir[_dir_of(g)].extend(g)
        if len(by_dir) >= 2:
            return [
                {"paths": sorted((q, cls_of.get(q, "")) for q in files), "size": len(files)}
                for d, files in sorted(by_dir.items())
                if files
            ]

    cents = []
    for g in groups:
        vecs = [mat[rows[q]] for q in g if q in rows]
        v = np.mean(vecs, axis=0) if vecs else np.zeros(mat.shape[1])
        nrm = float(np.linalg.norm(v))
        cents.append(v / nrm if nrm > 1e-9 else v)
    cl = AgglomerativeClustering(
        n_clusters=None, distance_threshold=1 - sim, metric="cosine", linkage="average"
    ).fit(np.array(cents))
    out: dict[int, list[str]] = defaultdict(list)
    for g, lab in zip(groups, cl.labels_):
        out[int(lab)].extend(g)
    parts = [
        {"paths": sorted((q, cls_of.get(q, "")) for q in files), "size": len(files)}
        for _, files in sorted(out.items())
    ]
    # still-mega parts recurse once at a stricter bar (depth-capped)
    if depth < MAX_DEPTH:
        parts = [
            q
            for p in parts
            for q in (
                _split_units(p, rows, mat, unit_of, RECURSE_SIM, depth + 1)
                if p["size"] > BLOB_MIN
                else [p]
            )
        ]
    return parts


def _pass_split_units(
    raw: list[dict], rows: dict[str, int], mat, unit_of: dict[str, list[str]],
    split_sim: float, blob_min: int,
) -> list[dict]:
    parts: list[dict] = []
    for c in raw:
        if c["size"] > blob_min:
            parts.extend(_split_units(c, rows, mat, unit_of, split_sim, depth=1))
        else:
            parts.append({"paths": list(c["paths"]), "size": c["size"]})
    return parts


def _pass_pack_split(parts: list[dict], rows: dict[str, int], mat) -> list[dict]:
    # surgical asset-pack split for genuinely mixed pack communities
    return [p for c in parts for p in _split_pack_cluster(c, rows, mat)]


def _pass_merge_small(parts: list[dict], rows: dict[str, int], mat) -> list[dict]:
    return _merge_small(parts, rows, mat)


def _pass_pack_consolidate(
    parts: list[dict], unit_of: dict[str, list[str]], units: list[list[str]] | None
) -> tuple[list[dict], dict[str, list[str]]]:
    """Pack consolidation pass. Returns (parts, unit_of): when welded units
    exist, unit_of is rebound to the engine's original unit lists (content
    identical to the copies built in finalize) for the passes downstream."""
    # pack consolidation: the split/merge passes above can leave pack
    # scenes strayed into other parts (audit: five fire scenes in earth,
    # earth files in the world blob). Each pack joins the part holding
    # its plurality (>=3 files, >=50% of the pack) whenever the strayed
    # part is not itself pack-dominated (<20% pack share); scenes move
    # with their welded units.
    if units:
        unit_of = {p: u for u in units for p in u}

        def _part_share(part: dict, pk: str) -> tuple[int, int]:
            tot = share = 0
            for path, _cls in part["paths"]:
                if is_scene_path(path) and _pack_key(_stem(path)) is not None:
                    tot += 1
                    if _pack_key(_stem(path)) == pk:
                        share += 1
            return share, tot

        for _round in range(2):
            part_of2: dict[str, int] = {}
            for pi, p in enumerate(parts):
                for path, _cls in p["paths"]:
                    part_of2[path] = pi
            pack_parts: dict[str, Counter] = {}
            pack_total: Counter = Counter()
            for pi, p in enumerate(parts):
                for path, _cls in p["paths"]:
                    k = _pack_key(_stem(path)) if is_scene_path(path) else None
                    if k:
                        pack_parts.setdefault(k, Counter())[pi] += 1
                        pack_total[k] += 1
            moved = False
            for k in sorted(pack_total):
                if pack_total[k] < 3:
                    continue
                cnt = pack_parts[k]
                home, hn = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                if hn * 5 < pack_total[k] * 2:  # home must hold >=40% of pack
                    continue
                if len(parts[home]["paths"]) > PART_SOFT_CAP:
                    # plurality part oversized: next-best eligible part
                    home = next(
                        (
                            pi
                            for pi, _n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
                            if pi != home and len(parts[pi]["paths"]) <= PART_SOFT_CAP
                        ),
                        -1,
                    )
                    if home < 0:
                        continue
                for pi in sorted(cnt):
                    if pi == home:
                        continue
                    share, tot = _part_share(parts[pi], k)
                    if tot and share * 5 >= tot:
                        continue  # strayed part is itself pack-dominated
                    budget = PART_CAP - len(parts[home]["paths"])
                    if budget <= 0:
                        continue
                    moved_n = 0
                    for path, _cls in list(parts[pi]["paths"]):
                        if moved_n >= budget:
                            break
                        if is_scene_path(path) and _pack_key(_stem(path)) == k:
                            u = _family_unit(path, k, unit_of)
                            for up in u:
                                for pj in list(range(len(parts))):
                                    if pj == home:
                                        continue
                                    ent = next(
                                        (e for e in parts[pj]["paths"] if e[0] == up), None
                                    )
                                    if ent is not None:
                                        parts[pj]["paths"].remove(ent)
                                        parts[home]["paths"].append(ent)
                                        moved = True
                            moved_n += len(u)
            if not moved:
                break
            for p in parts:
                p["size"] = len(p["paths"])
            parts = [p for p in parts if p["size"] > 0]
    return parts, unit_of


def _pass_usage(
    parts: list[dict], adj: dict[str, dict[str, float]] | None, unit_of: dict[str, list[str]]
) -> list[dict]:
    # post-split usage pass: the blob split reassigns whole units, so a
    # pure-script file whose every structural tie (either direction,
    # weighted) now lives in exactly one other part follows it there
    # (audit: camera_input_utils stranded with math utils while its only
    # callers sit in the Magic part)
    from clusters import dir_segments
    if adj:
        rev: dict[str, dict[str, float]] = defaultdict(dict)
        for s, d in adj.items():
            for t2, w2 in d.items():
                rev[t2][s] = w2
        for _round in range(2):
            part_of3: dict[str, int] = {}
            for pi, p in enumerate(parts):
                for path, _cls in p["paths"]:
                    part_of3[path] = pi
            moves = []
            for pi, p in enumerate(parts):
                for path, _cls in p["paths"]:
                    if not is_script_path(path) or path in unit_of:
                        continue
                    if dir_segments(path):
                        continue  # real dir identity handled by overlays
                    w: Counter = Counter()
                    for nb, wt in adj.get(path, {}).items():
                        pj = part_of3.get(nb)
                        if pj is not None:
                            w[pj] += wt
                    for nb, wt in rev.get(path, {}).items():
                        pj = part_of3.get(nb)
                        if pj is not None:
                            w[pj] += wt
                    w.pop(pi, None)
                    if not w:
                        continue
                    (t, tw) = sorted(w.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                    if tw >= 1.0 and len(w) == 1 and len(parts[t]["paths"]) <= PART_SOFT_CAP:
                        moves.append((path, pi, t))
            if not moves:
                break
            for path, pi, t in moves:
                ent = next(e for e in parts[pi]["paths"] if e[0] == path)
                parts[pi]["paths"].remove(ent)
                parts[t]["paths"].append(ent)
            for p in parts:
                p["size"] = len(p["paths"])
            parts = [p for p in parts if p["size"] > 0]
    return parts


def _pass_scene_majority(
    parts: list[dict], adj: dict[str, dict[str, float]] | None, unit_of: dict[str, list[str]]
) -> list[dict]:
    # scene structural majority: a scene whose weighted structural ties
    # (both directions, whole welded unit) overwhelmingly point into ONE
    # other part belongs there, embedding similarity notwithstanding
    # (field report: a display scene sat in the display-name blob while
    # every instancing wire crossed its element cluster). Requires >=60% of
    # edge weight into the target and >=10 total, and the scene must not
    # already hold more of its own ties (wired hubs stay home).
    # NOTE: iterate ALL .tscn paths — pure composition scenes have no
    # attached script and therefore no welded unit.
    if adj:
        rev: dict[str, dict[str, float]] = defaultdict(dict)
        for s, d in adj.items():
            for t2, w2 in d.items():
                rev[t2][s] = w2
        for _round in range(2):
            part_of4: dict[str, int] = {}
            for pi, p in enumerate(parts):
                for path, _cls in p["paths"]:
                    part_of4[path] = pi
            moves2 = []
            for path in sorted(part_of4):
                if not is_scene_path(path):
                    continue
                u = unit_of.get(path) or [path]
                # anchor on the unit's first SCENE member, not u[0]:
                # sorted unit lists put .gd before .tscn, so u[0] skipped
                # every canonical foo.gd+foo.tscn weld — the pass never
                # ran for exactly the pairs it was written for (issue #294)
                if min(p for p in u if is_scene_path(p)) != path:
                    continue  # process each welded unit once, via its anchor
                pi = part_of4[path]
                w2: Counter = Counter()
                for m in u:
                    for nb, wt in adj.get(m, {}).items():
                        pj = part_of4.get(nb)
                        if pj is not None:
                            w2[pj] += wt
                    for nb, wt in rev.get(m, {}).items():
                        pj = part_of4.get(nb)
                        if pj is not None:
                            w2[pj] += wt
                own = w2.pop(pi, None) or 0.0
                if not w2:
                    continue
                tot = own + sum(w2.values())
                if tot < 10.0:
                    continue
                (t, tw) = sorted(w2.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                if tw * 10 >= tot * 6 and own < tw and len(parts[t]["paths"]) <= PART_SOFT_CAP:
                    moves2.append((u, pi, t))
            if not moves2:
                break
            for u, pi, t in moves2:
                for m in u:
                    ent = next((e for e in parts[pi]["paths"] if e[0] == m), None)
                    if ent is not None:
                        parts[pi]["paths"].remove(ent)
                        parts[t]["paths"].append(ent)
            for p in parts:
                p["size"] = len(p["paths"])
            parts = [p for p in parts if p["size"] > 0]
    return parts


def _pass_cap_enforce(
    parts: list[dict], rows: dict[str, int], mat, unit_of: dict[str, list[str]]
) -> list[dict]:
    # final cap enforcement: the routing passes above can pile files into
    # one part faster than their individual caps account for. Any part
    # over the mega-blob cap gets unit-split at progressively stricter
    # similarity until it actually divides; a pathological part that
    # refuses every cut is chunked deterministically by sorted path.
    for _guard in range(8):
        bigs = [p for p in parts if p["size"] > PART_CAP]
        if not bigs:
            break
        parts = [p for p in parts if p["size"] <= PART_CAP]
        for b in sorted(bigs, key=lambda p: -p["size"]):
            done = False
            for s in (0.65, 0.60, 0.55, 0.50, 0.45, 0.40):
                got = _split_units(b, rows, mat, unit_of, s, depth=MAX_DEPTH)
                if len(got) > 1 and max(g["size"] for g in got) <= PART_CAP:
                    parts.extend(got)
                    done = True
                    break
            if done:
                continue
            # unit-aware deterministic chunking: never divide a weld
            seen2: set[str] = set()
            ugroups: list[list[str]] = []
            for path, _cls in sorted(b["paths"]):
                if path in seen2:
                    continue
                u = [q for q in unit_of.get(path, [path])]
                ugroups.append(sorted(u))
                seen2.update(u)
            ugroups.sort(key=lambda g: g[0])
            chunk: list = []
            for g in ugroups:
                ents = [(q, dict(b["paths"]).get(q, "")) for q in g]
                chunk.extend(ents)
                if len(chunk) >= CHUNK_MIN:
                    parts.append({"paths": sorted(chunk), "size": len(chunk)})
                    chunk = []
            if chunk:
                parts.append({"paths": sorted(chunk), "size": len(chunk)})
    return parts


def _pass_stray_sweep(parts: list[dict], unit_of: dict[str, list[str]]) -> list[dict]:
    # final stray sweep: post-enforce composition can leave 1-2 pack files
    # in wrong parts — pull them to the pack's plurality home
    for _sweep in range(2):
        moved = False
        pack_parts2: dict[str, Counter] = defaultdict(Counter)
        pack_tot2: Counter = Counter()
        for pi, p in enumerate(parts):
            for path, _cls in p["paths"]:
                k = _pack_key(_stem(path)) if is_scene_path(path) else None
                if k:
                    pack_parts2[k][pi] += 1
                    pack_tot2[k] += 1
        for k in sorted(pack_tot2):
            if pack_tot2[k] < 3:
                continue
            home, hn = sorted(pack_parts2[k].items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if hn * 5 < pack_tot2[k] * 2 or len(parts[home]["paths"]) >= PART_CAP:
                continue
            for pi in sorted(pack_parts2[k]):
                if pi == home or len(parts[home]["paths"]) >= PART_CAP:
                    continue
                share = pack_parts2[k].get(pi, 0)
                n_scenes = sum(1 for q, _ in parts[pi]["paths"] if is_scene_path(q))
                if n_scenes and share * 2 >= n_scenes:
                    continue  # strayed part is itself pack-dominated
                for path, _cls in list(parts[pi]["paths"]):
                    if is_scene_path(path) and _pack_key(_stem(path)) == k:
                        u = _family_unit(path, k, unit_of)
                        for up in u:
                            for pj in range(len(parts)):
                                if pj == home:
                                    continue
                                ent = next(
                                    (e for e in parts[pj]["paths"] if e[0] == up), None
                                )
                                if ent is not None:
                                    parts[pj]["paths"].remove(ent)
                                    parts[home]["paths"].append(ent)
                                    moved = True
                        break
        if not moved:
            break
        for p in parts:
            p["size"] = len(p["paths"])
        parts = [p for p in parts if p["size"] > 0]
    return parts


def _pass_tiny_merge(parts: list[dict]) -> list[dict]:
    # tiny-part merge: fold dir-labeled parts of <=4 files into the larger
    # part sharing their dir identity (keeps the cluster count in band)
    from clusters import seed_chain
    for _merge_round in range(2):
        changed = False
        seg_count: Counter = Counter()
        for pi, p in enumerate(parts):
            for path, _cls in p["paths"]:
                for seg in seed_chain(path)[:1]:
                    seg_count[(pi, seg)] += 1
        for pi in sorted(range(len(parts)), key=lambda i: (parts[i]["size"], i)):
            p = parts[pi]
            if p["size"] > 4 or not p["paths"]:
                continue
            my_seg = None
            for path, _cls in p["paths"]:
                ch = seed_chain(path)
                if ch:
                    my_seg = ch[0]
                    break
            if my_seg is None:
                continue
            best, bn = -1, 0
            for pj, q in enumerate(parts):
                if pj == pi or not (5 <= len(q["paths"]) <= PART_SOFT_CAP):
                    continue
                n_shared = seg_count.get((pj, my_seg), 0)
                if n_shared > bn or (n_shared == bn and n_shared > 0 and best >= 0 and len(q["paths"]) > len(parts[best]["paths"])):
                    best, bn = pj, n_shared
            if best < 0 or bn < 1:
                continue
            parts[best]["paths"].extend(p["paths"])
            parts[best]["size"] = len(parts[best]["paths"])
            for path, _cls in p["paths"]:
                for seg in seed_chain(path)[:1]:
                    seg_count[(best, seg)] += 1
                    seg_count[(pi, seg)] -= 1
            p["paths"] = []
            changed = True
        parts = [p for p in parts if p["paths"]]
        if changed:
            for p in parts:
                p["size"] = len(p["paths"])
        if not changed:
            break
    return parts

def _pass_partition(parts: list[dict], unit_of: dict[str, list[str]]) -> list[dict]:
    """Partition invariant: every file ends up in exactly ONE part.

    The routing passes move partial families (`_family_unit` leaves
    other-pack scenes behind) while cap-enforce's split/chunk paths
    regroup by FULL welded units — a weld broken earlier plus a part
    split later re-pulls members that already moved, landing a file in
    two parts (#114). Resolve duplicates by weld plurality: the kept
    copy lives in the part holding the most of the file's welded unit,
    ties by larger part then lower index; other copies are dropped,
    repeated copies within the kept part collapse to the first, and
    emptied parts are removed. Identity on already-disjoint input."""
    owners: dict[str, list[int]] = defaultdict(list)
    for pi, p in enumerate(parts):
        for path, _cls in p["paths"]:
            owners[path].append(pi)
    dups = {path: pis for path, pis in owners.items() if len(pis) > 1}
    if not dups:
        return parts
    for path in sorted(dups):
        pis = dups[path]
        unit = set(unit_of.get(path) or [path])
        keep = max(
            pis,
            key=lambda pi: (
                sum(1 for q, _ in parts[pi]["paths"] if q in unit),
                len(parts[pi]["paths"]),
                -pi,
            ),
        )
        for pi in sorted(set(pis)):
            if pi == keep:
                kept: list = []
                for e in parts[pi]["paths"]:
                    if e[0] == path and any(k[0] == path for k in kept):
                        continue
                    kept.append(e)
                parts[pi]["paths"] = kept
            else:
                parts[pi]["paths"] = [e for e in parts[pi]["paths"] if e[0] != path]
    parts = [p for p in parts if p["paths"]]
    for p in parts:
        p["size"] = len(p["paths"])
    return parts


def _pass_label(
    parts: list[dict], rows: dict[str, int], mat, adj: dict[str, dict[str, float]] | None
) -> list[dict]:
    from clusters import (  # labeler seam stayed in clusters.py (#380 carve)
        LabelContext,
        _autoload_map,
        _dedupe_label,
        _doc_tokens,
        label_cluster,
    )
    parts.sort(key=lambda c: (-c["size"], c["paths"][0][0] if c["paths"] else ""))
    for i, c in enumerate(parts):
        c["id"] = i

    df: Counter = Counter()
    for c in parts:
        for t in set(_doc_tokens(c["paths"])):
            df[t] += 1
    ctx = LabelContext(
        autoloads=_autoload_map(),
        df=df,
        k=len(parts),
        centroid_path={c["id"]: _centroid_path(c["paths"], rows, mat) for c in parts},
        struct_adj=adj or {},
    )
    used: set[str] = set()
    for c in parts:  # size-desc: bigger clusters win the clean label
        label, conf, method = label_cluster(c["paths"], ctx, c["id"])
        label = _dedupe_label(label, used, c["paths"], ctx)
        # tests-majority clusters get an explicit Tests prefix (audit:
        # "World" labeling tests/world while real world code sat elsewhere)
        if (
            sum(1 for p, _ in c["paths"] if p.startswith("tests/")) * 20 >= 11 * len(c["paths"])
            and "test" not in label.lower()
        ):
            label = f"Tests · {label}"
        used.add(label)
        c["label"], c["confidence"], c["method"] = label, conf, method
    return parts


def finalize(
    raw: list[dict], ids: list[str], mat, split_sim: float = SPLIT_SIM, blob_min: int = BLOB_MIN,
    adj: dict[str, dict[str, float]] | None = None,
    units: list[list[str]] | None = None,
) -> list[dict]:
    """Split mega-blobs, merge tiny subclusters, label everything.

    raw: [{id, size, paths: [(path, class_name)]}] from the engine.
    adj: file-level weighted structural adjacency (hub gating + infra
    routing context); optional for the legacy kNN engine.
    units: welded scene+script groups from the engine; blob splitting
    runs on unit centroids so a weld is never divided.
    Returns same dicts plus label / confidence / method per cluster.

    Pipeline of ordered passes over the parts list (same order as the
    original monolith; each pass is the verbatim stage):
    split-units -> pack-split -> merge-small -> pack-consolidate ->
    usage -> scene-majority -> cap-enforce -> stray-sweep -> tiny-merge
    -> partition -> label.
    """
    rows = {p: i for i, p in enumerate(ids)}
    unit_of: dict[str, list[str]] = {p: list(u) for u in (units or []) for p in u}
    parts = _pass_split_units(raw, rows, mat, unit_of, split_sim, blob_min)
    parts = _pass_pack_split(parts, rows, mat)
    parts = _pass_merge_small(parts, rows, mat)
    parts, unit_of = _pass_pack_consolidate(parts, unit_of, units)
    parts = _pass_usage(parts, adj, unit_of)
    parts = _pass_scene_majority(parts, adj, unit_of)
    parts = _pass_cap_enforce(parts, rows, mat, unit_of)
    parts = _pass_stray_sweep(parts, unit_of)
    parts = _pass_tiny_merge(parts)
    parts = _pass_partition(parts, unit_of)
    return _pass_label(parts, rows, mat, adj)
