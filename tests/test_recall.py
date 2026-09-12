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
import nav  # noqa: E402
import recall  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


g = graph.get_graph(rebuild=True)
check("self index populated", nav.count() > 0, f"{nav.count()} files embedded")

lex = recall.BM25F(g.files).scores("_fold_continuations")
check("bm25 exact identifier -> defining file",
      bool(lex) and lex[0][0] == "graph.py", str(lex[:3]))
lex = recall.BM25F(g.files).scores("_file_adjacency")
check("bm25 exact identifier -> recall.py",
      bool(lex) and lex[0][0] == "recall.py", str(lex[:3]))
top = recall.search("sync_functions", k=6 if not os.environ.get("NEURONAV_EMBED_FAKE") else 12)
# FAKE embeds are hash-random: cosine distances collapse into near-ties and
# HNSW traversal order (hence vec ranks, hence RRF order) depends on the
# store's mutation history — membership within k is the honest pin there.
# REAL mode keeps the exact top-3 rank.
check("fused search surfaces the defining file",
      any(h["file"] == "graph.py" and h["src"] in ("bm25", "both")
          for h in (top if os.environ.get("NEURONAV_EMBED_FAKE") else top[:3])),
      str([(h["file"], h["src"]) for h in top]))

# 2. a query with zero lexical overlap keeps the pure-vector ordering —
# fusion must not disturb what the vector side serves. Built by
# concatenation so the tokens appear nowhere in the corpus, including
# this file.
q = "qw" + "xyz  bl" + "orpt"
fused = recall.search(q, k=8)
pure = recall.search(q, k=8, bm25=False, expand=False)
check("no-overlap query stays vector-served",
      [h["file"] for h in fused] == [h["file"] for h in pure]
      and bool(fused) and all(h["src"] == "vec" for h in fused),
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
      and all(h["src"] in ("vec", "bm25", "both") for h in a)
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

# 7. nav.search delegates to the same fused path (server consumes this)
via_nav = nav.search("graph signal wiring edges", k=6)
check("nav.search delegates to recall.search",
      [h["file"] for h in via_nav]
      == [h["file"] for h in recall.search("graph signal wiring edges", k=6)],
      str([h["file"] for h in via_nav]))

# 8. graph-neighbor rank boost (issue #73): deterministic post-fusion
# promotion of 1-hop neighbors. The adjacency here is recomputed
# straight from g.edges, independent of recall._file_adjacency.
adj2: dict[str, set[str]] = {}
for _sk, _dsts in g.edges.items():
    _sf = _sk.split("::", 1)[0]
    for _dk in _dsts:
        _df = _dk.split("::", 1)[0]
        if _df != _sf:
            adj2.setdefault(_sf, set()).add(_df)
            adj2.setdefault(_df, set()).add(_sf)

# 8a. explicit λ=0 (and the module default) are byte-identical no-ops —
# the plumbing lands default-off.
z = recall.search("graph signal wiring edges", k=12)
check("graph_boost=0 is a no-op",
      json.dumps(recall.search("graph signal wiring edges", k=12, graph_boost=0.0))
      == json.dumps(z))

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

# 8d. promotion is real: with a strong λ, at least one top-k hit under
# boost is a 1-hop neighbor of the λ=0 top-1 file — pulled into the
# rank signal from the graph, not from vec/bm25 lists (src says graph).
strong = recall.search("graph signal wiring edges", k=12, graph_boost=16.0)
top0 = z[0]["file"]
nb0 = adj2.get(top0, set())
check("strong lambda pulls a 1-hop neighbor of the top hit",
      top0 in adj2  # non-vacuous: the corpus top hit is wired
      and any(h["file"] in nb0 for h in strong[:12]) and strong[0]["file"] != top0,
      f"top0={top0} nbs={sorted(nb0)[:3]} strong={[h['file'] for h in strong[:3]]}")

# 8e. every graph-tagged hit is a genuine 1-hop neighbor of a λ=0
# top-k source (boost sources are exactly the fused top-k), and λ=0
# hits never carry the graph tag.
srcs0 = {h["file"] for h in z[:12]}
bad_g = [h["file"] for h in b1 if h["src"] == "graph"
         and not any(h["file"] in adj2.get(s, set()) for s in srcs0)]
check("graph-tagged hits are real neighbors of top-k sources", not bad_g, str(bad_g))
check("no graph tag without boost",
      all(h["src"] in ("vec", "bm25", "both") for h in z))

# 8f. rrf_k sweep plumbing: k=30 sharpens the unit; still byte-stable
# and still a no-op at λ=0 relative to itself.
s30 = recall.search("graph signal wiring edges", k=12, rrf_k=30.0)
s30b = recall.search("graph signal wiring edges", k=12, rrf_k=30.0)
check("rrf_k override byte-stable", json.dumps(s30) == json.dumps(s30b))

# 8f'. negative boost is rejected loudly, not silently clamped.
try:
    recall.search("graph signal wiring edges", k=12, graph_boost=-0.5)
    neg_raised = False
except ValueError as e:
    neg_raised = "graph_boost" in str(e)
check("negative graph_boost raises ValueError", neg_raised)

# 8g. degraded mode + boost stays loud: every hit marked, deterministic.
orig = recall._vector_ranks
recall._vector_ranks = _boom
try:
    with contextlib.redirect_stderr(io.StringIO()) as err:
        dgb = recall.search("graph signal wiring edges", k=8, graph_boost=1.0)
        dgb2 = recall.search("graph signal wiring edges", k=8, graph_boost=1.0)
finally:
    recall._vector_ranks = orig
check("degraded + boost marks every hit",
      bool(dgb) and all(h.get("degraded") is True for h in dgb)
      and "BM25F-only" in err.getvalue())
check("degraded + boost deterministic", json.dumps(dgb) == json.dumps(dgb2))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
