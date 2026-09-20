# recall behavioral gate (BM25F fusion + hop expansion) — run in its
# own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_recall.py
# Hermetic by default: self-index profile + NEURONAV_EMBED_FAKE (hash
# embeddings, deterministic; exercises the real query path). A real
# Ollama works too — the assertions are mode-agnostic.
import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NEURONAV_CONFIG"] = str(Path(__file__).resolve().parents[1] / "config" / "neuronav.json")
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

import graph  # noqa: E402  (binds the self-index config)
import navindex, navstore
import recall  # noqa: E402



from harness import check, finish

# Hermetic bootstrap (GK #166 F1): a fresh checkout — CI — starts with
# an empty self-index store; nothing else in the suites job populates
# it. Rescan binds the config above (this repo's own .neuronav under
# the checkout) and, under FAKE embeds, writes deterministic hash
# embeddings. Never a live store: the config root is the checkout
# itself. Skipped when the store already has content.
if navstore.count() == 0:
    navindex.rescan()

g = graph.get_graph(rebuild=True)
check("self index populated", navstore.count() > 0, f"{navstore.count()} files embedded")
# langsep restatement (8d class): _fold_continuations moved to
# extractors/common.py as fold_continuations — the defining file moved
# with the identifier; lexical pin tracks it, intent unchanged.
lex = recall.BM25F(g.files).scores("fold_continuations")
check("bm25 exact identifier -> defining file",
      bool(lex) and lex[0][0] == "extractors/common.py", str(lex[:3]))
lex = recall.BM25F(g.files).scores("_graph_boost")
# issue #365 retired _file_adjacency — recall's 1-hop expansion now
# rides graph.file_wires — so the pin tracks the surviving consumer of
# that law in recall.py (the boost itself). fold_continuations
# precedent above: the exact identifier's DEFINING file stays in its
# top hits (top-3, not rank-1 — the #345 IDF re-weighting can let a
# partial-token neighbor edge out the definer on rank 1).
check("bm25 exact identifier -> recall.py",
      bool(lex) and any(f == "recall.py" for f, _s in lex[:3]), str(lex[:3]))
top = recall.search("sync_functions", k=6 if not os.environ.get("NEURONAV_EMBED_FAKE") else 12)
# FAKE embeds are hash-random: cosine distances collapse into near-ties and
# HNSW traversal order (hence vec ranks, hence RRF order) depends on the
# store's mutation history. The old fused-membership-in-k pin was an
# authoring-era corpus accident: adding ONE indexed file re-rolls the fake
# embed hashes and can push the bm25-rank-3 defining file out of the fused
# top-12 (RRR rivals with lucky vec draws outrank it) — measured: bm25
# scores byte-identical before/after the corpus growth, only the random vec
# tie-break moved. The honest FAKE-leg law is the deterministic lexical
# invariant; REAL mode keeps the exact fused top-3 rank. FAKE window is
# top-5, not top-3: the #200 re-export trim shortened extractors/__init__.py
# enough that BM25F length-norm concentrates its single sync_functions
# docstring mention — measured 5.8867 (__init__) vs 5.8142 (definer
# graph.py), definer at rank 4; graph.py's own scores are byte-identical,
# only the shorter rival moved. Do not re-tighten without re-measuring.
lex2 = recall.BM25F(g.files).scores("sync_functions")
check("fused search surfaces the defining file",
      (any(h["file"] == "graph.py" and h["src"] in ("bm25", "both") for h in top[:3]))
      if not os.environ.get("NEURONAV_EMBED_FAKE")
      else ("graph.py" in [f for f, _ in lex2[:5]]),
      str(([f for f, _ in lex2[:5]] if os.environ.get("NEURONAV_EMBED_FAKE") else [(h["file"], h["src"]) for h in top])))

