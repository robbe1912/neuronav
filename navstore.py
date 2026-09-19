"""navstore — the store/embed leaf of the old nav.py monolith (issue #344).

Chroma collection management (acquire/stamp/heal, issue #103; bounded
reads, #327) plus the pluggable embed client (ollama/openai wires,
issue #17; 429 backoff) and the store-side queries (hybrid recall via
recall.py, Louvain clusters via clusters.py).

SPLIT LAWS (issue #344):
- every config-derived global is REBINDABLE (navconfig._apply_config /
  config_scope) — this module reads them as ``navconfig.X`` attributes,
  never from-imports, so a scoped second config is always honored;
- cross-leaf calls are attribute calls too (``navstore.embed`` from
  navindex), so in-process test patching of this module's names keeps
  working exactly as ``nav.embed`` did;
- the process-global singletons live HERE and only here: one FileLock
  per store (_LOCKS), one PersistentClient per store (_CLIENTS), one
  cluster memo per (store, args) — no leaf re-instantiates another's
  cache, or the store races the locks exist to prevent reopen.
"""
from __future__ import annotations

import os
import random
import re
import sys
import time

import chromadb
from filelock import FileLock
import httpx

import navconfig
# hybrid recall (BM25F + reciprocal-rank fusion + 1-hop context). Module-
# level so the python extractor's import liveness keeps recall.py's funcs
# alive in the self-index; recall.py binds the nav leaves lazily inside
# its functions, so ``nav.py --config <profile> ...`` still switches
# profiles.
import recall

MAX_EMBED_CHARS = 30_000  # keep under Ollama context; head of .tscn has script links
EMBED_BATCH = 32
UPSERT_BATCH = 64  # shared with navindex's rescan/import batching


def _embed_post(chunk: list[str], headers: dict[str, str] | None) -> dict:
    """One embeddings POST with 429 backoff (issue #17): a parseable
    Retry-After is honored (capped at 60s), no header means 1s/2s/4s.
    Any other failure raises loud via raise_for_status."""
    attempt = 0
    while True:
        resp = httpx.post(
            navconfig.EMBED_URL,
            json={"model": navconfig.EMBED_MODEL, "input": chunk},
            headers=headers,
            timeout=300.0,
        )
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        if attempt >= 3:
            raise RuntimeError(
                f"{navconfig.EMBED_PROVIDER} embed still rate-limited (429) "
                f"after {attempt + 1} attempts: {resp.text[:200]}"
            )
        raw = resp.headers.get("Retry-After")
        if raw is None:
            time.sleep(float(2**attempt))
        else:
            try:
                time.sleep(max(min(float(raw), 60.0), 0.0))
            except ValueError:
                time.sleep(float(2**attempt))  # Retry-After as HTTP-date
        attempt += 1


def _fake_embeds() -> bool:
    """#298: =0 means OFF (unset/""/"0" falsy); only a non-"0" value swaps
    in the deterministic hash embeddings. Four sites used to read the env
    raw, so NEURONAV_EMBED_FAKE=0 silently faked every embed."""
    v = os.environ.get("NEURONAV_EMBED_FAKE")
    return v is not None and v != "" and v != "0"


def embed(texts: list[str]) -> list[list[float]]:
    """Batch-embed via the configured provider (issue #17): Ollama
    /api/embed or any OpenAI-compatible /embeddings endpoint. Truncates
    long inputs and chunks requests at EMBED_BATCH (OpenAI caps input
    array length).

    NEURONAV_EMBED_FAKE=1 swaps in deterministic hash embeddings (CI
    plumbing mode): same text -> same vector, so upsert/query/scoping all
    exercise for real while no model server is needed. NOT semantic -
    quality gates stay local with a real Ollama."""
    truncated = [t[:MAX_EMBED_CHARS] for t in texts]
    if _fake_embeds():
        out = []
        for t in truncated:
            rng = random.Random(f"neuronav-fake:{t}")
            out.append([rng.uniform(-1.0, 1.0) for _ in range(navconfig.EMBED_DIM)])
        return out
    headers = ({"Authorization": f"Bearer {navconfig.EMBED_API_KEY}"}
               if navconfig.EMBED_API_KEY else None)
    out: list[list[float]] = []
    for i in range(0, len(truncated), EMBED_BATCH):
        chunk = truncated[i : i + EMBED_BATCH]
        data = _embed_post(chunk, headers)
        if navconfig.EMBED_PROVIDER == "ollama":
            rows = data.get("embeddings")
        else:  # openai: data[i].embedding; row order is not guaranteed
            rows = [r.get("embedding") for r in sorted(data.get("data") or [], key=lambda r: r.get("index", 0))]
        if rows is None or any(r is None for r in rows):
            raise RuntimeError(f"{navconfig.EMBED_PROVIDER} embed failed: {data}")
        if len(rows) != len(chunk):
            raise RuntimeError(
                f"{navconfig.EMBED_PROVIDER} returned {len(rows)} embeddings for {len(chunk)} inputs"
            )
        out.extend(rows)
    return out

