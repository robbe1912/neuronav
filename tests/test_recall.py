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
top = recall.search("sync_functions", k=6)
check("fused search surfaces the defining file",
      any(h["file"] == "graph.py" and h["src"] in ("bm25", "both") for h in top[:3]),
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

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
