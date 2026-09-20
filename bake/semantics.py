# bake/semantics — pure per-job transforms for viz._build_data (issue #86
# phase-2 V8; #299 B moved the remaining semantic stages here). Data
# arrives as arguments — the emb triple (emb_idx, embs, emb_paths) comes
# from bake.embeddings._fetch_embeddings, and emb_paths is the ONE
# derivation of the store-index space (#299 C): sims' a/b indices are
# emb_paths-LOCAL and every node mapping goes through it.
import sys


# #279: rendered-affinity ink budget. The layout keeps riding ALL J9 pairs
# (the springs want the full field); only the INK channel is capped, like
# the wire/highway budgets. Hundreds, not thousands.
SEM_AFF_CAP = 220


def _cluster_matrix(nodes, emb):
    """J11: cluster-centroid cosine matrix from the bake's one embedding
    fetch — independent of the kNN stage's failures (the monolith's
    second fetch was equally independent). Degrades to ([], None) only
    when the fetch did (missing store); a matrix-stage failure raises
    job-named (#367 loud-failures)."""
    # cluster-level semantic sims for the layout: cosine between cluster
    # embedding centroids (mean of member embeddings). Drives cluster
    # springs + repulsion caps so semantically related clusters (VFX
    # family) sit as neighbors in the galaxy.
    ckeys: list = []
    cmat = None
    if emb is None:
        return ckeys, cmat
    _, embs, emb_paths = emb
    try:
        import numpy as cnp

        p2c = {nd["path"]: nd["cluster"] for nd in nodes}
        groups: dict = {}
        for r_i, p in enumerate(emb_paths):
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
    except Exception as e:
        raise RuntimeError(
            f"cluster matrix failed mid-bake (issue #367 loud-failures): "
            f"{len(nodes)} nodes: {e!r}"
        ) from e
    return ckeys, cmat


def _sem_aff(emb, sims, idx):
    """#279: semantic-affinity overlay rows — the J9 kNN pairs promoted
    from layout-only springs to also-rendered ink. Same pairs, same ONE
    embedding fetch, no second bake job (grounding: issue #279 comment).
    Rows are [i, j, sim] NODE indices (i < j), ranked by (-sim, path_i,
    path_j) and cut at SEM_AFF_CAP so the served ink is deterministic.
    Returns (rows, dropped)."""
    if emb is None or not sims:
        return [], 0
    emb_paths = emb[2]
    path_of = {v: k for k, v in idx.items()}
    rows = [
        (idx[emb_paths[a]], idx[emb_paths[b]], s) for a, b, s in sims
    ]
    rows.sort(key=lambda r: (-r[2], path_of[r[0]], path_of[r[1]]))
    dropped = max(0, len(rows) - SEM_AFF_CAP)
    return [[a, b, s] for a, b, s in rows[:SEM_AFF_CAP]], dropped


def _supergroups(clusters, emb):
    """J10: coarse supergroups over the fine clusters, from the kNN
    stage's embeddings. Degrades to ([], []) when the fetch did (missing
    store) or scipy/coarse_groups is unavailable — the one sanctioned
    optional-dep degrade, marked on stderr (#367); vacuous — no
    degrade, nothing to link — under 2 fine clusters (scipy linkage
    cannot run on a single observation; the monolith's broad except
    silently covered this shape too). Every other failure raises
    job-named."""
    cid_gid: dict[int, int] = {}   # fine cluster id -> supergroup id
    groups2: list[dict] = []
    if emb is None:
        return cid_gid, groups2
    if len(clusters) < 2:
        return cid_gid, groups2
    _, embs, emb_paths = emb
    # two-level navigation: coarse supergroups over the fine
    # clusters (scipy average-linkage over embedding centroids,
    # clusters.coarse_groups). Optional UI level — degrades to []
    # when scipy/embeddings are unavailable.
    try:
        from clusters import coarse_groups

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
    except ImportError as e:
        # the sanctioned optional-dep degrade: scipy missing inside
        # coarse_groups (clusters itself is long imported by this point
        # — viz._build_data ran navstore.clusters() and _knn_sims'
        # topk_desc import first). Marked on stderr, never silent (#367).
        print(
            "neuronav: bake supergroups degraded — scipy/coarse_groups "
            f"unavailable ({e}); baking without the coarse level",
            file=sys.stderr,
        )
        return {}, []
    except Exception as e:
        raise RuntimeError(
            f"supergroups failed mid-bake (issue #367 loud-failures): "
            f"{len(clusters)} clusters: {e!r}"
        ) from e
    return cid_gid, groups2