# Truthful degradation labels (issue #115): an embed-path failure that
# is really a model/config problem must say so — naming the model AND
# provider — instead of reading "backend unreachable" and sending the
# user to restart a server that is fine. Shared by recall.search, the
# server's find_functions fallback, explore's degraded notes and
# _ctx_semantic, so every degraded surface tells the same truth.
_MODEL_ERR_SIG = re.compile(
    r"model[^\n]{0,120}?(?:not\s+found|not\s+supported|unknown|invalid|"
    r"no\s+such|does\s+not\s+exist|missing)"
    r"|(?:not\s+found|not\s+supported|unknown|invalid|no\s+such|"
    r"does\s+not\s+exist|missing)[^\n]{0,120}?model",
    re.I,
)


def embed_failure_reason(exc: Exception) -> str:
    """One-line truthful reason for an embed-path exception, for the
    degraded-mode markers. Classes, in order:

    - the _check_model abort (index built under a different model or
      provider) -> "embed model/config mismatch" carrying the original
      actionable message (it already names both models + providers and
      the drop+rescan fix);
    - HTTP 4xx, or 5xx whose body carries a model-mismatch signature
      (ollama/openai "model not found" etc.) -> names the configured
      model + provider and says model/config error, not connectivity;
    - any other HTTP status -> endpoint error (backend reachable);
    - everything else (connection refused/timeouts) -> the one case
      that legitimately reads "embedding backend unreachable".
    """
    msg = str(exc)
    if "index was built with embed model" in msg:
        return f"embed model/config mismatch — {msg}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        snippet = " ".join((exc.response.text or "").split())[:160]
        if code // 100 == 4 or _MODEL_ERR_SIG.search(snippet):
            out = (
                f"{navconfig.EMBED_PROVIDER} embed endpoint rejected model "
                f"'{navconfig.EMBED_MODEL}' (HTTP {code}"
            )
            if snippet:
                out += f": {snippet}"
            return out + ") — model/config error, not connectivity"
        out = f"{navconfig.EMBED_PROVIDER} embed endpoint error (HTTP {code}"
        if snippet:
            out += f": {snippet}"
        return out + ") — backend reachable, endpoint failing"
    return f"embedding backend unreachable ({type(exc).__name__})"


def _db_lock(timeout: float | None = None) -> "FileLock":
    """Advisory cross-process writer lock (server, CLI, viz all write via
    the nav leaves). One lock per store, cached (issue #131): the
    universal server alternates configs, so the lock must follow the
    store. ``timeout`` bounds the acquire (issue #203 boot hardening);
    None waits forever (the filelock default). Always assigned: the
    instance is cached per store, so a boot-bounded acquire must not
    leak its bound onto later default callers."""
    navconfig.DB_DIR.mkdir(parents=True, exist_ok=True)
    lock = _LOCKS.setdefault(str(navconfig.DB_DIR),
                             FileLock(str(navconfig.DB_DIR / ".write.lock")))
    lock.timeout = -1 if timeout is None else timeout
    return lock


_LOCKS: dict[str, "FileLock"] = {}