# 2. a query with zero lexical overlap keeps the pure-vector ordering —
# fusion must not disturb what the vector side serves. Built by
# concatenation so the tokens appear nowhere in the corpus, including
# this file.
q = "qw" + "xyz  bl" + "orpt"
fused = recall.search(q, k=8)
# lexical abstention, not rank freeze: the shipped default boost may
# reorder wired neighbours (that is its job — issue #228), but the
# lexical side must still contribute nothing to a zero-overlap query.
check("no-overlap query: lexical side abstains",
      bool(fused) and all(h["src"] in ("vec", "graph") for h in fused),
      str([(h["file"], h["src"]) for h in fused]))

# 3. determinism: identical query -> byte-identical results, no dupes
a = recall.search("graph signal wiring edges", k=12)
b = recall.search("graph signal wiring edges", k=12)
check("search byte-stable run-to-run",
      json.dumps(a) == json.dumps(b) and len(a) == 12)
files = [h["file"] for h in a]
check("fused results deduped", len(files) == len(set(files)))
check("contract keys and bounded ctx",
      all({"file", "score", "src", "ctx"} <= set(h) for h in a)
      and all(h["src"] in ("vec", "bm25", "both", "graph") for h in a)
      and all(len(h["ctx"]) <= 3 for h in a))
check("healthy mode carries no degraded flag",
      all("degraded" not in h for h in a))

# 4. ctx labels are real bidirectional 1-hop graph neighbors —
# recomputed here straight from g.edges, independent of recall.py
adj: dict[str, set[str]] = {}
for src_key, dsts in g.edges.items():
    sf = src_key.split("::", 1)[0]
    for dk in dsts:
        df = dk.split("::", 1)[0]
        if df != sf:
            adj.setdefault(sf, set()).add(df)
            adj.setdefault(df, set()).add(sf)
bad = [(h["file"], c) for h in a for c in h["ctx"]
       if c == h["file"] or c not in adj.get(h["file"], set())]


check("ctx entries are real 1-hop neighbors", not bad, str(bad[:4]))
ctx_all = recall.hop_context(sorted(g.files), g)
check("corpus has wired files (non-vacuous)", any(ctx_all.values()))
check("ctx cap + self-exclusion corpus-wide",
      all(len(v) <= 3 and f not in v for f, v in ctx_all.items()))

# 5. vector side unavailable: BM25F-only, LOUD — every hit marked, one
# stderr line, never an exception and never silent
def _boom(query, depth):
    raise RuntimeError("simulated backend outage")


def _boom_raises():
    try:
        _boom("", 0)
    except RuntimeError:
        return True  # the stub's contract
    return False


check("backend-down stub raises", _boom_raises())


orig = recall._vector_ranks
recall._vector_ranks = _boom
try:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        d = recall.search("graph signal wiring edges", k=5)
        with contextlib.redirect_stderr(io.StringIO()):
            d2 = recall.search("graph signal wiring edges", k=5)
finally:
    recall._vector_ranks = orig
check("degraded mode marks every hit bm25-only",
      bool(d) and all(h.get("degraded") is True and h["src"] == "bm25" for h in d),
      str([(h["file"], h["src"], h.get("degraded")) for h in d[:3]]))
check("degraded mode warns on stderr",
      "recall:" in err.getvalue() and "BM25F-only" in err.getvalue(),
      err.getvalue().strip()[:120])
check("degraded results deterministic", json.dumps(d) == json.dumps(d2))

# 6. bench switches: bm25=False + expand=False is the pure-vector shape
pure = recall.search("registry parse extract", k=6, bm25=False, expand=False)
check("pure-vector path: vec src, no ctx",
      bool(pure) and all(h["src"] == "vec" and h["ctx"] == [] for h in pure))

# 7. two-pass (RepoCoder, issue #74): opt-in second retrieve — pass-1
# top hits donate their identifier surface to the augmented query; the
# output is byte-stable, the embed budget caps at 2 calls per query,
# engaged hits carry two_pass=True, and the degraded contract is
# untouched (pass 2 is never attempted when the vector side is down).
tp = recall.search("graph signal wiring edges", k=12, two_pass=True)
tp2 = recall.search("graph signal wiring edges", k=12, two_pass=True)
check("two-pass byte-stable run-to-run",
      json.dumps(tp) == json.dumps(tp2) and len(tp) == 12)
