# bake/embeddings — the store-edge transforms for viz._build_data
# (issue #86 phase-2 V8; #299 B moved them out of viz.py). The ONE chroma
# fetch per bake lives here; every downstream stage (kNN sims, semAff,
# supergroups, cluster matrix) receives the fetch results as arguments
# and must take the store-index space from them — see _store_paths.
import navconfig
import navstore


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
    index space — or None when the store is missing/empty (count()==0,
    the one sanctioned degrade: a missing dir materializes as an empty
    get_or_create'd collection, never as an exception). Any exception
    in here is a real failure — chroma internal error, schema drift,
    numpy — and raises job-named (#367): the #64 guard owns the
    empty-store shape, so a swallowed one could only ship a
    silently-thinner graph."""
    import numpy as np

    col = navstore._collection()
    if not col.count():
        return None
    try:
        got = navstore.col_get_all(col, ["embeddings"], "bake embeddings")
        emb_idx = {rid: i for i, rid in enumerate(got["ids"])}
        emb_paths = _store_paths(paths, emb_idx)
        rows = [emb_idx[p] for p in emb_paths]
        embs = np.array(
            [got["embeddings"][r] for r in rows], dtype=np.float32
        )
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        embs /= norms
    except Exception as e:
        raise RuntimeError(
            f"embeddings fetch failed mid-bake (issue #367 "
            f"loud-failures): collection '{navconfig.COLLECTION}' in "
            f"{navconfig.DB_DIR} ({col.count()} vectors): {e!r}"
        ) from e
    return emb_idx, embs, emb_paths


def _knn_sims(emb):
    """J9: semantic kNN pairs from the bake's one embedding fetch.
    Returns (sims, emb) — ([], None) only when the fetch degraded
    (missing store); the emb passthrough threads that same degrade to
    supergroups. A kNN-stage failure raises job-named (#367): a
    swallowed one silently thinned the layout's semantic springs."""
    # semantic kNN pairs from nav's embedding store — layout-only forces,
    # never rendered as edges: mutual top-6 neighbours with cosine >= 0.45
    # (mutual links resist transitive chaining, mirroring navstore.clusters()).
    # Degrades to [] only if the chroma store is missing/empty.
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
    except Exception as e:
        raise RuntimeError(
            f"semantic kNN failed mid-bake (issue #367 loud-failures): "
            f"{len(emb_paths)} embedded paths: {e!r}"
        ) from e
    return sims, emb