def _check_model(col: chromadb.Collection) -> chromadb.Collection:
    """Embedding fingerprint on the live collection — model AND provider
    (issue #17 stamped both): a same-name model behind a different
    provider is not guaranteed to be the same vector space, so a
    CHANGED key demands a re-embed; a re-stamp would copy
    wrong-provider vectors verbatim (CodeRabbit hardening on #151).
    A MISSING provider key is lineage, not drift (issue #159): pre-#17
    stores carry the model yet no provider, and the pre-#17 client
    spoke only the Ollama wire protocol — so the stamp heals to
    EMBED_PROVIDER via the re-stamp path when the config is ollama,
    while a provider-less store under any other config is genuine
    drift and still refuses. Mismatch messages print the raw stored
    provider (None reads as unstamped), never a fabricated default.
    Fully unstamped collections (both keys absent) take the copy
    path — nothing contradicts the config.

    The stamp must also keep hnsw:space=cosine (issue #103): chroma's
    modify() REPLACES the metadata dict and refuses hnsw:* keys outright
    ("changing the distance function ... is not supported", verified on
    the pinned 1.5.9), so a bare stamp both wipes the space key and
    cannot restore it — any later rebuild-from-metadata silently falls
    back to l2. A compatible collection missing only stamp/space keys
    is re-created with the full metadata, vectors copied (chroma's f32
    write quantization settles once, <= 1 ulp, then bit-stable),
    temp on the next call."""
    meta = col.metadata or {}
    stored = meta.get("embed_model")
    provider = meta.get("embed_provider")
    # absent provider = pre-#17 lineage: the only client that could
    # have built the store spoke ollama, so that is the effective stamp
    lineage = provider if provider is not None else "ollama"
    if stored is not None and (stored != navconfig.EMBED_MODEL
                               or lineage != navconfig.EMBED_PROVIDER):
        raise RuntimeError(
            f"index was built with embed model '{stored}' (provider "
            f"{provider!r}) but config says "
            f"'{navconfig.EMBED_MODEL}' (provider "
            f"'{navconfig.EMBED_PROVIDER}') — run "
            "`python nav.py drop` then rescan"
        )
    if (stored == navconfig.EMBED_MODEL and provider == navconfig.EMBED_PROVIDER
            and meta.get("hnsw:space") == "cosine"):
        return _adopt_orphan(col)
    return _restamp(col)


def _adopt_orphan(col: chromadb.Collection) -> chromadb.Collection:
    """Heal a re-stamp that crashed between dropping the source and
    renaming the temp in (CodeRabbit on #151): get_or_create has since
    re-made the name with correct metadata but partial data — it would
    pass _check_model and silently serve a truncated index. A strictly
    richer temp wins the name back; a poorer one is stale garbage from
    a mid-build crash. Double-checked under the write lock so a
    concurrent _restamp builder is never raced."""
    tmp_name = f"{col.name}-restamp"
    try:
        client().get_collection(tmp_name)
    except Exception:
        return col  # no temp: the common path, one cheap lookup
    with _db_lock():
        try:
            tmp = client().get_collection(tmp_name)
            if tmp.count() > col.count():
                n = tmp.count()
                client().delete_collection(col.name)
                tmp.modify(name=col.name)
                print(f"neuronav: adopted orphaned re-stamp temp for "
                      f"'{col.name}' ({n} vectors; a previous repair "
                      "crashed mid-swap)", file=sys.stderr)
                return client().get_collection(col.name)
            client().delete_collection(tmp_name)
        except Exception as e:
            raise RuntimeError(
                f"failed to settle re-stamp temp '{tmp_name}': {e}"
            ) from e
        return col