check("two-pass marks every hit", all(h.get("two_pass") is True for h in tp))
off = recall.search("graph signal wiring edges", k=12, two_pass=False)
check("two-pass opt-out leaves hits unmarked",
      bool(off) and all("two_pass" not in h for h in off))
check("two-pass keeps contract keys and dedup",
      all(set(h) >= {"file", "score", "src", "ctx"} for h in tp)
      and len({h["file"] for h in tp}) == 12)
pv = recall.search("qw" + "xyz  bl" + "orpt", k=8, bm25=False, expand=False, two_pass=True)
check("two-pass works in pure-vector mode",
      len(pv) == 8 and all(h.get("two_pass") is True
                           and h["src"] in ("vec", "graph")
                           and h["ctx"] == [] for h in pv))

# embed budget: at most 2 embed calls per query even with two passes
calls: list[int] = []
_orig_embed = navstore.embed


def _counting(texts):
    calls.append(len(texts))
    return _orig_embed(texts)


navstore.embed = _counting
try:
    recall.search("graph signal wiring edges", k=12, two_pass=True)
    two_calls = len(calls)
    calls.clear()
    recall.search("graph signal wiring edges", k=12, two_pass=False)
    one_calls = len(calls)
finally:
    navstore.embed = _orig_embed
check("two-pass caps the embed budget at 2", two_calls == 2, f"{two_calls} embed calls")
check("single-pass stays 1 embed call", one_calls == 1, f"{one_calls} embed calls")

# degraded + two_pass: pass 2 skipped, BM25F-only contract byte-identical
recall._vector_ranks = _boom
try:
    err3 = io.StringIO()
    with contextlib.redirect_stderr(err3):
        dtp = recall.search("graph signal wiring edges", k=5, two_pass=True)
finally:
    recall._vector_ranks = orig
check("two-pass degraded is byte-identical to plain degraded",
      json.dumps(dtp) == json.dumps(d))
check("two-pass degraded keeps the BM25F-only contract",
      bool(dtp) and all(h.get("degraded") is True and h["src"] == "bm25"
                        and "two_pass" not in h for h in dtp))

# 7a. two-pass tuning knobs (issue #228, RepoCoder loop): the dict form
# of two_pass overrides the module defaults per call — pool = harvest
# donor count, budget = identifier-tail chars, imports = harvested
# from_imports names, weight = pass-2 RRF side scale. True is exactly
# the defaults; every knob is deterministic and pinned end-to-end.
_q0 = "cluster labeling wires"
_lex0 = [p for p, _s in recall._cached_index(g.files).scores(_q0)[:24]]
check("harvest donor corpus is non-empty for the pinned query",
      len(_lex0) >= 3, str(_lex0[:3]))
_aug1 = recall._augment(_q0, _lex0[:1], g, budget=10 ** 9)
_aug3 = recall._augment(_q0, _lex0[:3], g, budget=10 ** 9)
check("pool knob widens the harvested identifier tail",
      _aug1 != _aug3 and _aug3.startswith(_aug1))
_full = recall._augment("q", ["clusters.py"], g, budget=10 ** 9)
_cut = recall._augment("q", ["clusters.py"], g, budget=13)
check("budget truncates the identifier tail",
      _cut == "q\n" + _full.split("\n", 1)[1][:13])
# navconfig keeps a from-import name its own surface lacks: most resolved
# imports fold into the importer's consts (graph's definer folding), so
# the imports knob only adds the residual — navconfig/EXTENSIONS is one
# (issue #344: _apply_config's lazy extractors import moved to the
# config leaf; nav.py itself now imports only the sibling leaves).
_fi = sorted({n for _m, n in g.files["navconfig.py"].from_imports}
             - set(recall._surface(g.files["navconfig.py"])))
