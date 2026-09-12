# bake/semantics — pure per-job transforms for viz._build_data (issue #86
# phase-2 V8). Moved verbatim from viz.py; every nav/graph/chroma edge
# stays in the viz.py orchestrator — data arrives as arguments.

def _cluster_matrix(paths, nodes, emb):
    """J11: cluster-centroid cosine matrix from the bake's one embedding
    fetch — independent try, so a kNN-stage failure never degrades cmat
    (the monolith's second fetch was equally independent)."""
    # cluster-level semantic sims for the layout: cosine between cluster
    # embedding centroids (mean of member embeddings). Drives cluster
    # springs + repulsion caps so semantically related clusters (VFX
    # family) sit as neighbors in the galaxy. Degrades to None.
    ckeys: list = []
    cmat = None
    if emb is None:
        return ckeys, cmat
    emb_idx, embs = emb
    try:
        import numpy as cnp

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
    return ckeys, cmat
