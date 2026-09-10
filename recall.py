"""recall: hybrid recall — BM25F lexical ranks fused with vector ranks.

Pure stdlib on the lexical side, no embedding backend dependency:

- ``BM25F`` — field-weighted lexical scoring (k1=1.2, b=0.75) over the
  structural graph's file/symbol data. Fields: filename x5, class_name
  x5, symbols x3, path x2, body x1 (body = parsed fn bodies, full
  length — no embed truncation; module-level code and scene XML are
  covered by the structured fields instead).
- reciprocal-rank fusion (k=60) of chroma vector ranks + BM25 ranks.
- bidirectional 1-hop expansion: each hit carries up to 3 context
  labels — its strongest-wired graph neighbors, never itself.

``search`` is the entry point ``nav.search`` delegates to. When the
vector side is unavailable (empty collection or embed backend down)
recall degrades to BM25F-only LOUDLY: one stderr line plus
``degraded: True`` on every hit — never a silent fallback.

nav and graph are imported lazily inside the functions that need them:
``nav.py --config <profile> search`` rebinds the active config only
after this module has loaded, so a module-level binding would freeze
the default profile (and, run as a script, dual-bind nav).
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter

RRF_K = 60.0
BM25_K1 = 1.2
BM25_B = 0.75
CTX_CAP = 3

# (field, weight) — order aligned with BM25F._field_texts
FIELDS: tuple[tuple[str, float], ...] = (
    ("filename", 5.0),
    ("class_name", 5.0),
    ("symbols", 3.0),
    ("path", 2.0),
    ("body", 1.0),
)

_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
# BM25F -> BM, 25, F / parseGd -> parse, Gd / XMLReader -> XML, Reader
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")


def _tokens(text: str) -> list[str]:
    """Deterministic token stream: split on non-alphanumerics, split
    camel humps, lowercase, drop single characters."""
    out: list[str] = []
    for chunk in _SPLIT_RE.split(text):
        if not chunk:
            continue
        for tok in _CAMEL_RE.findall(chunk):
            if len(tok) > 1:
                out.append(tok.lower())
    return out


class BM25F:
    """Field-weighted BM25 over the graph's FileSym corpus, backed by a
    prebuilt inverted index (term -> {path: weighted tf}).

    Weighted term frequency w_tf = sum_f weight_f * tf_f; document
    length = sum_f weight_f * len_f (tokens); Lucene-style idf
    ln(1 + (N - df + 0.5) / (df + 0.5)) — always positive. The index is
    built once per corpus (O(corpus) tokenization, amortized at rescan
    time); each query then costs O(query tokens x postings touched)
    instead of a full corpus re-tokenization — at engine scale (~6k
    files, ~12M tokens) that is the difference between interactive
    search and ~15 s/query. Build iterates sorted paths and counted
    structures only; scoring accumulates per-doc terms in query order,
    so scores are bit-stable for the same corpus bytes (verified
    bit-exact against the pre-index linear-scan implementation).
    """

    def __init__(self, files: dict) -> None:
        self._n = 0
        # term -> {path: w_tf} — postings; admits a doc iff it holds
        # >= 1 query term, replacing the old per-doc tfs scan
        self._postings: dict[str, dict[str, float]] = {}
        self._wlens: dict[str, float] = {}
        self._df: Counter[str] = Counter()
        total = 0.0
        for path in sorted(files):
            tfs: dict[str, float] = {}
            wlen = 0.0
            for text, (_fname, weight) in zip(self._field_texts(files[path]), FIELDS):
                toks = _tokens(text)
                wlen += weight * len(toks)
                for term, cnt in Counter(toks).items():
                    tfs[term] = tfs.get(term, 0.0) + weight * cnt
            if wlen <= 0.0 or not tfs:
                continue  # nothing indexable in this file
            self._n += 1
            self._wlens[path] = wlen
            for term, wtf in tfs.items():
                self._postings.setdefault(term, {})[path] = wtf
            self._df.update(tfs.keys())
            total += wlen
        self._avg = total / self._n if self._n else 0.0

    @staticmethod
    def _field_texts(fs) -> list[str]:
        """Per-field text, order aligned with FIELDS."""
        base = fs.path.rsplit("/", 1)[-1]  # filename with extension
        symbols = (
            sorted(fs.funcs)
            + sorted(fs.signals)
            + sorted(fs.members)
            + sorted(fs.consts)
        )
        body = "\n".join(fs.funcs[n].body for n in sorted(fs.funcs))
        return [base, fs.class_name, " ".join(symbols), fs.path, body]

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def scores(self, query: str) -> list[tuple[str, float]]:
        """(path, score) for every doc matching >= 1 query token, best
        first, ties by path. Empty when the query has no indexable
        tokens or the corpus is empty."""
        qtoks = _tokens(query)
        if not qtoks or not self._n:
            return []
        cand: set[str] = set()
        for t in frozenset(qtoks):
            posting = self._postings.get(t)
            if posting:
                cand.update(posting)
        out: list[tuple[str, float]] = []
        for path in sorted(cand):
            norm = BM25_K1 * (1.0 - BM25_B + BM25_B * self._wlens[path] / self._avg)
            s = 0.0
            for t in qtoks:  # multiplicity counts: repeated terms weigh more
                posting = self._postings.get(t)
                if posting is None:
                    continue
                tfw = posting.get(path)
                if tfw is None:
                    continue
                s += self._idf(t) * tfw * (BM25_K1 + 1.0) / (tfw + norm)
            if s > 0.0:
                out.append((path, s))
        out.sort(key=lambda kv: (-kv[1], kv[0]))
        return out


# One index per corpus, not per query: search() used to construct
# BM25F(g.files) inline, re-tokenizing the whole corpus on every call.
# The fingerprint (files-dict identity + sorted path set) catches graph
# rebuilds (get_graph(rebuild=True) yields a fresh dict) and path
# add/remove; FileSym contents are frozen once Graph.build() returns, so
# identity per corpus is sound. Pure perf — scores() output is
# bit-identical to a fresh BM25F over the same files.
_index_cache: tuple[tuple, BM25F] | None = None


def _cached_index(files: dict) -> BM25F:
    global _index_cache
    fp = (id(files), tuple(sorted(files)))
    if _index_cache is None or _index_cache[0] != fp:
        _index_cache = (fp, BM25F(files))
    return _index_cache[1]


def _vector_ranks(query: str, depth: int) -> tuple[list[str], dict[str, dict]]:
    """Chroma whole-file ranks (ids in rank order) + per-file metadata.
    Returns ([], {}) when the collection is empty; raises on backend
    failure so the caller can degrade loudly."""
    import nav  # lazy: `nav.py --config` rebinds after this module loads

    col = nav._collection()
    count = col.count()
    if count == 0:
        return [], {}
    vector = nav.embed([query])[0]
    got = col.query(
        query_embeddings=[vector],
        n_results=min(depth, count),
        include=["metadatas"],
    )
    ids = list(got["ids"][0])
    metas = {fid: (m or {}) for fid, m in zip(ids, got["metadatas"][0])}
    return ids, metas


def _fuse(
    vec: list[str], lex: list[str], w_vec: float = 1.0, w_lex: float = 1.0
) -> list[tuple[str, float, str]]:
    """Reciprocal-rank fusion (k=60) with src tagging and optional
    per-list weights (unweighted = the literal contract form). Sorted
    by (-score, path) — byte-stable for identical rank lists."""
    score: dict[str, float] = {}
    for ranks, w in ((vec, w_vec), (lex, w_lex)):
        for i, doc in enumerate(ranks):
            score[doc] = score.get(doc, 0.0) + w / (RRF_K + 1.0 + i)
    in_vec = frozenset(vec)
    in_lex = frozenset(lex)
    fused = [
        (
            doc,
            round(s, 6),
            "both" if doc in in_vec and doc in in_lex
            else ("vec" if doc in in_vec else "bm25"),
        )
        for doc, s in score.items()
    ]
    fused.sort(key=lambda t: (-t[1], t[0]))
    return fused


def _file_adjacency(g) -> dict[str, dict[str, int]]:
    """File-level wire counts derived from the fn-level edge sets: edge
    a::f -> b::g is one wire between a and b. Bidirectional by
    construction (an edge makes each file a neighbor of the other)."""
    adj: dict[str, dict[str, int]] = {}
    for src_key, dsts in g.edges.items():
        src = src_key.split("::", 1)[0]
        row = adj.setdefault(src, {})
        for dst_key in dsts:
            dst = dst_key.split("::", 1)[0]
            if dst == src:
                continue
            row[dst] = row.get(dst, 0) + 1
    return adj


def hop_context(files: list[str], g, cap: int = CTX_CAP) -> dict[str, list[str]]:
    """Up to ``cap`` bidirectional 1-hop context labels per file:
    strongest-wired neighbors first (ties by path), the file itself
    excluded."""
    adj = _file_adjacency(g)
    out: dict[str, list[str]] = {}
    for f in files:
        row = adj.get(f, {})
        out[f] = [
            p for p, _w in sorted(row.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]
        ]
    return out


def search(
    query: str,
    k: int = 12,
    bm25: bool = True,
    expand: bool = True,
    weights: tuple[float, float] | None = None,
) -> list[dict[str, object]]:
    """Hybrid recall: chroma vector ranks fused with BM25F lexical
    ranks, each hit carrying 1-hop graph context labels. ``bm25`` /
    ``expand`` are the bench switches (False, False = the pure-vector
    baseline behavior). ``weights`` = (vec, bm25) list weights for
    fusion arbitration; None keeps the pinned unweighted RRF k=60."""
    k = max(1, min(k, 50))
    w_vec, w_lex = weights if weights is not None else (1.0, 1.0)
    depth = max(16, 4 * k)

    vec: list[str] = []
    metas: dict[str, dict] = {}
    reason: str | None = None
    try:
        vec, metas = _vector_ranks(query, depth)
        if not vec:
            reason = "vector index is empty (call rescan first)"
    except Exception as exc:  # backend down = degraded, never a crash
        reason = f"embedding backend unreachable ({type(exc).__name__})"
    if reason:
        print(
            f"recall: vector recall unavailable — {reason}; serving BM25F-only",
            file=sys.stderr,
        )

    g = None
    lex: list[str] = []
    if bm25 or expand:
        import graph  # lazy: binding only, attrs read at call time

        g = graph.get_graph()
        if bm25:
            lex = [p for p, _s in _cached_index(g.files).scores(query)[:depth]]

    hits: list[dict[str, object]] = []
    for f, s, src in _fuse(vec, lex, w_vec, w_lex)[:k]:
        meta = metas.get(f, {})
        fs = g.files.get(f) if g is not None else None
        hit: dict[str, object] = {
            "file": f,
            "score": s,
            "src": src,
            "ctx": [],
        }
        if fs is not None:
            hit["class_name"] = fs.class_name
            hit["extends"] = fs.extends
            hit["ext"] = fs.ext
        else:
            hit["class_name"] = str(meta.get("class_name", ""))
            hit["extends"] = str(meta.get("extends", ""))
            hit["ext"] = str(meta.get("ext", ""))
        if reason:
            hit["degraded"] = True
        hits.append(hit)

    if expand and g is not None and hits:
        ctx = hop_context([str(h["file"]) for h in hits], g)
        for h in hits:
            h["ctx"] = ctx.get(str(h["file"]), [])
    return hits