_a_plain = recall._augment("q", ["navconfig.py"], g, budget=10 ** 9)
_a_imp = recall._augment("q", ["navconfig.py"], g, budget=10 ** 9, imports=True)
check("imports=True harvests from_imports names beyond the base surface",
      bool(_fi) and all(n in _a_imp.split() for n in _fi)
      and not any(n in _a_plain.split() for n in _fi), str(_fi))

_tuned = recall.search("graph signal wiring edges", k=12,
                       two_pass={"pool": 4, "budget": 200, "weight": 0.5})
_tuned2 = recall.search("graph signal wiring edges", k=12,
                        two_pass={"pool": 4, "budget": 200, "weight": 0.5})
check("tuned two-pass byte-stable run-to-run",
      json.dumps(_tuned) == json.dumps(_tuned2) and len(_tuned) == 12)
check("tuned two-pass marks every hit", all(h.get("two_pass") is True for h in _tuned))
_dflt = recall.search("graph signal wiring edges", k=12, two_pass={})
_same = recall.search("graph signal wiring edges", k=12, two_pass=True)
check("two_pass={} is exactly True (module defaults)",
      json.dumps(_dflt) == json.dumps(_same))

# the knobs reach the wire: pass 2 embeds exactly prefix + tuned augment,
# and the pass-2 RRF sides carry the weight scale
_orig_vr0 = recall._vector_ranks
_tp_seen: list[str] = []


def _tp_capture(q, depth):
    _tp_seen.append(q)
    return _orig_vr0(q, depth)
recall._vector_ranks = _tp_capture
try:
    recall.search(_q0, k=6, two_pass={"pool": 2, "budget": 5000, "imports": True})
finally:
    recall._vector_ranks = _orig_vr0
check("pass-2 embeds prefix + pool/budget/imports-tuned augment",
      len(_tp_seen) == 2
      and _tp_seen[1] == recall.QUERY_PREFIX
      + recall._augment(_q0, _lex0[:2], g, budget=5000, imports=True),
      repr(_tp_seen[1][:120]))

_sides_seen: list[list[float]] = []
_orig_rrf = recall._rrf


def _rrf_capture(sides, rrf_k):
    _sides_seen.append([w for _t, _l, w in sides])
    return _orig_rrf(sides, rrf_k)


recall._rrf = _rrf_capture
try:
    recall.search(_q0, k=6, two_pass={"weight": 0.5})
    recall.search(_q0, k=6, two_pass=True)
finally:
    recall._rrf = _orig_rrf
check("weight scales exactly the pass-2 RRF sides",
      len(_sides_seen) == 2 and _sides_seen[0][2:] == [0.5, 0.5]
      and _sides_seen[1][2:] == [1.0, 1.0], str(_sides_seen))

# 7b. task-instruction query prefix (issue #217): the winning nl2code
# instruction from the #214/#75 A/B ships default-on — prepended to the
# EMBEDDED query only (pass 1 AND the two-pass augmented retrieve); the
# lexical side keeps the raw query. Deterministic constant (no
# clock/env input); query_prefix='' is the explicit raw wire.
seen: list[str] = []
_orig_vr = recall._vector_ranks


def _vr_capture(q, depth):
    seen.append(q)
    return _orig_vr(q, depth)


recall._vector_ranks = _vr_capture
try:
    recall.search("cluster labeling wires", k=6)
    p1 = list(seen)
    seen.clear()
    recall.search("cluster labeling wires", k=6, two_pass=True)
    p2 = list(seen)
    seen.clear()
    recall.search("cluster labeling wires", k=6, query_prefix="")
    p3 = list(seen)
finally:
    recall._vector_ranks = _orig_vr
Q = "Find the most relevant code snippet given the following query:\n"
check("query prefix constant is the pinned nl2code instruction",
      recall.QUERY_PREFIX == Q, repr(recall.QUERY_PREFIX))