def _restamp(col: chromadb.Collection,
             embed_mode: str | None = None,
             doc_shape: str | None = None) -> chromadb.Collection:
    """Re-create `col` with the full metadata (issue #103) — the only
    write that keeps hnsw:space, since modify() replaces the dict and
    rejects hnsw:* keys. Build-and-validate before the swap (CodeRabbit
    on #151): the copy lands in a durable '<name>-restamp' temp and is
    count-checked, so an add() failure leaves the source untouched and
    never strands a truncated collection that still passes the checks;
    the cutover is delete + rename, and a crash in between is healed
    by _adopt_orphan from the temp (export_base's manifest-last law is
    the precedent). Vectors are provider output, model+provider-gated
    by _check_model; documents included. Chroma's f32 quantization
    settles once on copy (<= 1 ulp, then bit-stable). Under the
    write lock, reentrant from the export/import callers. ``embed_mode``
    stamps the new collection's vector-space lineage (#220); None (the
    default) preserves the stored key verbatim, and a store that never
    carried one stays unstamped — pre-#220 lineage is real. ``doc_shape``
    is the same law for the doc-construction lineage (#229)."""
    name = col.name
    tmp_name = f"{name}-restamp"
    with _db_lock():
        try:
            # issue #239 rider: retry the hnsw-settle transient so the
            # re-stamp heals instead of tripping the raced-collection
            # fallback on self-healing noise; real failures still fall
            data = col_get_all(
                col, ["embeddings", "documents", "metadatas"], "re-stamp read"
            )
        except Exception as e:
            print(f"neuronav: collection '{name}' needs a metadata re-stamp but "
                  f"reading its vectors failed ({e}); metadata left as-is",
                  file=sys.stderr)
            try:  # a concurrent process may have finished the re-stamp
                raced = client().get_collection(name)
            except Exception:
                raise RuntimeError(
                    f"collection '{name}' vanished during metadata re-stamp"
                ) from e
            m = raced.metadata or {}
            if (m.get("embed_model"), m.get("embed_provider"), m.get("hnsw:space")) != (
                navconfig.EMBED_MODEL, navconfig.EMBED_PROVIDER, "cosine"
            ):
                raise RuntimeError(
                    f"collection '{name}' carries foreign metadata after a "
                    f"re-stamp race (embed_model={m.get('embed_model')!r}, "
                    f"embed_provider={m.get('embed_provider')!r}, "
                    f"hnsw:space={m.get('hnsw:space')!r}) — run "
                    "`python nav.py drop` then rescan"
                ) from e
            return raced
        try:
            client().delete_collection(tmp_name)  # stale partial from an earlier crash
        except Exception:
            pass
        mode_key = ((col.metadata or {}).get("embed_mode") if embed_mode is None
                    else embed_mode)
        shape_key = ((col.metadata or {}).get("doc_shape") if doc_shape is None
                     else doc_shape)
        stamp = {"hnsw:space": "cosine", "embed_model": navconfig.EMBED_MODEL,
                 "embed_provider": navconfig.EMBED_PROVIDER}
        if mode_key is not None:
            stamp["embed_mode"] = mode_key
        if shape_key is not None:
            stamp["doc_shape"] = shape_key
        tmp = client().create_collection(name=tmp_name, metadata=stamp)
        try:
            for i in range(0, len(data["ids"]), UPSERT_BATCH):
                tmp.add(ids=data["ids"][i : i + UPSERT_BATCH],
                        embeddings=data["embeddings"][i : i + UPSERT_BATCH],
                        documents=data["documents"][i : i + UPSERT_BATCH],
                        metadatas=data["metadatas"][i : i + UPSERT_BATCH])
            if tmp.count() != len(data["ids"]):
                raise RuntimeError(
                    f"re-stamp copy of '{name}' landed {tmp.count()} of "
                    f"{len(data['ids'])} vectors — source untouched, retry"
                )
        except Exception:
            try:
                client().delete_collection(tmp_name)  # never leave a partial temp
            except Exception:
                pass
            raise
        client().delete_collection(name)
        tmp.modify(name=name)
    print(f"neuronav: re-stamped collection '{name}' with full metadata "
          f"(hnsw:space=cosine, #103): {len(data['ids'])} vectors copied",
          file=sys.stderr)
    return client().get_collection(name)


def client() -> "chromadb.PersistentClient":
    """Chroma client at the configured store — one per store for the
    process (issue #131): alternation must not churn handles (deletes
    were observed silently no-op-ing under client churn) and each open
    client holds sqlite resources in its store dir."""
    return _CLIENTS.setdefault(str(navconfig.DB_DIR),
                               chromadb.PersistentClient(path=str(navconfig.DB_DIR)))


_CLIENTS: dict[str, "chromadb.PersistentClient"] = {}


def fns_name() -> str:
    """Fn-level sibling collection name — '-fns' rides the main
    collection so per-config stores never mix function vectors."""
    return f"{navconfig.COLLECTION}-fns"


def _named_collection(name: str) -> chromadb.Collection:
    """Born-correct metadata — fresh stores never need a re-stamp; an
    existing collection keeps its stored metadata and _check_model
    heals stale or wiped stamps (#103). The born stamp records the
    embed mode (#220) and the doc-construction shape (#229) so a later
    rescan in the other mode — or under a different doc shaper —
    refuses to silently reuse the vectors."""
    col = client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine",
                  "embed_model": navconfig.EMBED_MODEL,
                  "embed_provider": navconfig.EMBED_PROVIDER,
                  "embed_mode": embed_mode(),
                  "doc_shape": doc_shape()},
    )
    return _check_model(col)


def _collection() -> chromadb.Collection:
    """Main file-level collection."""
    return _named_collection(navconfig.COLLECTION)


