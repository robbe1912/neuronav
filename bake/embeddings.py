# bake/embeddings — the store-edge transforms for viz._build_data
# (issue #86 phase-2 V8; #299 B moved them out of viz.py). The ONE chroma
# fetch per bake lives here; every downstream stage (kNN sims, semAff,
# supergroups, cluster matrix) receives the fetch results as arguments
# and must take the store-index space from them — see _store_paths.

import nav


def _store_paths(paths, emb_idx):
    """#299 C: THE single derivation of the store-index space — walk
    order filtered to embedded paths. sims' a/b indices are
    emb_paths-LOCAL: node mapping goes only through this expression or
    the emb_paths element _fetch_embeddings returns. A second
    derivation anywhere is the silent wrong-wire drift class (#64) —
    test_bakeint's #299 leg counts occurrences in the bake tree."""
    return [p for p in paths if p in emb_idx]


def _fetch_embeddings(paths):
    """The ONE chroma embedding fetch per bake (D4/V7): the two original
    fetch sites issued byte-identical calls, so hoisting the fetch is
    semantic-preserving. Returns (emb_idx, normalized float32 rows,
    emb_paths) for the indexed paths — emb_paths is the #299 C shared
    index space — or None when the store is missing/empty."""
    try:
        import numpy as np

        col = nav._collection()
        if col.count():
            got = nav.col_get_all(col, ["embeddings"], "bake embeddings")
            emb_idx = {rid: i for i, rid in enumerate(got["ids"])}
            emb_paths = _store_paths(paths, emb_idx)
            rows = [emb_idx[p] for p in emb_paths]
            embs = np.array(
                [got["embeddings"][r] for r in rows], dtype=np.float32
            )
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            embs /= norms
            return emb_idx, embs, emb_paths
    except Exception:
        return None
    return None


def _knn_sims(emb):
    """J9: semantic kNN pairs from the bake's one embedding fetch.
    Returns (sims, emb) — the emb passthrough lets supergroups degrade
    exactly when the kNN stage failed (None also when the fetch did),
    mirroring the monolith's shared outer try."""
    # semantic kNN pairs from nav's embedding store — layout-only forces,
    # never rendered as edges: mutual top-6 neighbours with cosine >= 0.45
    # (mutual links resist transitive chaining, mirroring nav.clusters()).
    # Degrades to [] if the chroma store is missing/empty.
    sims: list[list] = []
    if emb is None:
        return sims, None
    _, embs, emb_paths = emb
    try:
        import numpy as np

        sim = embs @ embs.T
        np.fill_diagonal(sim, -1.0)
        from clusters import topk_desc

        knn = topk_desc(sim, 6)
        for a in range(len(emb_paths)):
            for b in knn[a]:
                b = int(b)
                if a < b and a in knn[b] and sim[a, b] >= 0.45:
                    sims.append([a, b, round(float(sim[a, b]), 4)])
    except Exception:
        sims = []
        return sims, None
    return sims, emb