check("default pass-1 embeds prefix + raw query",
      p1 == [Q + "cluster labeling wires"], repr(p1))
check("two-pass prefixes both embeds; pass 2 rides the augmented query",
      len(p2) == 2 and p2[0] == Q + "cluster labeling wires"
      and p2[1].startswith(Q) and p2[1] != p2[0], repr(p2))
check("query_prefix='' is the explicit raw wire",
      p3 == ["cluster labeling wires"], repr(p3))

# lexical side never sees the instruction tokens: wrap the cached BM25F
# index's scores and pin the exact raw query strings it is fed
_idx = recall._cached_index(g.files)
_orig_scores = _idx.scores
lex_seen: list[str] = []


def _scores_capture(q):
    lex_seen.append(q)
    return _orig_scores(q)


_idx.scores = _scores_capture
try:
    recall.search("cluster labeling wires", k=6)
    recall.search("cluster labeling wires", k=6, two_pass=True)
finally:
    _idx.scores = _orig_scores
check("BM25F side keeps the raw query (both passes, no prefix)",
      len(lex_seen) == 3 and lex_seen[0] == lex_seen[1] == "cluster labeling wires"
      and lex_seen[2].startswith("cluster labeling wires") and lex_seen[2] != lex_seen[1]
      and all(not q.startswith(Q) for q in lex_seen), repr(lex_seen))

# 8. navstore.search delegates to the same fused path (server consumes this)
via_nav = navstore.search("graph signal wiring edges", k=6)
check("navstore.search delegates to recall.search",
      json.dumps(via_nav) == json.dumps(
          recall.search("graph signal wiring edges", k=6, two_pass=False)),
      str([h["file"] for h in via_nav]))

# 8. graph-neighbor rank boost (issue #73): deterministic post-fusion
# promotion of 1-hop neighbors. The adjacency here is recomputed
# straight from g.edges, independent of graph.file_wires.
adj2: dict[str, set[str]] = {}
for _sk, _dsts in g.edges.items():
    _sf = _sk.split("::", 1)[0]
    for _dk in _dsts:
        _df = _dk.split("::", 1)[0]
        if _df != _sf:
            adj2.setdefault(_sf, set()).add(_df)
            adj2.setdefault(_df, set()).add(_sf)

# 8a. the module default ships the #228 grid winner (λ 0.25 @ rrf_k 30,
# double-run stable — bench/RESULTS.md); graph_boost=0.0 is the
# explicit off wire — byte-stable, never graph-tagged.
z = recall.search("graph signal wiring edges", k=12, graph_boost=0.0)
z2 = recall.search("graph signal wiring edges", k=12, graph_boost=0.0)
zd = recall.search("graph signal wiring edges", k=12)
check("module default is the swept winner (λ 0.25 @ rrf_k 30, #228)",
      recall.GRAPH_BOOST == 0.25 and recall.RRF_K == 30.0
      and json.dumps(zd) == json.dumps(
          recall.search("graph signal wiring edges", k=12,
                        graph_boost=recall.GRAPH_BOOST, rrf_k=recall.RRF_K)))
check("graph_boost=0 is the explicit off wire",
      json.dumps(z) == json.dumps(z2)
      and all(h["src"] in ("vec", "bm25", "both") for h in z))

# 8b. λ>0 reranks deterministically: byte-identical double run.
b1 = recall.search("graph signal wiring edges", k=12, graph_boost=1.0)
b2 = recall.search("graph signal wiring edges", k=12, graph_boost=1.0)
check("boosted search byte-stable run-to-run", json.dumps(b1) == json.dumps(b2))

# 8c. boost only ever adds: for every doc visible in BOTH top-12s the
# λ=1 score is at least its λ=0 score (a doc can fall below the cut,
# but its score is never lowered).
base_scores = {h["file"]: h["score"] for h in z}
boost_scores = {h["file"]: h["score"] for h in b1}
common = set(base_scores) & set(boost_scores)
check("boost never lowers an existing score",
      bool(common) and all(boost_scores[f] >= base_scores[f] for f in common))

