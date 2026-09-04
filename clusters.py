"""Cluster labeling + mega-blob splitting for nav.clusters().

Imported lazily by nav.clusters() so module import stays cheap. Works on
paths + class names + embeddings only — no language parsing, so a second
language's files label the same way.

Label cascade (first confident hit wins):
  1. AUTOLOAD   member is an autoload backing file       conf 1.0
 1b. STEM-PREFIX scene-dominant + >=60% of scene files share a Pfx_Seg*
               stem prefix (flat asset-pack folders) -> Seg  conf 0.7
  2. DIR        >=55% share the deepest dir chain; label = last
               non-generic segment of that chain           conf 0.85
  3. SCENE STEM scene-dominant + >=50% share a stem      conf 0.8
  4. c-TF-IDF   top discriminative identifier tokens     conf 0.6
  5. FALLBACK   centroid member's class_name / Mixed(N)  conf 0.4/0.2

Blob split (Qwen3-0.6B over-merge artifact, e.g. a 238-file mega-cluster):
  dir shortcut (>=60% one dir seg -> group by subdirectory) else
  AgglomerativeClustering(cosine, average, sim>=0.65); subclusters <3
  members merge into the nearest centroid (cosine>=0.55) or a Misc bucket;
  subclusters >60 members recurse once at sim 0.70 (max depth 2).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import nav

GENERIC_DIRS = {
    "scripts", "src", "core", "code", "main", "game", "utils", "helpers",
    "common", "shared", "autoload", "singletons", "resources", "assets",
    "scenes",
}

# compact english stopword set — no external dep, deterministic
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "into", "through", "during",
    "before", "after", "to", "from", "up", "down", "in", "out", "on",
    "off", "over", "under", "again", "further", "once", "here", "there",
    "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "can", "will", "just", "should", "now", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "of", "it", "its", "this", "that", "these", "those",
    "as", "while", "because", "until", "against", "between", "them",
    "they", "their", "you", "your", "we", "our",
}

BLOB_MIN = 60          # clusters larger than this get split
PACK_SPLIT_SHARE = 0.35  # each of the top-2 stem packs must cover >= this
                         # for the surgical asset-pack split to fire
BIG_SEED_MAX = 60      # seed groups at/above this size need cross_sim even
                       # for same-seed unions (blob-scale flat folders like
                       # VFX/Scenes chain unrelated asset packs at min_sim)
SMALL_MIN = 3          # subclusters smaller than this merge / go Misc
MERGE_SIM = 0.55       # centroid cosine needed to merge a small subcluster
SPLIT_SIM = 0.65       # default agglomerative similarity floor (dist 0.35)
RECURSE_SIM = 0.70     # second-pass floor for still-big subclusters (0.30)
MAX_DEPTH = 2

TOKEN_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")


def titleize(seg: str) -> str:
    return re.sub(r"[\s_-]+", " ", seg).strip().title()


def tokenize_ident(s: str) -> list[str]:
    s = s.replace("_", " ").replace("-", " ")
    s = TOKEN_CAMEL_RE.sub(r"\1 \2", s)
    return [t.lower() for t in s.split()]


def _ok_token(t: str) -> bool:
    return len(t) >= 3 and t not in STOPWORDS and t not in GENERIC_DIRS and not t.isdigit()


def dir_segments(path: str) -> list[str]:
    """Non-generic directory segments of a repo-relative posix path."""
    parts = path.split("/")[:-1]  # drop the file name
    return [p for p in parts if p.lower() not in GENERIC_DIRS]


def seed_chain(path: str) -> list[str]:
    """Dir segments with only the TOP-LEVEL generic walk dir stripped
    (scripts/, scenes/, ...). Nested segments stay even when generic-named,
    so VFX/Scenes and VFX/Fire are distinct depths, while scripts/inventory
    -> ["inventory"] and scripts/inventory/services -> ["inventory",
    "services"]."""
    parts = path.split("/")[:-1]
    if parts and parts[0].lower() in GENERIC_DIRS:
        parts = parts[1:]
    return parts


def dir_seed(path: str) -> str | None:
    """Full dir chain (top-generic stripped) — the union-permission key for
    dir-seeded clustering (nav.clusters). Files at different depths under
    the same tree only share a seed when they share the WHOLE dir chain;
    None means no dir at all (repo root): unconstrained, plain kNN."""
    chain = seed_chain(path)
    return "/".join(chain) if chain else None


def communities_graph(
    ids: list[str],
    metas: list[dict],
    mat,
    sim,
    knn,
    min_sim: float = 0.6,
    resolution: float = 1.0,
) -> list[dict]:
    """Louvain hybrid engine for nav.clusters(). Weighted graph over files:
    - semantic edges: mutual-kNN embedding pairs (weight = sim * 0.7)
    - structural edges from graph.py: call/signal func-pair counts capped
      at 5 per file pair; attach/inst 1.5 each
    tests/ files are excluded from the graph and returned as their own
    community (finalize's blob split sub-divides by subdirectory).
    Loose files (<2 structural edges AND max sim < 0.55) join the
    community dominating their dir seed. Deterministic (louvain seed=42).
    Returns raw [{id, size, paths: [(path, class_name)]}]."""
    import networkx as nx

    n = len(ids)
    idset = set(ids)
    tests = {i for i, p in enumerate(ids) if p.startswith("tests/")}
    id_of = {p: i for i, p in enumerate(ids)}

    # structural pairs from the code graph, aggregated to file level
    import graph as _graph

    g = _graph.get_graph()
    call_pairs: Counter = Counter()
    scene_pairs: Counter = Counter()
    for src_key, dsts in g.edges.items():
        sf = src_key.split("::")[0]
        if sf not in idset or id_of[sf] in tests:
            continue
        for dk in dsts:
            df = dk.split("::")[0]
            if df == sf or df not in idset or id_of[df] in tests:
                continue
            tys = g.edge_types.get((src_key, dk), set())
            if tys & {"call", "signal"}:
                call_pairs[(sf, df)] += 1
            if tys & {"attach", "inst"}:
                # tested attach at 0.8 to split scene<->script blobs: worse —
                # weaker binding lets scene-sim communities absorb the logic
                # core (19 logic/scene clashes vs 8 at 1.5)
                scene_pairs[(sf, df)] += 1.5

    G = nx.Graph()
    G.add_nodes_from(i for i in range(n) if i not in tests)

    def add(i: int, j: int, w: float) -> None:
        if G.has_edge(i, j):
            G[i][j]["weight"] += w
        else:
            G.add_edge(i, j, weight=w)

    max_sim = {i: 0.0 for i in G.nodes}
    for i in list(G.nodes):
        for j in knn[i]:
            j = int(j)
            if j in tests or j not in G:
                continue
            s = float(sim[i, j])
            if s < min_sim or i not in knn[j]:
                continue
            add(i, j, s * 0.7)
            if s > max_sim[i]:
                max_sim[i] = s
            if s > max_sim[j]:
                max_sim[j] = s

    for (sf, df), c in call_pairs.items():
        add(id_of[sf], id_of[df], min(c, 5))
    for (sf, df), w in scene_pairs.items():
        add(id_of[sf], id_of[df], w)

    comms = list(
        nx.community.louvain_communities(
            G, weight="weight", resolution=resolution, seed=42
        )
    )

    # loose-file overlay: weakly connected files join the community that
    # dominates their dir seed
    struct_deg: Counter = Counter()
    for sf, df in list(call_pairs) + list(scene_pairs):
        struct_deg[sf] += 1
        struct_deg[df] += 1
    loose = [
        i
        for i in G.nodes
        if struct_deg.get(ids[i], 0) < 2 and max_sim.get(i, 0.0) < 0.55
    ]
    if loose:
        comm_of: dict[int, int] = {}
        for ci, comm in enumerate(comms):
            for i in comm:
                comm_of[i] = ci
        seed_counts: dict[str, Counter] = {}
        for ci, comm in enumerate(comms):
            for i in comm:
                sd = dir_seed(ids[i])
                if sd:
                    seed_counts.setdefault(sd, Counter())[ci] += 1
        for i in loose:
            sd = dir_seed(ids[i])
            if sd and sd in seed_counts:
                ci, _cnt = sorted(
                    seed_counts[sd].items(), key=lambda kv: (-kv[1], kv[0])
                )[0]
                comms[comm_of[i]].discard(i)
                comms[ci].add(i)
                comm_of[i] = ci

    # dir-majority overlay: a .gd file whose dir chain is a minority (<3)
    # in its community joins the community where that chain (or its parent
    # chain, one level up) dominates — keeps inventory logic out of
    # UI-scene communities without breaking scene+script togetherness
    for _round in range(2):
        seed_comm: dict[tuple, Counter] = {}
        for ci, comm in enumerate(comms):
            for i in comm:
                if ids[i].endswith(".gd"):
                    chain = tuple(seed_chain(ids[i]))
                    if chain:
                        seed_comm.setdefault(chain, Counter())[ci] += 1
        moves = []
        for ci, comm in enumerate(comms):
            for i in comm:
                if not ids[i].endswith(".gd"):
                    continue
                chain = tuple(seed_chain(ids[i]))
                for key in (chain, chain[:-1]):
                    if not key:
                        break
                    dom = seed_comm.get(key)
                    if not dom:
                        continue
                    best_ci, best_n = sorted(
                        dom.items(), key=lambda kv: (-kv[1], kv[0])
                    )[0]
                    here_n = dom.get(ci, 0)
                    if best_ci != ci and best_n >= 3 and (
                        here_n < 3 or best_n >= here_n + 3
                    ):
                        moves.append((i, ci, best_ci))
                        break
        if not moves:
            break
        for i, ci, best_ci in moves:
            comms[ci].discard(i)
            comms[best_ci].add(i)

    out: list[dict] = []
    for comm in comms:
        if not comm:
            continue
        items = sorted(
            (ids[m], str((metas[m] or {}).get("class_name", ""))) for m in comm
        )
        out.append({"id": len(out), "size": len(items), "paths": items})
    if tests:
        items = sorted(
            (ids[m], str((metas[m] or {}).get("class_name", ""))) for m in tests
        )
        out.append({"id": len(out), "size": len(items), "paths": items})
    return out


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0]


def _stem_prefix_seg(members: list[tuple[str, str]]) -> str | None:
    """Varying segment of a shared stem prefix, for flat asset-pack folders
    (VFX/Scenes): >=60% of the cluster's scene files must match Pfx_Seg*
    where Seg is the first camel hump of the second underscore token
    (VFX_FireArea_A -> "Fire"); the winning segment must hold >=55% of the
    prefix-matching files. Returns the lowercase segment, or None."""
    stems = [_stem(p) for p, _ in members if p.endswith(".tscn")]
    if not stems:
        return None
    cand: list[tuple[str, str]] = []
    for s in stems:
        toks = s.split("_")
        if len(toks) < 2 or not toks[1]:
            continue
        humps = re.findall(r"[A-Z][a-z]*", toks[1])
        seg = humps[0] if humps else toks[1]
        cand.append((toks[0].lower(), seg.lower()))
    if not cand:
        return None
    pfx, pn = sorted(Counter(p for p, _ in cand).items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if pn / len(stems) < 0.6:
        return None
    seg, sn = sorted(Counter(g for p, g in cand if p == pfx).items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if sn / pn < 0.55 or pfx == seg:
        return None
    return seg


def _autoload_map() -> dict[str, str]:
    """rel path -> autoload singleton name (inverse of project.godot)."""
    pg = nav.ROOT / "project.godot"
    out: dict[str, str] = {}
    if not pg.is_file():
        return out
    in_auto = False
    for line in nav._read_text(pg).splitlines():
        s = line.strip()
        if s.startswith("[autoload]"):
            in_auto = True
            continue
        if s.startswith("["):
            in_auto = False
        if in_auto:
            m = re.match(r'^(\w+)\s*=\s*"\*?res://([\w/.-]+\.\w+)"', s)
            if m:
                out[m.group(2)] = m.group(1)
    return out


@dataclass
class LabelContext:
    autoloads: dict[str, str]
    df: Counter                    # token -> number of clusters containing it
    k: int                         # cluster count
    centroid_path: dict[int, str] = field(default_factory=dict)


def _doc_tokens(paths: list[tuple[str, str]]) -> list[str]:
    toks: list[str] = []
    for p, cls in paths:
        toks.extend(t for t in tokenize_ident(_stem(p)) if _ok_token(t))
        if cls:
            toks.extend(t for t in tokenize_ident(cls) if _ok_token(t))  # x2 weight
    return toks


def tfidf_terms(paths: list[tuple[str, str]], ctx: LabelContext, n: int = 3) -> list[str]:
    """c-TF-IDF: (freq_in_cluster / total_tokens) * log(1 + K / df)."""
    toks = _doc_tokens(paths)
    if not toks:
        return []
    total = len(toks)
    freq = Counter(toks)
    scored = []
    for t, f in freq.items():
        df = max(1, ctx.df.get(t, 0))
        scored.append((f / total * math.log(1 + ctx.k / df), t))
    scored.sort(key=lambda st: (-st[0], st[1]))  # score desc, alpha tie-break
    return [t for _, t in scored[:n]]


def label_cluster(
    members: list[tuple[str, str]], ctx: LabelContext, cid: int = -1
) -> tuple[str, float, str]:
    ordered = sorted(members)
    # 1. autoload backing file
    for p, _ in ordered:
        if p in ctx.autoloads:
            return ctx.autoloads[p], 1.0, "autoload"
    n = len(members) or 1
    # 1b. stem-prefix pack (scene-dominant): flat asset-pack folders hold
    # every pack in one dir; pack identity is the stem prefix VFX_Fire*.
    # The dir cascade would bottom out at the shared "VFX" segment, so the
    # varying segment is extracted BEFORE the dir rule can flatten it.
    scene_all = [p for p, _ in members if p.endswith(".tscn")]
    if len(scene_all) * 2 > len(members):
        seg = _stem_prefix_seg(members)
        if seg:
            return titleize(seg), 0.7, "stem"
    # 2. deepest majority dir chain: walk prefix-constrained while the
    # majority (>=55%) still shares the chain; label = last NON-GENERIC
    # segment of that chain. A cluster spread over VFX/Scenes bottoms out
    # at "Vfx" (mixed symptom); VFX/Fire labels "Fire".
    chains = [seed_chain(p) for p, _ in members]
    nonempty = [c for c in chains if c]
    cur: list[str] = []
    if nonempty:
        while True:
            depth = len(cur) + 1
            cnt: Counter = Counter()
            for c in nonempty:
                if len(c) >= depth and c[: depth - 1] == cur:
                    cnt[tuple(c[:depth])] += 1
            if not cnt:
                break
            top, ntop = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if ntop / n < 0.55:
                break
            cur = list(top)
    if cur:
        good = [s for s in cur if s.lower() not in GENERIC_DIRS]
        if good:
            return titleize(good[-1]), 0.85, "dir"
    # 3. scene stem
    scene = scene_all
    if len(scene) * 2 > len(members):
        stems = Counter(_stem(p) for p in scene)
        stem, cnt = sorted(stems.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if cnt / n >= 0.5:
            return titleize(stem), 0.8, "scene"
    # 4. c-TF-IDF terms
    terms = tfidf_terms(members, ctx, n=3)
    if terms:
        return " - ".join(terms), 0.6, "tfidf"
    # 5. centroid member's class_name, else Mixed
    cp = ctx.centroid_path.get(cid, "")
    for p, cls in ordered:
        if p == cp and cls:
            return titleize(cls), 0.4, "class"
    for _, cls in ordered:
        if cls:
            return titleize(cls), 0.35, "class"
    return f"Mixed ({len(members)})", 0.2, "mixed"


def _dedupe_label(label: str, used: set[str], paths: list[tuple[str, str]], ctx: LabelContext) -> str:
    if label not in used:
        return label
    low = label.lower()
    for extra in tfidf_terms(paths, ctx, n=8):
        if extra in low:
            continue  # never append a term already contained in the label
        cand = f"{label} {extra}"
        if cand not in used:
            return cand
    # last-two-segment join from a member's dir chain ("Vfx Fire")
    chain = [s for s in seed_chain(paths[0][0]) if s.lower() not in GENERIC_DIRS]
    for j in range(len(chain) - 1, -1, -1):
        cand = " ".join(titleize(s) for s in chain[max(0, j - 1) : j + 1])
        if not cand or cand in used:
            continue
        if all(w.lower() in low for w in cand.split()):
            continue  # "Vfx Vfx" guard: every word already in the label
        return cand
    i = 2
    while f"{label} {i}" in used:
        i += 1
    return f"{label} {i}"


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


def _split_cluster(
    cluster: dict, rows: dict[str, int], mat, sim: float, depth: int
) -> list[dict]:
    paths = [p for p, _ in cluster["paths"]]
    # 1. one dir segment covering >=60% -> group by subdirectory, done
    seg_counts: Counter = Counter()
    for p in paths:
        for seg in set(dir_segments(p)):
            seg_counts[seg] += 1
    groups: dict[str, list[int]] = defaultdict(list)
    if seg_counts:
        seg, cnt = sorted(seg_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if cnt / len(paths) >= 0.60:
            for i, p in enumerate(paths):
                groups["/".join(p.split("/")[:-1])].append(i)
            if len(groups) < 2:
                # flat folder: subdir grouping returns the blob itself —
                # fall through to the embedding split instead
                groups = defaultdict(list)
    if not groups:
        # 2. agglomerative over the blob's embeddings only
        from sklearn.cluster import AgglomerativeClustering

        idxs = [rows[p] for p in paths if p in rows]
        if len(idxs) < len(paths):
            return [dict(cluster)]  # embeddings missing — leave untouched
        labels = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=1.0 - sim,
            metric="cosine",
            linkage="average",
        ).fit_predict(mat[idxs])
        for pos, lab in enumerate(labels):
            groups[int(lab)].append(pos)
    out: list[dict] = []
    for _, member_pos in sorted(groups.items(), key=lambda kv: (-len(kv[1]), paths[kv[1][0]])):
        sub_paths = [cluster["paths"][i] for i in sorted(member_pos)]
        sub = {"paths": sub_paths, "size": len(sub_paths)}
        if depth < MAX_DEPTH and sub["size"] > BLOB_MIN:
            out.extend(_split_cluster(sub, rows, mat, RECURSE_SIM, depth + 1))
        else:
            out.append(sub)
    return out or [dict(cluster)]


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
    packs each holding >= PACK_SPLIT_SHARE of the cluster (the wind+earth
    mixed community in the flat VFX/Scenes folder). Global resolution is
    untouched. Named pack groups smaller than SMALL_MIN merge into the
    nearest surviving pack centroid (>= MERGE_SIM) else join the no-pack
    leftover bucket."""
    import numpy as np

    paths = cluster["paths"]
    n = len(paths)
    scene = [p for p, _ in paths if p.endswith(".tscn")]
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


def finalize(
    raw: list[dict], ids: list[str], mat, split_sim: float = SPLIT_SIM, blob_min: int = BLOB_MIN
) -> list[dict]:
    """Split mega-blobs, merge tiny subclusters, label everything.

    raw: [{id, size, paths: [(path, class_name)]}] from nav's union-find.
    Returns same dicts plus label / confidence / method per cluster.
    """
    rows = {p: i for i, p in enumerate(ids)}
    parts: list[dict] = []
    for c in raw:
        if c["size"] > blob_min:
            parts.extend(_split_cluster(c, rows, mat, split_sim, depth=1))
        else:
            parts.append({"paths": list(c["paths"]), "size": c["size"]})
    # surgical asset-pack split for genuinely mixed pack communities
    parts = [p for c in parts for p in _split_pack_cluster(c, rows, mat)]
    parts = _merge_small(parts, rows, mat)
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
    )
    used: set[str] = set()
    for c in parts:  # size-desc: bigger clusters win the clean label
        label, conf, method = label_cluster(c["paths"], ctx, c["id"])
        label = _dedupe_label(label, used, c["paths"], ctx)
        used.add(label)
        c["label"], c["confidence"], c["method"] = label, conf, method
    return parts


