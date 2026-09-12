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
    so art/scenes and art/scenes/fire are distinct depths, while src/inventory
    -> ["inventory"] and src/inventory/services -> ["inventory",
    "services"]."""
    parts = path.split("/")[:-1]
    if parts and parts[0].lower() in GENERIC_DIRS:
        parts = parts[1:]
    return parts


def dir_seed(path: str) -> str | None:
    """Full dir chain (top-generic stripped) — the dir key for the
    dir-seeded fallbacks (loose-file unit seeding below). Files at
    different depths under the same tree only share a seed when they
    share the WHOLE dir chain; None means no dir at all (repo root):
    unconstrained, plain kNN."""
    chain = seed_chain(path)
    return "/".join(chain) if chain else None

def topk_desc(sim, k: int):
    """Row-wise top-k column indices by descending similarity — the exact
    index lists ``np.argsort(-sim, axis=1)[:, :k]`` yields, at argpartition
    cost instead of a full n·log n sort per row (engine scale: seconds,
    not tens of seconds). Exactness contract: when a row's top-(k+1)
    values contain a duplicate — a tie straddles the cut or reorders the
    kept set; degenerate/fake embeddings do this, real ones don't — the
    row falls back to the full argsort, so every consumer sees lists
    identical to the old implementation (downstream iteration order
    feeds louvain edge insertion)."""
    import numpy as np

    n = sim.shape[1]
    if k >= n:
        return np.argsort(-sim, axis=1)[:, :k]
    kk = k + 1
    part = np.argpartition(-sim, kk - 1, axis=1)[:, :kk]
    sv = np.take_along_axis(sim, part, axis=1)
    order = np.argsort(-sv, axis=1, kind="stable")
    part = np.take_along_axis(part, order, axis=1)
    sv = np.take_along_axis(sv, order, axis=1)
    thr = sv[:, -1]
    tied = (sv[:, :-1] == sv[:, 1:]).any(axis=1) | (
        (sim == thr[:, None]).sum(axis=1) > 1
    )
    out = part[:, :k].copy()
    if tied.any():
        out[tied] = np.argsort(-sim[tied], axis=1)[:, :k]
    return out


def _weld_units(
    ids: list[str], tests: set[int], id_of: dict[str, int], g
) -> tuple[callable, dict[int, list[int]]]:
    """Atomic scene+script pre-merge: a .tscn and each script it attaches
    form ONE louvain unit (hard union). Returns (ufind, members_of):
    ufind is the union-find root resolver callers reuse for unit identity,
    members_of maps root -> member indices."""
    n = len(ids)
    upar = list(range(n))

    def ufind(x: int) -> int:
        while upar[x] != x:
            upar[x] = upar[upar[x]]
            x = upar[x]
        return x

    mcnt = [1] * n

    def _union(i: int, j: int, cap: int | None) -> None:
        a, b = ufind(i), ufind(j)
        if a == b:
            return
        if cap is not None and mcnt[a] + mcnt[b] > cap:
            return
        lo, hi = min(a, b), max(a, b)
        upar[hi] = lo
        mcnt[lo] += mcnt[hi]
        mcnt[hi] = 0

    def _scripts(rel: str) -> list[str]:
        out: list[str] = []
        for s in getattr(g.files[rel], "scripts", ()) or ():
            srel = s[len("res://"):] if s.startswith("res://") else s
            j = id_of.get(srel)
            if j is not None and j not in tests:
                out.append(srel)
        return out

    # pass 1: each scene welds to its PRIMARY attached script unconditionally
    for rel, i in sorted(id_of.items()):
        if not rel.endswith(".tscn") or i in tests or rel not in g.files:
            continue
        att = getattr(g.files[rel], "attached_script", None) or ""
        if not att:
            continue
        aj = att[len("res://"):] if att.startswith("res://") else att
        j = id_of.get(aj)
        if j is not None and j not in tests:
            _union(i, j, None)
    # pass 2: remaining script welds capped — a heavily-shared script
    # (a hub component can sit on half the UI scenes) must not transitively
    # glue dozens of scenes into one mega-unit
    for rel, i in sorted(id_of.items()):
        if not rel.endswith(".tscn") or i in tests or rel not in g.files:
            continue
        for srel in _scripts(rel):
            _union(i, id_of[srel], 12)
    members_of: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        if i not in tests:
            members_of[ufind(i)].append(i)
    return ufind, members_of


def _unit_paths(ids: list[str], members_of: dict[int, list[int]], r: int) -> list[str]:
    return sorted(ids[m] for m in members_of[r])


def _unit_seed(ids: list[str], members_of: dict[int, list[int]], r: int) -> str | None:
    for p in _unit_paths(ids, members_of, r):
        if p.endswith(".gd"):
            return dir_seed(p)
    ps = _unit_paths(ids, members_of, r)
    return dir_seed(ps[0]) if ps else None


def _is_pure_gd(ids: list[str], members_of: dict[int, list[int]], r: int) -> bool:
    mem = members_of[r]
    return bool(mem) and all(ids[m].endswith(".gd") for m in mem)


def _sim_graph(
    ids: list[str], tests: set[int], members_of: dict[int, list[int]], knn, sim,
    min_sim: float, call_pairs: Counter, scene_pairs: Counter,
    id_of: dict[str, int], ufind: callable,
):
    """Contracted louvain graph over welded units: semantic mutual-kNN
    edges (sim * 0.7) plus structural call/scene-pair edges. Returns
    (G, max_sim). Determinism: node order sorted, edge accumulation in
    the caller's fixed loop order."""
    import networkx as nx

    n = len(ids)
    G = nx.Graph()
    G.add_nodes_from(sorted(members_of))

    def add(i: int, j: int, w: float) -> None:
        ri, rj = ufind(i), ufind(j)
        if ri == rj:
            return  # intra-unit edge: the unit IS the hard union
        if G.has_edge(ri, rj):
            G[ri][rj]["weight"] += w
        else:
            G.add_edge(ri, rj, weight=w)

    max_sim = {r: 0.0 for r in G.nodes}
    for i in range(n):
        if i in tests:
            continue
        for j in knn[i]:
            j = int(j)
            if j in tests:
                continue
            s = float(sim[i, j])
            if s < min_sim or i not in knn[j]:
                continue
            add(i, j, s * 0.7)
            for r in (ufind(i), ufind(j)):
                if s > max_sim[r]:
                    max_sim[r] = s

    for (sf, df), c in call_pairs.items():
        add(id_of[sf], id_of[df], min(c, 5))
    for (sf, df), w in scene_pairs.items():
        add(id_of[sf], id_of[df], w)
    return G, max_sim