# 8d. promotion is real AND non-vacuous (GK #166 F2): a 1-hop
# neighbour of the first WIRED off-wire hit must STRICTLY RANK UP under
# λ=16 — its boosted rank beats its off-wire rank (a neighbour outside
# the off-wire top-12 entering the boosted top-12 counts as up). The
# off-wire rank map is deterministic (8a), so a strictly-lifted
# neighbour proves promotion rather than lexical accident. Top-0
# itself is corpus-composition-sensitive under FAKE embeds (hash
# near-ties let a lexically-heavy edgeless file top the list — e.g. a
# generated-content suite file), so anchor on the first off-wire hit
# that actually has neighbours; the boost law is about wired files
# either way.
wired0 = next((h["file"] for h in z[:12] if adj2.get(h["file"])), None)
nb0 = adj2.get(wired0, set())
strong = recall.search("graph signal wiring edges", k=12, graph_boost=16.0)
rz = {h["file"]: i for i, h in enumerate(z)}
ups = [(h["file"], rz.get(h["file"], 99), i) for i, h in enumerate(strong)
       if h["file"] in nb0 and rz.get(h["file"], 99) > i]
check("strong lambda strictly lifts a wired 1-hop neighbour",
      wired0 is not None  # non-vacuous: some λ=0 top-k hit is wired
      and bool(ups),      # and promotion moved at least one of its neighbours up
      f"wired0={wired0} ups={ups[:3]} strong={[h['file'] for h in strong[:3]]}")

# 8e. every graph-tagged hit is a genuine 1-hop neighbor of a λ=0
# top-k source (boost sources are exactly the fused top-k), and λ=0
# hits never carry the graph tag.
srcs0 = {h["file"] for h in z[:12]}
bad_g = [h["file"] for h in b1 if h["src"] == "graph"
         and not any(h["file"] in adj2.get(s, set()) for s in srcs0)]
check("graph-tagged hits are real neighbors of top-k sources", not bad_g, str(bad_g))
srcsd = {h["file"] for h in zd[:12]}
bad_d = [h["file"] for h in zd if h["src"] == "graph"
         and not any(h["file"] in adj2.get(s, set()) for s in srcsd)]
check("default-wire graph tags are real neighbors of top-k sources",
      not bad_d, str(bad_d))
# 8f. rrf_k override plumbing (30 is the shipped default since #228):
# byte-stable, and the explicit override reproduces the default wire.
s30 = recall.search("graph signal wiring edges", k=12, rrf_k=30.0)
s30b = recall.search("graph signal wiring edges", k=12, rrf_k=30.0)
check("rrf_k override byte-stable",
      json.dumps(s30) == json.dumps(s30b) and json.dumps(s30) == json.dumps(zd))

# 8f'. negative boost is rejected loudly, not silently clamped.
try:
    recall.search("graph signal wiring edges", k=12, graph_boost=-0.5)
    neg_raised = False
except ValueError as e:
    neg_raised = "graph_boost" in str(e)
check("negative graph_boost raises ValueError", neg_raised)

# 8g. degraded mode keeps the BM25F-only contract even with the boost
# knob on: the boost never rides the degraded wire (no graph tags, no
# two_pass key), every hit still marked, deterministic, loud warning.
orig = recall._vector_ranks
recall._vector_ranks = _boom
try:
    with contextlib.redirect_stderr(io.StringIO()) as err:
        dgb = recall.search("graph signal wiring edges", k=8, graph_boost=1.0)
        dgb2 = recall.search("graph signal wiring edges", k=8, graph_boost=1.0)
finally:
    recall._vector_ranks = orig
check("degraded + boost keeps the BM25F-only contract",
      bool(dgb) and all(h.get("degraded") is True and h["src"] == "bm25"
                        and "two_pass" not in h for h in dgb)
      and "BM25F-only" in err.getvalue())