def coarse_groups(fine: list[dict], ids: list[str], mat, cut: float = 0.45) -> list[dict]:
    """Optional coarse level: scipy average-linkage over cluster centroids
    (cosine), cut at `cut` distance, nudged into 3..8 groups. Named by
    dominant dir (>=60%) else c-TF-IDF union."""
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage

    rows = {p: i for i, p in enumerate(ids)}
    cents = []
    for c in fine:
        vecs = [mat[rows[p]] for p, _ in c["paths"] if p in rows]
        v = np.mean(vecs, axis=0) if vecs else np.zeros(mat.shape[1])
        v = v / max(float(np.linalg.norm(v)), 1e-9)
        cents.append(v)
    z = linkage(np.array(cents), method="average", metric="cosine")
    t = cut
    labels = fcluster(z, t=t, criterion="distance")
    # higher cut distance = more merging = fewer groups
    while len(set(labels)) > 8 and t < 0.95:
        t += 0.05
        labels = fcluster(z, t=t, criterion="distance")
    while len(set(labels)) < 3 and t > 0.05:
        t -= 0.05
        labels = fcluster(z, t=t, criterion="distance")
    by_group: dict[int, list[int]] = defaultdict(list)
    for ci, lab in enumerate(labels):
        by_group[int(lab)].append(ci)
    # name by dominant dir else tfidf union
    df: Counter = Counter()
    for c in fine:
        for tok in set(_doc_tokens(c["paths"])):
            df[tok] += 1
    ctx = LabelContext(autoloads=_autoload_map(), df=df, k=len(fine))
    out = []
    for lab, cis in sorted(by_group.items()):
        paths = [p for ci in cis for p in fine[ci]["paths"]]
        name = ""
        segs: Counter = Counter()
        for p, _ in paths:
            for seg in set(dir_segments(p)):
                segs[seg] += 1
        if segs:
            seg, cnt = sorted(segs.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if cnt / len(paths) >= 0.60:
                name = titleize(seg)
        if not name:
            terms = tfidf_terms(paths, ctx, n=3)
            name = " - ".join(terms) if terms else "Mixed"
        out.append({"id": len(out), "label": name, "cluster_ids": cis, "size": len(paths)})
    return out
