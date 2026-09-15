"""recall: hybrid recall — BM25F lexical ranks fused with vector ranks.

Pure stdlib on the lexical side, no embedding backend dependency:

- ``BM25F`` — field-weighted lexical scoring (k1=1.2, b=0.75) over the
  structural graph's file/symbol data. Fields: filename x5, class_name
  x5, symbols x3, path x2, body x1 (body = parsed fn bodies, full
  length — no embed truncation; module-level code and scene XML are
  covered by the structured fields instead).
- reciprocal-rank fusion (k=30) of chroma vector ranks + BM25 ranks.
- post-fusion graph-neighbor boost (issues #73/#228, default ON,
  ``GRAPH_BOOST``): the fused top-k each promote their 1-hop wire
  neighbors by a rank-decayed λ·RRF-unit bump — the λ × RRF-k ×
  weights grid that chose the default lives in bench/RESULTS.md.
- task-instruction query prefix (issue #217, default ON): the
  winning ``QUERY_PREFIX`` from the #214/#75 embedding A/B — the
  nl2code instruction from the JCE model card — is prepended to the
  EMBEDDED query only (semantic pass 1 and the two-pass augmented
  retrieve). Query-side only: stores stay compatible (no re-index)
  and the lexical side keeps the raw query.
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
import weakref

RRF_K = 30.0
BM25_K1 = 1.2
BM25_B = 0.75
CTX_CAP = 3
# 1-hop graph-neighbor rank boost, default ON (issues #73/#228). λ is a
# multiplier of the RRF unit 1/(rrf_k+1): each fused top-k source adds
# λ·unit/(source rank) to every distinct 1-hop file neighbor. λ 0.25 @
# rrf_k 30 is the #228 grid winner (hit@5 +0.08, MRR +0.075 over the
# qprefix baseline, double-run stable — bench/RESULTS.md); 0.0 is the
# explicit off wire.
GRAPH_BOOST = 0.25
# char budget for the two-pass augmented query's harvested identifier
# tail — sized so the original query stays dominant (issue #74 A/B)
TWO_PASS_BUDGET = 320
# two-pass tuning knobs (issue #228, census exp 2): pool = how many
# pass-1 lexical hits donate identifiers (RepoBench ICLR 2024:
# retrieval accuracy degrades monotonically as kept context grows — a
# small pool may beat the deep 12-file harvest); weight = pass-2 RRF
# side weight scale (pass-2 ranks ride a noisier augmented query, so
# a discount may pay); imports = harvest imported symbol names too
# (from_imports — the y in `from x import y`, what graph.py already
# exposes). Defaults pin the #228 sweep winner (bench/RESULTS.md);
# the dict form of search(two_pass={...}) overrides per call for
# bench legs — True is exactly these defaults.
TWO_PASS_POOL = 12
TWO_PASS_WEIGHT = 1.0
TWO_PASS_IMPORTS = False
# nl2code task-instruction query prefix (issues #75/#217): the winning
# `qprefix` A/B leg text (JCE model card, arXiv 2508.21290), shipped
# default-on. Prepended to the EMBEDDED query only, so the store stays
# compatible (query-side transform, no re-index) and BM25F never sees
# the instruction tokens. Deterministic constant — no clock/env input.
QUERY_PREFIX = "Find the most relevant code snippet given the following query:\n"

# (field, weight) — order aligned with BM25F._field_texts
FIELDS: tuple[tuple[str, float], ...] = (
    ("filename", 5.0),
    ("class_name", 5.0),
    ("symbols", 3.0),
    ("path", 2.0),
    ("body", 1.0),
)

_SPLIT_RE = re.compile(r"[^\w]+")  # Unicode-aware (issue #115): non-ASCII
# identifiers and CJK runs survive as tokens instead of silently
# dropping to (near-)zero — ASCII-only input splits exactly as before.
# BM25F -> BM, 25, F / parseGd -> parse, Gd / XMLReader -> XML, Reader
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")


def _tokens(text: str) -> list[str]:
    """Deterministic token stream: split on non-word characters, split
    camel humps on the ASCII segments, lowercase, drop single
    characters. A chunk containing non-ASCII is ALSO kept whole as its
    own token (issue #115) while still yielding the ASCII camel tokens
    inside it, so 'über' indexes as BOTH 'über' and 'ber', and pure-CJK
    identifiers stop tokenizing to []."""
    out: list[str] = []
    for chunk in _SPLIT_RE.split(text):
        if not chunk:
            continue
        if chunk.isascii():
            toks = _CAMEL_RE.findall(chunk)
        else:
            toks = _CAMEL_RE.findall(chunk) + [chunk]
        for tok in toks:
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
# The cache holds the files dict ITSELF (plus its sorted path set) and
# matches by identity: the strong reference means the cached dict can
# never be freed, so its address can never be recycled onto a newer
# dict while the entry lives (issue #115 — the old id()-only
# fingerprint survived a free/realloc cycle, and two consecutive graph
# rebuilds with no query in between routinely landed the third files
# dict on the first one's address, serving the pre-rescan tokenization
# with no warning). A different dict — graph rebuild, config-scope
# swap — misses the identity check and rebuilds. FileSym contents are
# frozen once Graph.build() returns. Pure perf — scores() output is
# bit-identical to a fresh BM25F over the same files.
_index_cache: tuple[dict, tuple, BM25F] | None = None


def _cached_index(files: dict) -> BM25F:
    global _index_cache
    paths = tuple(sorted(files))
    if _index_cache is None or _index_cache[0] is not files or _index_cache[1] != paths:
        _index_cache = (files, paths, BM25F(files))
    return _index_cache[2]


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


def _rrf(
    sides: list[tuple[str, list[str], float]], rrf_k: float = RRF_K
) -> list[tuple[str, float, str]]:
    """Reciprocal-rank fusion (default k=60) over (tag, rank-list,
    weight) sides, tag in {"vec", "bm25"}: a doc any vec side found is
    "vec", any bm25 side "bm25", both "both". Sorted by (-score, path)
    — byte-stable for identical rank lists."""
    score: dict[str, float] = {}
    found: dict[str, set[str]] = {"vec": set(), "bm25": set()}
    for tag, ranks, w in sides:
        for i, doc in enumerate(ranks):
            score[doc] = score.get(doc, 0.0) + w / (rrf_k + 1.0 + i)
            found[tag].add(doc)
    fused = [
        (
            doc,
            round(s, 6),
            "both" if doc in found["vec"] and doc in found["bm25"]
            else ("vec" if doc in found["vec"] else "bm25"),
        )
        for doc, s in score.items()
    ]
    fused.sort(key=lambda t: (-t[1], t[0]))
    return fused


def _graph_boost(
    fused: list[tuple[str, float, str]],
    g,
    k: int,
    lam: float,
    rrf_k: float,
) -> list[tuple[str, float, str]]:
    """Post-fusion 1-hop neighbor promotion (issue #73): each of the
    fused top-k sources, in fused order, adds one rank-decayed bump
    lam/(rrf_k+1)/(source rank) to every distinct 1-hop file neighbor —
    a file wired to several top hits accumulates their consensus.
    Neighbors absent from the fused lists enter with src "graph".
    Iteration is fused order, then (-wires, path) per source, so float
    accumulation order is fixed; the re-sort by (-score, path) makes
    the output byte-stable run-to-run."""
    adj = _file_adjacency(g)
    scores = {doc: s for doc, s, _src in fused}
    srcs = {doc: src for doc, _s, src in fused}
    for pos, (doc, _s, _src) in enumerate(fused[:k]):
        row = adj.get(doc)
        if not row:
            continue
        bump = lam / (rrf_k + 1.0) / (pos + 1.0)
        for nb, _w in sorted(row.items(), key=lambda kv: (-kv[1], kv[0])):
            if nb == doc:
                continue
            scores[nb] = scores.get(nb, 0.0) + bump
            srcs.setdefault(nb, "graph")
    out = [(d, round(s, 6), srcs[d]) for d, s in scores.items()]
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


def _fuse(
    vec: list[str],
    lex: list[str],
    w_vec: float = 1.0,
    w_lex: float = 1.0,
    rrf_k: float = RRF_K,
) -> list[tuple[str, float, str]]:
    """Two-side RRF — the literal one-vector-list + one-lexical-list
    contract form; ``search`` uses it for pass-1 ranking, ``_rrf`` for
    the fused multi-side (two-pass) ranking."""
    return _rrf([("vec", vec, w_vec), ("bm25", lex, w_lex)], rrf_k)


def _file_adjacency(g) -> dict[str, dict[str, int]]:
    """File-level wire counts derived from the fn-level edge sets: edge
    a::f -> b::g is one wire between a and b. Bidirectional by
    construction (an edge makes each file a neighbor of the other)."""
    import graph  # lazy: binding only, attrs read at call time

    adj: dict[str, dict[str, int]] = {}
    for src_key, dsts in g.edges.items():
        src = graph.split_key(src_key)
        row = adj.setdefault(src, {})
        for dst_key in dsts:
            dst = graph.split_key(dst_key)
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

def _surface(fs, imports: bool = False) -> list[str]:
    """Ordered identifier surface of a FileSym: class name first, then
    fn / signal / member / const names, each group sorted — the same
    surface the BM25F symbols field indexes, so a harvested name is
    guaranteed lexically retrievable. ``imports=True`` (issue #228,
    RepoCoder identifier harvest) appends the file's imported symbol
    names (from_imports — the y in `from x import y`): those are NOT
    part of the symbols field, but they reappear as tokens in
    consuming files' body text, so the pass-2 lexical re-score still
    matches them and the vec side sees them verbatim."""
    out: list[str] = []
    if fs.class_name:
        out.append(fs.class_name)
    out += sorted(fs.funcs)
    out += sorted(fs.signals)
    out += sorted(fs.members)
    out += sorted(fs.consts)
    if imports:
        out += sorted({name for _mod, name in fs.from_imports})
    return out


def _augment(query: str, top: list[str], g, budget: int = TWO_PASS_BUDGET,
             imports: bool = TWO_PASS_IMPORTS) -> str:
    """RepoCoder-style augmented query (issue #74): the pass-1 lexical
    top hits donate their identifier surface — the exact tokens a
    re-query hunts — to a second retrieve. (Lexical pool: a pure
    function of query + corpus, so pass-1 embed jitter cannot amplify
    through the harvest.) Rank order across hits, sorted order within
    one, first-seen dedupe, hard char budget: fully deterministic, no
    RNG anywhere. Identifier-only and 320 chars on bench evidence
    (#74 A/B): body text diluted short queries (hit@1 0.40 -> 0.36,
    hit@5 0.84 -> 0.80) while identifiers alone lift hit@10
    (0.92 -> 0.96, one full miss recovered) at hit@5 parity. Returns ""
    when the graph knows none of the hits (pass 2 skipped)."""
    parts: list[str] = []
    seen: set[str] = set()
    for f in top:
        fs = g.files.get(f)
        if fs is None:
            continue
        for ident in _surface(fs, imports):
            if ident not in seen:
                seen.add(ident)
                parts.append(ident)
    if not parts:
        return ""
    return query + "\n" + " ".join(parts)[:budget]

def search(
    query: str,
    k: int = 12,
    bm25: bool = True,
    expand: bool = True,
    weights: tuple[float, float] | None = None,
    graph_boost: float | None = None,
    rrf_k: float | None = None,
    two_pass: bool | dict | None = None,
    query_prefix: str | None = None,
) -> list[dict[str, object]]:
    """Hybrid recall: chroma vector ranks fused with BM25F lexical
    ranks, each hit carrying 1-hop graph context labels. ``bm25`` /
    ``expand`` are the bench switches (False, False = the pure-vector
    baseline behavior). ``weights`` = (vec, bm25) list weights for
    fusion arbitration; None keeps the pinned unweighted RRF (k=30).
    ``graph_boost`` = λ multiplier of the RRF unit 1/(rrf_k+1) — each
    fused top-k source promotes its 1-hop wire neighbors by
    λ·unit/(source rank); None keeps the module default GRAPH_BOOST
    (0.25, the #228 grid winner; 0.0 = explicit off). The boost rides
    the structural wire only — the graph loads for bm25 / expand /
    two_pass (pure-vector keeps vector ranks untouched), and the
    degraded BM25F-only contract is served without it. ``rrf_k``
    overrides the fusion constant for bench sweeps.

    ``query_prefix`` (issues #75/#217, JCE card): task-instruction
    text prepended to the EMBEDDED query only — pass 1 and the two-pass
    augmented retrieve both; the lexical side keeps the raw query so
    instruction tokens never pollute BM25F. None (the default) ships
    the winning ``QUERY_PREFIX``; "" is the explicit raw wire (bench
    unprefixed legs).

    ``two_pass`` (issue #74, RepoCoder): deterministic second retrieve —
    the pass-1 top hits donate their identifier surface (char-budgeted
    via ``_augment``) to an augmented query, re-embedded once and
    RRF-fused with the pass-1 ranks (embed budget: 2 calls per query,
    hard cap). Engaged hits carry ``two_pass: True``; a failed pass 2
    warns once on stderr and serves the pass-1 fusion unmarked. Never
    attempted when the vector side is already degraded — the BM25F-only
    contract stays byte-identical. None defers to the config knob
    ``recall_two_pass`` (nav reads it; default from the #74 bench A/B).
    A dict (issue #228 bench tuning) switches the loop ON and overrides
    the module defaults per call — keys pool / budget / weight / imports
    map to the TWO_PASS_* constants; True is exactly the defaults."""
    k = max(1, min(k, 50))
    w_vec, w_lex = weights if weights is not None else (1.0, 1.0)
    if graph_boost is not None and graph_boost < 0.0:
        raise ValueError(f"graph_boost must be >= 0, got {graph_boost}")
    lam = GRAPH_BOOST if graph_boost is None else graph_boost
    krrf = RRF_K if rrf_k is None else rrf_k
    pfx = QUERY_PREFIX if query_prefix is None else query_prefix
    depth = max(16, 4 * k)
    if two_pass is None:
        import nav  # lazy: knob follows the active config (see header)

        two_pass = bool(getattr(nav, "RECALL_TWO_PASS", False))
    if isinstance(two_pass, dict):
        tp = two_pass
        two_pass = True
    else:
        tp = {}
    tp_pool = max(1, int(tp.get("pool", TWO_PASS_POOL)))
    tp_budget = max(0, int(tp.get("budget", TWO_PASS_BUDGET)))
    tp_weight = float(tp.get("weight", TWO_PASS_WEIGHT))
    tp_imports = bool(tp.get("imports", TWO_PASS_IMPORTS))

    vec: list[str] = []
    metas: dict[str, dict] = {}
    reason: str | None = None
    try:
        vec, metas = _vector_ranks(pfx + query, depth)
        if not vec:
            reason = "vector index is empty (call rescan first)"
    except Exception as exc:  # backend down = degraded, never a crash
        import nav  # lazy: truthful labels share nav's classifier (#115)

        reason = nav.embed_failure_reason(exc)
    if reason:
        print(
            f"recall: vector recall unavailable — {reason}; serving BM25F-only",
            file=sys.stderr,
        )

    g = None
    lex: list[str] = []
    if bm25 or expand or two_pass:
        import graph  # lazy: binding only, attrs read at call time

        g = graph.get_graph()
        if bm25:
            lex = [p for p, _s in _cached_index(g.files).scores(query)[:depth]]

    sides: list[tuple[str, list[str], float]] = [
        ("vec", vec, w_vec),
        ("bm25", lex, w_lex),
    ]
    engaged = False
    if two_pass and not reason and g is not None:
        # harvest pool = the lexical top-k (a pure function of query +
        # corpus): harvesting from the fused ranks would feed pass-1
        # embed jitter forward into the augmented query and amplify it
        # run-to-run (observed: double-run rank flips); the lexical
        # pool keeps the two query embeds the only jitter surface —
        # same exposure as the single-pass baseline. Vec fallback only
        # when the lexical side is switched off entirely.
        pool = (lex if lex else vec)[:tp_pool]
        aug = _augment(query, pool, g, tp_budget, tp_imports)
        if aug:
            try:
                vec2, metas2 = _vector_ranks(pfx + aug, depth)  # embed 2 of 2
            except Exception as exc:
                import nav  # lazy: same truthful classifier as pass 1

                print(
                    f"recall: two-pass retrieve failed "
                    f"({nav.embed_failure_reason(exc)}); serving pass-1 fusion",
                    file=sys.stderr,
                )
            else:
                lex2: list[str] = []
                if bm25:
                    lex2 = [p for p, _s in _cached_index(g.files).scores(aug)[:depth]]
                sides += [
                    ("vec", vec2, w_vec * tp_weight),
                    ("bm25", lex2, w_lex * tp_weight),
                ]
                metas.update(metas2)
                engaged = True

    fused = _rrf(sides, krrf)
    if reason is None and lam > 0.0 and g is not None:
        fused = _graph_boost(fused, g, k, lam, krrf)

    hits: list[dict[str, object]] = []
    for f, s, src in fused[:k]:
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
            hit["degraded_reason"] = reason
        if engaged:
            hit["two_pass"] = True
        hits.append(hit)

    if expand and g is not None and hits:
        ctx = hop_context([str(h["file"]) for h in hits], g)
        for h in hits:
            h["ctx"] = ctx.get(str(h["file"]), [])
    return hits