def fns_collection() -> chromadb.Collection:
    """Fn-level sibling (graph.sync_functions / find_functions)."""
    return _named_collection(fns_name())


def chroma_read(what: str, read):
    """Run a chroma read, retrying only the hnsw-settling transient
    (issue #239): right after embedding upserts — the boot rescan or a
    watcher tick — chroma's on-disk hnsw segment can lag the sqlite
    metadata for a moment under load, and a read then fails with
    "Error creating hnsw segment reader: Nothing found on disk" from
    the Rust executor. The segment settles by itself, so the read is
    retried on exactly that signature: a loud stderr note per retry;
    anything else — or exhaustion — raises unchanged. No silent
    degradation, no changed auto-rescan semantics."""
    for pause in _CHROMA_READ_PAUSES_S:
        try:
            return read()
        except Exception as exc:
            if _HNSW_SETTLING not in str(exc):
                raise
            print(
                f"neuronav: chroma read retry ({what}): hnsw segment still "
                f"settling after upserts — next try in {pause:g}s "
                f"({len(_CHROMA_READ_PAUSES_S)} retries max)",
                file=sys.stderr,
            )
            time.sleep(pause)
    return read()


_HNSW_SETTLING = "hnsw segment reader"
_CHROMA_READ_PAUSES_S = (0.5, 1.0, 2.0, 4.0)

GET_CHUNK = 512  # bounded reads (#327): safely under the ~999 SQL
# variable ceiling of old bundled sqlite builds and far under the
# ~32766 of modern ones — an unfiltered col.get() binds every row's
# columns at once and dies with InternalError "too many SQL variables"
# at monorepo scale


def col_get_all(col, include, what="chunked read"):
    """Full-collection read via bounded, deterministically-ordered pages
    (issue #327). One unfiltered col.get() trips the sqlite build's
    bound-variable ceiling on stores past it, so reads page through
    GET_CHUNK-sized chunks (each under the caller's store lock, each
    with the hnsw-settle retry) and merge sorted by id — the result,
    and every export or store-copy built from it, is a function of the
    data alone, not of chroma's internal row order. A row-count
    mismatch across pages is a loud error, never a silent short read."""
    total = col.count()
    rows: list[tuple] = []
    for off in range(0, total, GET_CHUNK):
        got = chroma_read(
            f"{what} (rows {off + 1}..{min(off + GET_CHUNK, total)})",
            lambda off=off: col.get(
                include=include, limit=GET_CHUNK, offset=off
            ),
        )
        rows.extend(zip(got["ids"], *(got[k] for k in include)))
    if len(rows) != total:
        raise RuntimeError(
            f"neuronav: {what} on '{col.name}' merged {len(rows)} of "
            f"{total} rows — the store changed mid-read despite the lock"
        )
    rows.sort(key=lambda r: r[0])
    out: dict[str, list] = {"ids": [r[0] for r in rows]}
    for pos, key in enumerate(include, start=1):
        out[key] = [r[pos] for r in rows]
    return out


def embed_mode() -> str:
    """Vector-space lineage of the current process (#220): "fake" under
    NEURONAV_EMBED_FAKE, else "real". Recorded next to embed_model in
    the collection stamp so a rescan in the OTHER mode force-re-embeds
    instead of silently reusing sha-gated vectors from the wrong space
    (the #219 rig failure: hash-embed bootstrap, real bench, cosine 0)."""
    return "fake" if _fake_embeds() else "real"


def doc_shape() -> str:
    """Doc-construction lineage of the current process (#229): "raw"
    when file-doc shaping is off, else "cast<rev>@<scale>" — the graph
    shaper revision plus the config scale. Stamped next to embed_mode
    (same #220 law, doc side): sha-gating skips re-embeds on unchanged
    BYTES, so a store whose vectors were built from the other doc shape
    must be re-embedded loudly, never silently reused."""
    if navconfig.FILE_DOC_CAST <= 0.0:
        return "raw"
    import graph  # lazy: the shaper revision lives with the shaper

    return f"cast{graph.FILE_DOC_REV}@{navconfig.FILE_DOC_CAST:g}"


def count() -> int:
    return _collection().count()