def _route_infra(
    comms: list[set], ids: list[str], members_of: dict[int, list[int]],
    adj: dict[str, dict[str, float]], idset: set[str],
    pack_home: dict[str, int], rev_adj: dict[str, set[str]],
) -> None:
    """Infra routing (in place over comms): generic scripts referenced by
    >=2 pack scenes join the community holding the plurality of those
    scenes' packs (audit: three generic fx scripts used only by two
    element packs ended up parked in a third pack's community)."""
    pack_of_scene: dict[str, str] = {}
    for p in idset:
        if p.endswith(".tscn"):
            k = _pack_key(_stem(p))
            if k:
                pack_of_scene[p] = k

    def comm_size(ci: int) -> int:
        return sum(len(members_of[r]) for r in comms[ci])

    infra_moves = []
    for ci, comm in enumerate(comms):
        for r in comm:
            if not _is_pure_gd(ids, members_of, r):
                continue
            p = ids[members_of[r][0]]
            votes: Counter = Counter()
            for nb in list(adj.get(p, {})) + list(rev_adj.get(p, ())):
                k = pack_of_scene.get(nb)
                if k and k in pack_home:
                    votes[pack_home[k]] += 1
            tot = sum(votes.values())
            if tot < 2:
                continue
            ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
            (t, tv) = ranked[0]
            if len(ranked) > 1 and ranked[1][1] == tv:
                t2 = ranked[1][0]
                t = t2 if comm_size(t2) > comm_size(t) else t
            if tv * 2 < tot or t == ci:
                continue
            infra_moves.append((r, ci, t))
    for r, ci, t in infra_moves:
        comms[ci].discard(r)
        comms[t].add(r)