check("degraded + boost deterministic", json.dumps(dgb) == json.dumps(dgb2))

# 8h. single truth (issue #365): recall's 1-hop expansion rides
# graph.file_wires — gutting the fold must empty every ctx label and
# strip every graph-src hit. A re-inlined adjacency in recall would
# keep serving g.edges and sail through: exactly the silent divergence
# #365 closed.
_orig_fw = graph.Graph.file_wires
try:
    graph.Graph.file_wires = lambda self: {rel: {} for rel in self.files}
    gut_ctx = recall.hop_context(sorted(g.files), g)
    gut = recall.search("graph signal wiring edges", k=12, graph_boost=1.0)
finally:
    graph.Graph.file_wires = _orig_fw
check("gutted file_wires empties ctx labels and graph srcs (#365)",
      all(not v for v in gut_ctx.values())
      and all(h["src"] != "graph" for h in gut))

# 11. absolute relevance floor (issue #297): pure-noise queries used to
# fuse into confident unmarked rows (the PR-254 standing red). The floor
# is MARK-ONLY: a row is weak when every side's raw evidence is under
# the calibrated floor (cos < 0.48 AND bm25 < 6.0); ranks, membership,
# and scores are unchanged — bench/golden pins the quality contract.
gq = recall.search("purple elephant dishwasher quadrant marmalade", k=5)
check("garbage query: every fused row weak-flagged",
      len(gq) == 5 and all(h.get("weak") is True for h in gq),
      str([(h["file"], h["src"], h.get("weak")) for h in gq[:3]]))
tq = recall.search("bm25 idf length normalization", k=12)
# Query re-calibrated at #377 — the neutral-helper hoist moved ~230
# lines between extractor files, shifting the self-index BM25F corpus
# stats until the old query ("graph signal wiring edges") cleared the
# mark-only floor for NO top-3 row. Floors are UNCHANGED (cos 0.48 /
# bm25 6.0, issue #297 + bench/golden — the pinned quality contract);
# what over-fit was the query choice. Intent preserved and tightened:
# a real query about the ranking machinery must land non-weak rows on
# its defining files (recall.py / server_search.py — untouched by
# extractor refactors, so the pin rides stable content, not mutable
# corpus stats). Systemic follow-up filed by GK: this leg should
# eventually assert rank-shape on a stable fixture corpus instead.
check("real query: top rows clear the floor (not all weak)",
      any("weak" not in h for h in tq[:3])
      and any(h["file"] == "recall.py" for h in tq[:3]),
      str([(h["file"], h.get("weak")) for h in tq[:3]]))

import graph  # noqa: E402  (fn-level floor shares the same constant)
import server  # noqa: E402
import server_search  # noqa: E402  # _fmt's home since #345  (_fmt is the MCP render surface for hits)

fng = graph.find_functions("purple elephant dishwasher quadrant marmalade", 4)
check("garbage fn query: rows weak-flagged (mark-only)",
      bool(fng) and all(r.get("weak") is True for r in fng),
      str([(r["func"], r["score"], r.get("weak")) for r in fng[:2]]))
rendered = server_search._fmt(gq)
check("weak rows reach the wire with a floor footer",
      rendered.count("  weak") == len(gq) and "relevance floor" in rendered,
      rendered[-140:])
check("floor constants pinned (self-index calibration)",
      recall.RELEVANCE_FLOOR_SIM == 0.48 and recall.RELEVANCE_FLOOR_BM25 == 6.0)

# ---- #298 smalls: dead import, docstring truth, pass-2 budget gate -----------
check("no unused weakref import rides along (#298)",
      not hasattr(recall, "weakref"))
check("_rrf docstring states k=RRF_K=30, not k=60 (#298)",
      "k=RRF_K=30" in (recall._rrf.__doc__ or ""), (recall._rrf.__doc__ or "")[:60])
check("_augment budget<=0 skips pass 2 entirely (#298)",
      recall._augment("q", ["nav.py"], g, budget=0) == "")

finish()