def search(
    query: str,
    k: int = 12,
    two_pass: bool = False,
    graph_boost: float | None = None,
) -> list[dict[str, object]]:
    """Hybrid recall: chroma vector ranks fused (reciprocal-rank fusion,
    k=30) with BM25F lexical ranks over the structural graph; each hit
    carries bidirectional 1-hop context labels.

    Hit keys: file, score, src ("vec"|"bm25"|"both"), ctx (<=3 neighbor
    paths) + class_name/extends/ext. two_pass/graph_boost pass straight
    through to recall.search (issues #74/#73/#228 — the server's
    semantic_search exposes them on the wire); graph_boost None rides
    the shipped recall default (λ 0.25 — the #228 grid winner), 0.0 is
    the explicit off wire. If the vector side is unavailable the results
    degrade LOUDLY to BM25F-only (stderr warning + ``degraded: True``
    on every hit) — see recall.search.
    """
    return recall.search(query, k=k, two_pass=two_pass, graph_boost=graph_boost)


# clusters() result cache (K2/#86, store-keyed for #131): same store +
# engine args + unchanged data -> the same list object, so 10 call-sites
# stop re-running Louvain; alternation cannot cross-wire stores because
# the store key rides every entry (see navconfig.store_key()).
_clusters_memo: dict[tuple, list] = {}


def _memo_drop_current() -> None:
    """Invalidate the ACTIVE store's cluster memos (K2/#86): rescan/
    import_base/drop changed one store, so only that store's entries go —
    alternation keeps the other stores' entries warm (issue #131)."""
    key = navconfig.store_key()
    for k in [k for k in _clusters_memo if k[:2] == key]:
        del _clusters_memo[k]


def clusters(
    k: int = 6,
    min_sim: float = 0.6,
    split_sim: float = 0.65,
    blob_min: int = 60,
) -> list[dict[str, object]]:
    """Subsystem clusters: Louvain community detection over a hybrid
    weighted graph — mutual-kNN embedding sims (weight = sim * 0.7) +
    structural edges from graph.py (call/signal capped 5 per file pair,
    attach/inst 1.5); tests/ files get their own community, loose files
    join the community dominating their dir seed (see
    clusters.communities_graph). Resolution swept {1.0: 49 clusters/
    largest 131, 1.2: 35/74, 1.5: 29/69, 1.8: 30/70} — the knob lives
    in clusters.communities_graph (default 1.0). Mega-blobs are then
    split + every cluster labeled (see clusters.finalize).
    Returns [{id, size, paths: [(path, class_name)], label, confidence,
    method}]. Memoized (K2/#86): callers share ONE list per (store, engine-args)
    tuple while that store is unchanged — rescan()/import_base()/`drop`
    drop the store's entries, and a profile switch cannot cross-wire
    stores because the store key rides every entry (issue #131). Treat
    the returned list as read-only."""
    memo_key = navconfig.store_key() + (k, min_sim, split_sim, blob_min)
    hit = _clusters_memo.get(memo_key)
    if hit is not None:
        return hit
    import numpy as np

    col = _collection()
    if col.count() == 0:
        return []
    got = chroma_read("clusters", lambda: col.get(include=["metadatas", "embeddings"]))
    # issue #118: chroma returns ids in insertion order — a function of
    # store HISTORY, not data (a fresh store and a grown one over the
    # same files disagree). Sort every column by id so union-find roots,
    rows = sorted(
        zip(
            got["ids"],
            got.get("embeddings") if got.get("embeddings") is not None else [],
            got.get("metadatas") or [],
        ),
        key=lambda r: r[0],
    )
    ids = [r[0] for r in rows]
    embs = [r[1] for r in rows]
    metas = [r[2] for r in rows]
    mat = np.array([e.tolist() if hasattr(e, "tolist") else e for e in embs], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat /= norms
    sim = mat @ mat.T
    np.fill_diagonal(sim, -1.0)
    import clusters as _clusters

    knn = _clusters.topk_desc(sim, k)

    # louvain hybrid: structural edges + embedding sims; adj feeds the
    # labeler's autoload hub gating, units keep scene+script welds
    # intact through finalize's embedding split passes
    out, adj, units = _clusters.communities_graph(
        ids, metas, mat, sim, knn, min_sim=min_sim
    )
    out.sort(key=lambda c: -int(c["size"]))
    for idx, c in enumerate(out):
        c["id"] = idx

    result = _clusters.finalize(
        out, ids, mat, split_sim=split_sim, blob_min=blob_min, adj=adj, units=units
    )
    _clusters_memo[memo_key] = result
    return result