def communities_graph(
    ids: list[str],
    metas: list[dict],
    mat,
    sim,
    knn,
    min_sim: float = 0.6,
    resolution: float = 1.0,
) -> tuple[list[dict], dict]:
    """Louvain hybrid engine for nav.clusters(). Weighted graph over files:
    - semantic edges: mutual-kNN embedding pairs (weight = sim * 0.7)
    - structural edges from graph.py: call/signal func-pair counts capped
      at 5 per file pair; attach/inst 1.5 each
    - ATOMIC PRE-MERGE: every .tscn and the scripts it attaches are
      contracted into one clustering unit (hard union) — kills the
      scene/script split-brain the audit flagged everywhere
    tests/ files are excluded from the graph and returned as their own
    community (finalize's blob split sub-divides by subdirectory).
    Loose files (<2 structural edges AND max sim < 0.55) join the
    community dominating their dir seed. Deterministic (louvain seed=42).
    Returns (raw [{id, size, paths: [(path, class_name)]}], file-level
    weighted structural adjacency for labeler hub gating)."""
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

    # file-level weighted adjacency (labeler hub gating + infra routing)
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    for (sf, df), c in call_pairs.items():
        adj[sf][df] = adj[sf].get(df, 0.0) + min(c, 5)
    for (sf, df), w in scene_pairs.items():
        adj[sf][df] = adj[sf].get(df, 0.0) + w
    adj = {s: dict(d) for s, d in adj.items()}

    # atomic scene+script pre-merge: a .tscn and each script it attaches
    # form ONE louvain unit
    ufind, members_of = _weld_units(ids, tests, id_of, g)

    G, max_sim = _sim_graph(
        ids, tests, members_of, knn, sim, min_sim,
        call_pairs, scene_pairs, id_of, ufind,
    )

    comms = list(
        nx.community.louvain_communities(
            G, weight="weight", resolution=resolution, seed=42
        )
    )

    # loose-unit overlay: weakly connected units join the community that
    # dominates their dir seed (moves whole scene+script units)
    struct_deg: Counter = Counter()
    for sf, df in list(call_pairs) + list(scene_pairs):
        struct_deg[ufind(id_of[sf])] += 1
        struct_deg[ufind(id_of[df])] += 1
    loose = [
        r
        for r in G.nodes
        if struct_deg.get(r, 0) < 2 and max_sim.get(r, 0.0) < 0.55
    ]
    if loose:
        comm_of: dict[int, int] = {}
        for ci, comm in enumerate(comms):
            for r in comm:
                comm_of[r] = ci
        seed_counts: dict[str, Counter] = {}
        for ci, comm in enumerate(comms):
            for r in comm:
                sd = _unit_seed(ids, members_of, r)
                if sd:
                    seed_counts.setdefault(sd, Counter())[ci] += 1
        for r in loose:
            sd = _unit_seed(ids, members_of, r)
            if sd and sd in seed_counts:
                ci, _cnt = sorted(
                    seed_counts[sd].items(), key=lambda kv: (-kv[1], kv[0])
                )[0]
                comms[comm_of[r]].discard(r)
                comms[ci].add(r)
                comm_of[r] = ci

    # dir-majority overlay: a pure-script unit whose dir chain is a
    # minority (<3) in its community joins the community where that chain
    # (or its parent chain, one level up) dominates — scene+script units
    # are atomic and never moved by this rule
    for _round in range(2):
        seed_comm: dict[tuple, Counter] = {}
        for ci, comm in enumerate(comms):
            for r in comm:
                if _is_pure_gd(ids, members_of, r):
                    chain = tuple(seed_chain(ids[members_of[r][0]]))
                    if chain:
                        seed_comm.setdefault(chain, Counter())[ci] += 1
        moves = []
        for ci, comm in enumerate(comms):
            for r in comm:
                if not _is_pure_gd(ids, members_of, r):
                    continue
                chain = tuple(seed_chain(ids[members_of[r][0]]))
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
                        moves.append((r, ci, best_ci))
                        break
        if not moves:
            break
        for r, ci, best_ci in moves:
            comms[ci].discard(r)
            comms[best_ci].add(r)

    # pack routing: a unit whose scenes all belong to one Pfx_Seg pack
    # whose plurality home is a DIFFERENT community joins that home —
    # pack identity beats embedding ties (audit: fire subemitter scenes
    # landed in the blood pack, wind scenes in the earth pack)
    pack_comm: dict[str, Counter] = {}
    for ci, comm in enumerate(comms):
        for r in comm:
            for p in _unit_paths(ids, members_of, r):
                if p.endswith(".tscn"):
                    k = _pack_key(_stem(p))
                    if k:
                        pack_comm.setdefault(k, Counter())[ci] += 1
    pack_home: dict[str, int] = {}
    for k, cnt in pack_comm.items():
        ci, cn = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if cn >= 3:
            pack_home[k] = ci
    pack_moves = []
    for ci, comm in enumerate(comms):
        for r in comm:
            ks = {
                k
                for p in _unit_paths(ids, members_of, r)
                if p.endswith(".tscn") and (k := _pack_key(_stem(p)))
            }
            if not ks:
                continue
            targets = {pack_home[k] for k in ks if k in pack_home}
            if len(targets) != 1:
                continue
            t = targets.pop()
            if t == ci:
                continue
            if all(
                pack_comm[k].get(t, 0) > pack_comm[k].get(ci, 0) for k in ks
            ):
                pack_moves.append((r, ci, t))
    for r, ci, t in pack_moves:
        comms[ci].discard(r)
        comms[t].add(r)

    # infra routing: generic scripts referenced by >=2 pack scenes join
    # the community holding the plurality of those scenes' packs (audit:
    # three generic fx scripts used only by two element packs ended
    # up parked in a third pack's community)
    rev_adj: dict[str, set[str]] = defaultdict(set)
    for s, d in adj.items():
        for t in d:
            rev_adj[t].add(s)
    _route_infra(comms, ids, members_of, adj, idset, pack_home, rev_adj)

    # usage routing: a pure-script unit whose structural neighbours
    # (either direction) vote overwhelmingly for ONE other community
    # joins it — generalizes infra routing from packs to any hub the
    # script is actually called by / calls (audit: camera_input_utils
    # stranded with math utils while its callers cluster elsewhere)
    comm_of2: dict[int, int] = {}
    for ci, comm in enumerate(comms):
        for r in comm:
            comm_of2[r] = ci
    comm_size2 = [sum(len(members_of[r]) for r in comm) for comm in comms]
    usage_moves = []
    for ci, comm in enumerate(comms):
        for r in comm:
            if not _is_pure_gd(ids, members_of, r):
                continue
            if any(dir_segments(q) for q in _unit_paths(ids, members_of, r)):
                continue  # real dir identity: dir-majority overlay owns it
            p = ids[members_of[r][0]]
            votes: Counter = Counter()
            for nb in adj.get(p, {}):
                nr = id_of.get(nb)
                if nr is not None and nr not in tests:
                    votes[comm_of2[ufind(nr)]] += 1
            for nb in rev_adj.get(p, ()):
                nr = id_of.get(nb)
                if nr is not None and nr not in tests:
                    votes[comm_of2[ufind(nr)]] += 1
            votes.pop(ci, None)
            tot = sum(votes.values())
            if tot < 2:
                continue
            (t, tv) = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if tv != tot or comm_size2[t] > 66:
                continue
            usage_moves.append((r, ci, t))
    for r, ci, t in usage_moves:
        comms[ci].discard(r)
        comms[t].add(r)

    # structural merge-down: acceptance band wants 15-40 communities, but
    # the contracted graph fragments louvain output. Greedily merge the
    # community pair with the highest inter-community weight (ties: smaller
    # combined size, then lower index) while the result stays under the
    # mega-blob cap — structure decides who merges, never embeddings alone.
    TARGET_COMMS = 38
    MAX_COMM = 58

    def _comm_weight(a: int, b: int) -> float:
        small = comms[a] if len(comms[a]) <= len(comms[b]) else comms[b]
        other = comms[b] if small is comms[a] else comms[a]
        return sum(G[ra][rb]["weight"] for ra in small for rb in other if G.has_edge(ra, rb))

    # merge-down at engine scale (spec §4 #8): louvain fragments into
    # hundreds of seed communities and the naive rescan re-summed every
    # inter-community weight — an all-member-pairs edge sweep — after each
    # of the ~C-38 merges. Pre-aggregate instead: pair weights keyed by
    # community CONTENTS (communities are pairwise disjoint and only grow
    # by union here, so a frozenset identifies both the community and the
    # exact float the naive scan sums on those same set objects) and
    # sizes as a positional column. Unchanged pairs keep their cached
    # float — bit-identical to recomputation, O(1) per scanned pair.
    _wkey = [frozenset(c) for c in comms]
    _wmin = [min(k) if k else -1 for k in _wkey]
    _wsz = [sum(len(members_of[r]) for r in c) for c in comms]
    _wcache: dict = {}

    def _cw(a: int, b: int) -> float:
        ka, kb = _wkey[a], _wkey[b]
        key = (ka, kb) if _wmin[a] < _wmin[b] else (kb, ka)
        w = _wcache.get(key)
        if w is None:
            w = _wcache[key] = _comm_weight(a, b)
        return w

    while len(comms) > TARGET_COMMS:
        best = None
        for a in range(len(comms)):
            for b in range(a + 1, len(comms)):
                sz = _wsz[a] + _wsz[b]
                if sz > MAX_COMM:
                    continue
                key = (-_cw(a, b), sz, a, b)
                if best is None or key < best[0]:
                    best = (key, a, b)
        if best is None:
            break
        _, a, b = best
        comms[a] |= comms[b]
        _wkey[a] = _wkey[a] | _wkey[b]
        _wmin[a] = min(_wmin[a], _wmin[b])
        _wsz[a] += _wsz[b]
        del comms[b]
        del _wkey[b]
        del _wmin[b]
        del _wsz[b]

    out: list[dict] = []
    for comm in comms:
        files = sorted(m for r in comm for m in members_of[r])
        if not files:
            continue
        items = sorted(
            (ids[m], str((metas[m] or {}).get("class_name", ""))) for m in files
        )
        out.append({"id": len(out), "size": len(items), "paths": items})
    if tests:
        items = sorted(
            (ids[m], str((metas[m] or {}).get("class_name", ""))) for m in tests
        )
        out.append({"id": len(out), "size": len(items), "paths": items})
    units = [
        sorted(ids[m] for m in mem)
        for _, mem in sorted(members_of.items())
        if len(mem) > 1
    ]
    return out, dict(adj), units


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0]


def _stem_prefix_seg(members: list[tuple[str, str]]) -> str | None:
    """Varying segment of a shared stem prefix, for flat asset-pack folders
    (art/scenes): >=60% of the cluster's scene files must match Pfx_Seg*
    where Seg is the first camel hump of the second underscore token
    (Art_FireArea_A -> "Fire"); the winning segment must hold >=55% of the
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
    struct_adj: dict[str, dict[str, float]] = field(default_factory=dict)


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
    # 1. autoload backing file — ONLY when it is the cluster's in-cluster
    # hub (highest weighted structural in-degree). A non-hub autoload
    # would brand a grab-bag with conf 1.0 (audit: one audio hub on 61
    # mostly-unrelated files, E2EMatchProbe on 7 asset tools).
    mem_paths = [p for p, _ in ordered]

    def _indeg(p: str) -> float:
        return sum(ctx.struct_adj.get(s, {}).get(p, 0.0) for s in mem_paths)

    autos = [p for p in mem_paths if p in ctx.autoloads]
    if autos:
        hub = sorted(mem_paths, key=lambda p: (-_indeg(p), p))[0]
        if hub in autos and _indeg(hub) > 0:
            return ctx.autoloads[hub], 1.0, "autoload"
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
    # segment of that chain. A cluster spread over a flat asset folder
    # bottoms out at the root name (mixed symptom); subfolders win.
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
    packs each holding >= PACK_SPLIT_SHARE of the cluster (a two-element
    mixed community in a flat asset folder). Global resolution is
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


def _family_unit(path: str, k: str | None, unit_of: dict) -> list[str]:
    """Pack-routing move set: the scene itself, its non-scene unit members
    (scripts), and only SAME-pack scenes. VFX scenes often weld into one
    unit via a shared script — moving the whole unit would drag other
    packs' scenes back and forth between sweeps."""
    return [
        q
        for q in unit_of.get(path, [path])
        if q == path or not q.endswith(".tscn") or (_pack_key(_stem(q)) or "") == (k or "")
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
        gd = [q for q in grp if q.endswith(".gd")]
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
                if path.endswith(".tscn") and _pack_key(_stem(path)) is not None:
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
                    k = _pack_key(_stem(path)) if path.endswith(".tscn") else None
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
                        if path.endswith(".tscn") and _pack_key(_stem(path)) == k:
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
                    if not path.endswith(".gd") or path in unit_of:
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
                if not path.endswith(".tscn"):
                    continue
                u = unit_of.get(path) or [path]
                if u[0] != path:
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
                k = _pack_key(_stem(path)) if path.endswith(".tscn") else None
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
                n_scenes = sum(1 for q, _ in parts[pi]["paths"] if q.endswith(".tscn"))
                if n_scenes and share * 2 >= n_scenes:
                    continue  # strayed part is itself pack-dominated
                for path, _cls in list(parts[pi]["paths"]):
                    if path.endswith(".tscn") and _pack_key(_stem(path)) == k:
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


def _pass_label(
    parts: list[dict], rows: dict[str, int], mat, adj: dict[str, dict[str, float]] | None
) -> list[dict]:
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
    -> label.
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
    return _pass_label(parts, rows, mat, adj)


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


def crosstalk(cs: list[dict], g=None) -> dict:
    """Coupling-hotspot report: structural (call/signal/var/inst/attach)
    edges that cross cluster boundaries. Generic over languages — cluster
    membership comes from `cs` (nav.clusters() output), edges from the
    structural graph of the active config. Answers "which subsystems are
    wired together despite clustering apart" and "which clusters are
    internally hollow"."""
    import graph

    if g is None:
        g = graph.get_graph()
    file_cluster: dict[str, int] = {}
    for c in cs:
        for p, _cls in c["paths"]:
            file_cluster[p] = c["id"]
    by_id = {c["id"]: c for c in cs}
    internal_by: Counter = Counter()
    cluster_out: Counter = Counter()
    cluster_in: Counter = Counter()
    pair_edges: Counter = Counter()  # (min_id, max_id) -> cross func pairs
    pair_files: dict[tuple[int, int], Counter] = defaultdict(Counter)
    unclustered = 0
    for src, dsts in g.edges.items():
        sf = src.split("::")[0]
        for dst in dsts:
            df = dst.split("::")[0]
            if sf == df:
                continue  # same-file pairs carry no cluster signal
            a = file_cluster.get(sf)
            b = file_cluster.get(df)
            if a is None or b is None:
                unclustered += 1
                continue
            if a == b:
                internal_by[a] += 1
                continue
            cluster_out[a] += 1
            cluster_in[b] += 1
            key = (min(a, b), max(a, b))
            pair_edges[key] += 1
            pair_files[key][f"{sf} -> {df}"] += 1
    ext_total = int(sum(pair_edges.values()))
    int_total = int(sum(internal_by.values()))
    by_cluster = []
    for c in cs:
        cid = c["id"]
        ext = cluster_out[cid] + cluster_in[cid]
        tot = ext + internal_by[cid]
        by_cluster.append(
            {
                "id": cid,
                "label": c.get("label", ""),
                "size": c["size"],
                "internal": internal_by[cid],
                "external_out": cluster_out[cid],
                "external_in": cluster_in[cid],
                "external_share": round(ext / tot, 3) if tot else 0.0,
            }
        )
    by_cluster.sort(key=lambda r: (-(r["external_out"] + r["external_in"]), r["id"]))
    worst_pairs = []
    for (a, b), w in sorted(pair_edges.items(), key=lambda kv: (-kv[1], (kv[0][0], kv[0][1]))):
        top = sorted(pair_files[(a, b)].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        worst_pairs.append(
            {
                "a_id": a,
                "b_id": b,
                "a": by_id[a].get("label", str(a)),
                "b": by_id[b].get("label", str(b)),
                "edges": w,
                "top_files": [{"pair": p, "w": wt} for p, wt in top],
            }
        )
        if len(worst_pairs) >= 10:
            break
    return {
        "clusters": len(cs),
        "internal_edges": int_total,
        "external_edges": ext_total,
        "external_ratio": round(ext_total / max(ext_total + int_total, 1), 3),
        "unclustered_endpoint_edges": unclustered,
        "by_cluster": by_cluster,
        "worst_pairs": worst_pairs,
    }


def fmt_crosstalk(rep: dict, align: bool = False) -> str:
    """Render a crosstalk() report for humans — the ONE formatter shared
    by the MCP `crosstalk` tool and the nav CLI verb (align=True pads
    columns for terminal reading). Machines consume the rep dict."""
    lines = [
        f"crosstalk: {rep['clusters']} clusters, "
        f"internal {rep['internal_edges']} edges, "
        f"cross-cluster {rep['external_edges']} "
        f"({rep['external_ratio'] * 100:.1f}% of clustered)"
    ]
    if rep["unclustered_endpoint_edges"]:
        lines.append(
            f"  ({rep['unclustered_endpoint_edges']} edges touch unclustered files)"
        )
    lines += ["", "per cluster (top 10 by external):"]
    for r in rep["by_cluster"][:10]:
        if align:
            lines.append(
                f"  [{r['id']:>2}] {r['label'][:34]:<34} n={r['size']:<3}"
                f" internal {r['internal']:<4} out {r['external_out']:<4}"
                f" in {r['external_in']:<4} ext {r['external_share'] * 100:.0f}%"
            )
        else:
            lines.append(
                f"  [{r['id']:>2}] {r['label'][:34]}  n={r['size']}  "
                f"internal {r['internal']}  out {r['external_out']}  "
                f"in {r['external_in']}  ext {r['external_share'] * 100:.0f}%"
            )
    if rep["worst_pairs"]:
        lines += ["", "worst pairs:"]
        for wp in rep["worst_pairs"]:
            tops = ", ".join(f"{t['pair']} x{t['w']}" for t in wp["top_files"])
            lines.append(f"  {wp['a']} <-> {wp['b']}: {wp['edges']} edges (top: {tops})")
    return "\n".join(lines)
