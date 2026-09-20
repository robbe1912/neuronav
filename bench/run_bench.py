"""bench/run_bench.py — recall-quality benchmark over the neuronav self-index.

Measures 4 configs x the golden query set (bench/golden.json):

  vec      recall.search(bm25=False, expand=False)   baseline cosine
  bm25     recall.search(bm25=True,  expand=False)   +BM25F fusion (RRF)
  expand   recall.search(bm25=False, expand=True)    +1-hop graph ctx on hits
  both     recall.search(bm25=True,  expand=True)    the shipped default

Config switching is INTERNAL (kwargs on recall.search; falls back to plain
navstore.search where recall.py does not exist yet, e.g. the pre-fusion baseline
worktree). NEURONAV_CONFIG is hard-set in-process to the self-index profile
BEFORE any neuronav import — never exported from the shell (AGENTS.md
config-leakage trap). Each measured checkout gets its own state store
(state_dir is root-relative: <root>/.neuronav), so runs never touch the
live shared index.

Runs are recorded as bench/runs/<set>-<config>.json (rank-level evidence,
not raw scores); RESULTS.md is re-rendered from all records, so reruns with
identical metrics produce byte-identical output. Every record stamps the
golden-set fingerprint it was measured against; render() refuses records
from a different (or unfingerprinted) golden set instead of silently
drawing '?' cells (issue #104).

Usage:
  .venv/Scripts/python.exe -X utf8 bench/run_bench.py --set after --configs vec
  .venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake
  .venv/Scripts/python.exe -X utf8 bench/run_bench.py --verify-only

--repo PATH  checkout to measure (default: this script's repo). Point it at
             a detached worktree for per-commit attribution.
--set NAME   record set: after | fake | sweep (fake implies CI plumbing
             mode), or the issue #75 embedding A/B legs ab | qprefix |
             jina | jinaq | jinap (real embeds only; the jina legs ride
             the #17 openai wire against a llama-server hosting the
             official JCE GGUF, and use their own state store under
             .tmp/ so the qwen3 store stays untouched). Since #217 the
             nl2code query prefix is recall's shipped default: `qprefix`
             rides the default wire, `ab`/`jina` pin the raw query
             explicitly (query_prefix=""). Issue #229 doc-shape legs:
             castq (cAST file docs, the shipped default) | rawq (raw
             file docs, chunk_file_doc=0, own .tmp store).
--fake       NEURONAV_EMBED_FAKE=1 (deterministic hash embeddings; coherent
             only against a collection built in the same mode — use a fresh
             checkout/.neuronav per mode).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = BENCH_DIR.parent
K = 12  # contract: navstore.search(q, k=12)
CONFIGS = ("vec", "bm25", "expand", "both", "wfused", "gb", "twopass")
NEEDS = {"vec": (), "bm25": ("bm25",), "expand": ("expand",), "both": ("bm25", "expand"), "wfused": ("bm25", "expand", "weights"), "gb": ("bm25", "expand", "gboost"), "twopass": ("bm25", "expand", "two_pass")}
WFUSED_WEIGHTS = (1.0, 0.7)  # (vec, bm25) — Main-pinned weighted fusion vs unweighted RRF
# graph-neighbor boost (issues #73/#228): gb pins the shipped default
# (λ 0.25 @ rrf_k 30) explicitly for record readability. λ multiplies
# the RRF unit 1/(rrf_k+1); each fused top-k source adds λ·unit/(source
# rank) to every distinct 1-hop file neighbor. The #73 sweep picked the
# λ tier; the #228 ceiling grid (4λ × 3k × 4 weight pairs) confirmed it
# and shipped it default-on — numbers in bench/RESULTS.md.
GB_LAMBDA = 0.25  # #228 grid winner: h1 +0.08 / MRR +0.075 over λ=0, double-run stable
GB_RRF_K = 30.0  # recall.GRAPH_BOOST / recall.RRF_K ship these since #228
GB_LAMBDAS = (0.0, 0.25, 0.5, 1.0, 2.0)
GB_RRF_KS = (30.0, 60.0, 120.0)
# --- issue #228 (census exp 1+2): the recall-ceiling grids ---
# exp 1 — graph-boost fusion grid: λ × RRF-k × (vec, bm25) weights on
# the golden set, qprefix default wire, real embeds (Hydra arXiv
# 2602.11671: dependency-aware retrieval fused after similarity beats
# pure embedding RAG). The λ=0 rows double as the pure weights×k
# fusion sweep; gb0-k60-w1-1 IS the shipped `both` wire and serves as
# the in-grid baseline + the exp-2 easy/hard split seed.
GBC_LAMBDAS = (0.0, 0.25, 0.5, 0.75)
GBC_RRF_KS = (30.0, 60.0, 120.0)
GBC_WEIGHTS = ((1.0, 1.0), (1.0, 0.7), (1.0, 0.5), (0.7, 1.0))
# exp 2 — two-pass RepoCoder loop tuning grid: harvest pool × char
# budget × imports × pass-2 weight scale (RepoCoder EMNLP 2023:
# identifiers harvested from retrieved context lift exact-match
# retrieval; RepoBench ICLR 2024: small kept context beats large).
# Boost stays OFF here — the two questions are isolated; stacking is
# measured only if both grids clear their bars.
TPC_POOLS = (3, 5, 12)
TPC_BUDGETS = (160, 320, 640)
TPC_IMPORTS = (False, True)
TPC_WEIGHTS = (0.5, 1.0)
# --- issue #75: embedding A/B (jina-code-embeddings-0.5b vs qwen3) ---
# Task instructions from the JCE model card (arXiv 2508.21290). The
# nl2code QUERY instruction lives in recall.QUERY_PREFIX and, since
# issue #217, ships as recall's default — bench never redefines it. Only
# the PASSAGE instruction stays bench-local (an index-time A/B knob via
# embed_doc_prefix, not a shipped default).
DOC_PREFIX = "Candidate code snippet:\n"
# JCE-0.5b Q8_0 (official jinaai GGUF) served by llama-server with the
# card's pooling contract — Ollama imports the same GGUF as a completion
# model (no pooling metadata) and its /api/embed refuses it, so the leg
# rides the #17 openai wire against llama-server's /v1/embeddings.
JINA_EMBED = {
    "tag": "jina",
    "embed_url": "http://127.0.0.1:18081/v1/embeddings",
    "embed_model": "jina-code-embeddings-0.5b:Q8_0",
    "embed_dim": 896,
    "state_dir": ".tmp/bench-jina-store",
}
# set name -> run() kwargs; every A/B set is a real-embed leg. Since
# issue #217 the nl2code prefix is recall's shipped default
# (query_prefix None), so `qprefix`/`jinaq`/`jinap` ride the default
# wire while `ab`/`jina` pin query_prefix="" to stay raw-query baselines.
SET_FLAGS = {
    "ab": {"query_prefix": ""},
    "qprefix": {},
    "jina": {"query_prefix": "", "embed": dict(JINA_EMBED)},
    "jinaq": {"embed": dict(JINA_EMBED)},
    "jinap": {"embed": {**JINA_EMBED, "tag": "jina-pfx",
                        "embed_doc_prefix": DOC_PREFIX,
                        "state_dir": ".tmp/bench-jinap-store"}},
    # issue #229: cAST file-doc shaping A/B. castq rides the default
    # wire and the default store (chunk_file_doc=1.0 ships on); rawq is
    # the same-commit raw-docs control in its own .tmp store (a shape
    # flip re-embeds the whole store, so the two legs keep disjoint
    # vectors like the jina variants).
    "castq": {},
    "rawq": {"embed": {"tag": "raw-docs", "chunk_file_doc": 0.0,
                       "state_dir": ".tmp/bench-rawq-store"}},
}


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except Exception:
        return ""


def _load_golden() -> list[dict]:
    data = json.loads((BENCH_DIR / "golden.json").read_text(encoding="utf-8"))
    queries = data["queries"]
    assert len(queries) == 25, f"golden set must stay at 25 queries, got {len(queries)}"
    return queries

def _golden_fp(queries: list[dict] | None = None) -> str:
    """Stable fingerprint of the golden query set — full rows (q, kind,
    targets, anchor: all of them move metrics), order-insensitive. Stamped
    into every record at write time; render() refuses records whose stamp
    doesn't match the current golden (issue #104)."""
    rows = _load_golden() if queries is None else queries
    canon = "\n".join(sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _echo(query: str, hay: str) -> str | None:
    """First contiguous 3-word echo of the query inside `hay`, if any."""
    qt = _TOKEN_RE.findall(query.lower())
    ht = _TOKEN_RE.findall(hay.lower())
    tris = {tuple(ht[i : i + 3]) for i in range(len(ht) - 2)}
    for i in range(len(qt) - 2):
        tri = tuple(qt[i : i + 3])
        if tri in tris:
            return " ".join(tri)
    return None


def verify_golden(repo: Path) -> int:
    """Mechanical justification of the golden set: every target must still
    contain its anchor substring, and prose/cross queries must not echo 3
    contiguous words of any target file. Fails loudly on drift."""
    fails = 0
    for row in _load_golden():
        anchor = row["anchor"]
        anchors = anchor if isinstance(anchor, list) else [anchor] * len(row["targets"])
        texts = {}
        for tgt, a in zip(row["targets"], anchors):
            path = repo / tgt
            if not path.is_file():
                print(f"FAIL  target missing: {tgt} ({row['q']})")
                fails += 1
                continue
            texts[tgt] = path.read_text(encoding="utf-8", errors="replace")
            if a not in texts[tgt]:
                print(f"FAIL  anchor '{a}' not in {tgt} ({row['q']})")
                fails += 1
        if row["kind"] in ("prose", "cross"):
            for tgt, text in texts.items():
                hit = _echo(row["q"], text)
                if hit:
                    print(f"FAIL  echo '{hit}' vs {tgt} ({row['q']})")
                    fails += 1
    print(f"golden verify: {25 - fails}/25 justified" if not fails else "golden verify: DRIFT — re-justify targets")
    return 1 if fails else 0


def _normalize(hits: list) -> list[dict]:
    """Old pre-fusion Hit dataclass and the recall dict contract -> one shape."""
    out = []
    for h in hits:
        if isinstance(h, dict):
            out.append(
                {
                    "file": h["file"],
                    "src": h.get("src", ""),
                    "ctx": list(h.get("ctx") or []),
                }
            )
        else:  # nav.Hit (baseline checkout, pre-recall)
            out.append({"file": h.path, "src": "vec", "ctx": []})
    return out


def _run_config(repo: Path, search_fn, config: str, queries: list[dict]) -> dict:
    per_query = []
    for row in queries:
        hits = _normalize(search_fn(row["q"]))
        files = [h["file"] for h in hits]
        targets = set(row["targets"])
        rank = next((i + 1 for i, f in enumerate(files) if f in targets), None)
        ctx_hits = {}
        for k in (5, 10):
            labels = set()
            for h in hits[:k]:
                labels.update(h["ctx"])
            ctx_hits[k] = any(t in labels for t in targets)
        per_query.append(
            {
                "q": row["q"],
                "rank": rank,
                "ctx5": ctx_hits[5],
                "ctx10": ctx_hits[10],
            }
        )
    n = len(per_query)

    def hit_at(k: int) -> float:
        return round(sum(1 for r in per_query if r["rank"] is not None and r["rank"] <= k) / n, 3)

    def reach_at(k: int) -> float:
        key = f"ctx{k}"
        return round(
            sum(
                1
                for r in per_query
                if (r["rank"] is not None and r["rank"] <= k) or r[key]
            )
            / n,
            3,
        )

    def by_kind() -> dict:
        kinds = {}
        for kind in ("exact", "symbol", "prose", "cross"):
            rows = [r for r, q in zip(per_query, queries) if q["kind"] == kind]
            m = len(rows)
            kinds[kind] = {
                "n": m,
                "hit@1": round(sum(1 for r in rows if r["rank"] == 1) / m, 3),
                "hit@5": round(sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 5) / m, 3),
                "hit@10": round(sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 10) / m, 3),
                "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / m, 3),
            }
        return kinds

    return {
        "config": config,
        "hit@1": hit_at(1),
        "hit@5": hit_at(5),
        "hit@10": hit_at(10),
        "mrr": round(sum(1 / r["rank"] for r in per_query if r["rank"]) / n, 3),
        "reach@5": reach_at(5),
        "reach@10": reach_at(10),
        "by_kind": by_kind(),
        "per_query": per_query,
    }


def _override_config(repo: Path, embed: dict) -> Path:
    """Scratch config for an A/B embed leg (issue #75): the repo's
    self-index profile with the embed keys overridden and its own state
    store, parked under <repo>/.tmp (gitignored, walker-excluded). The
    separate store is load-bearing: _check_model refuses to reuse a
    store stamped with a different embed model, so the qwen3 store and
    each jina variant keep disjoint vectors."""
    cfg = json.loads((repo / "config" / "neuronav.json").read_text(encoding="utf-8"))
    for key in ("embed_url", "embed_model", "embed_dim", "embed_provider",
                "embed_doc_prefix", "chunk_file_doc"):
        if embed.get(key) is not None:
            cfg[key] = embed[key]
    if embed.get("state_dir"):
        sd = Path(embed["state_dir"])
        cfg["state_dir"] = str(sd if sd.is_absolute() else repo / sd)
    out_dir = repo / ".tmp"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"bench-embed-{embed.get('tag', 'override')}.json"
    out.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8", newline="\n")
    return out

def _verify_store_vectors(navstore, navconfig, sample: int = 5) -> bool:
    """Issue #220 belt-and-braces: before measuring, re-embed a sample
    of stored documents and compare against the stored vectors. A
    coherent store returns ~1.0 cosine per doc (embedding inference is
    deterministic per text+model); a store whose vectors came from the
    OTHER embed mode — or any other path that swapped the vector space
    under sha-gating's feet — lands near 0 and would publish garbage
    hit rates. Refuse loudly, naming the store path and its stamp."""
    import math

    col = navstore._collection()
    if col.count() == 0:
        print(f"ERROR: store {navconfig.DB_DIR} is empty — rescan produced nothing "
              "to measure")
        return False
    got = col.get(limit=sample, include=["embeddings", "documents"])
    docs = list(got["documents"])
    texts = [navconfig.EMBED_DOC_PREFIX + d for d in docs] if navconfig.EMBED_DOC_PREFIX else docs
    fresh = navstore.embed(texts)

    def _cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    sims = [round(_cos(s, v), 4) for s, v in zip(got["embeddings"], fresh)]
    if min(sims) < 0.5:
        meta = col.metadata or {}
        print(f"ERROR: store {navconfig.DB_DIR} (embed_model={meta.get('embed_model')!r}, "
              f"embed_mode={meta.get('embed_mode')!r}, "
              f"embed_provider={meta.get('embed_provider')!r}) holds doc vectors "
              f"that do not match fresh embeds of the same text (sampled cosines "
              f"{sims}, expected ~1.0) — poisoned store (#220). Wipe it "
              "(delete the state dir or `python nav.py drop`) and rescan")
        return False
    return True


# Leaf singletons bound by _boot() (issue #379): the measured repo only
# lands on sys.path after the config pin, so the nav*/recall imports
# must stay lazy — entry-point bodies read them as module globals.
navconfig = navindex = navstore = recall = None
HAVE_RECALL = False


def _probe_url(embed_url: str) -> str:
    """Health-probe URL for an embed endpoint (issue #379): the openai
    wire (.../embeddings — navconfig's provider rule) answers GET
    itself; the ollama wire rides the sibling /api/tags listing.
    Derived from navconfig.EMBED_URL, the SAME endpoint config the
    store's embeds ride, so every entry point honors an embed_url
    sidecar or config override — no bench-side host can drift."""
    if embed_url.rstrip("/").endswith("/embeddings"):
        return embed_url
    return embed_url.rstrip("/").rsplit("/api/", 1)[0] + "/api/tags"


def _boot(repo: Path, embed: dict | None = None, fake: bool = False) -> int:
    """The one boot preamble every entry point rides (issue #379: was
    triplicated across run/sweep/ceiling_sweep with probe drift — run
    honored embed_url sidecars while the sweeps hardcoded 11434). Pins
    NEURONAV_CONFIG (an A/B leg gets its scratch profile), puts the
    measured repo first on sys.path, lazily imports the leaves above
    (recall presence -> HAVE_RECALL), probes the endpoint derived from
    the loaded config, verifies the golden set, wipes a scratch
    checkout's stale store in fake mode, rescans, and coherence-checks
    the store (#220). Returns 0 booted, 3 golden drift, 4 poisoned
    store. run()-only gates (configs needing recall, query_prefix)
    stay in run() and fire after boot."""
    global navconfig, navindex, navstore, recall, HAVE_RECALL

    if fake:
        os.environ["NEURONAV_EMBED_FAKE"] = "1"
    if embed:
        os.environ["NEURONAV_CONFIG"] = str(_override_config(repo, embed))
    else:
        os.environ["NEURONAV_CONFIG"] = str(repo / "config" / "neuronav.json")
    sys.path.insert(0, str(repo))

    import navconfig, navindex, navstore  # noqa: E402

    try:
        import recall  # noqa: E402

        HAVE_RECALL = True
    except ImportError:
        HAVE_RECALL = False

    if not fake:
        import httpx

        probe = _probe_url(navconfig.EMBED_URL)
        try:
            httpx.get(probe, timeout=10)
        except Exception as e:
            raise RuntimeError(f"real mode needs the embed endpoint at {probe}: {e}") from e

    if verify_golden(repo):
        return 3

    if fake and repo != DEFAULT_REPO:
        import shutil

        db = navconfig.STATE_DIR  # the worktree's OWN state store (<root>/.neuronav): a
        if db.is_dir():  # store would poison fake runs (sha-unchanged rescans skip
            shutil.rmtree(db)  # re-embed -> fake queries vs real docs). Real runs
            # keep the sha-incremental store: docs embed once, reruns only re-embed
            # queries, so Ollama fp jitter cannot shift document-side near-ties.

    # coherent index for this mode in this checkout's .neuronav; sha-incremental,
    # so real reruns and sweep/ceiling cells only re-embed queries
    stats = navindex.rescan()
    print(f"index: {navstore.count()} files (rescan {stats['added']}+/{stats['updated']}~/{stats['deleted']}-)")
    if not _verify_store_vectors(navstore, navconfig):
        return 4
    return 0


def run(repo: Path, set_name: str, configs: list[str], fake: bool,
        query_prefix: str | None = None, embed: dict | None = None) -> int:
    code = _boot(repo, embed, fake)
    if code:
        return code

    missing = [c for c in configs if NEEDS[c] and not HAVE_RECALL]
    if missing:
        print(f"ERROR: config(s) {missing} need recall.py, which this checkout lacks")
        return 2
    if query_prefix and not HAVE_RECALL:
        print("ERROR: query_prefix needs recall.py (vector-side prefixing)")
        return 2

    # effective embedded-query prefix for the record stamp (issue #217):
    # None = recall's shipped default (QUERY_PREFIX); "" = the explicit
    # raw leg (ab/jina); a literal = a forced leg.
    effective_prefix = (
        recall.QUERY_PREFIX if query_prefix is None else query_prefix
    ) if HAVE_RECALL else ""

    # vec/bm25/expand are BASELINE legs: since #228 the shipped default
    # carries λ 0.25, so they pin graph_boost=0.0 to keep measuring the
    # isolated sides; both/wfused/gb/twopass ride the default fusion
    # wire (gb still pins (λ, k) explicitly for record readability).
    def make(flags):
        def search(query: str):
            if HAVE_RECALL:
                kw = {"k": K, "bm25": "bm25" in flags, "expand": "expand" in flags}
                if not set(flags) & {"bm25", "expand", "gboost", "two_pass"}:
                    kw["graph_boost"] = 0.0
                if "weights" in flags:
                    kw["weights"] = WFUSED_WEIGHTS
                if "gboost" in flags:
                    kw["graph_boost"] = GB_LAMBDA
                    kw["rrf_k"] = GB_RRF_K
                if "two_pass" in flags:
                    kw["two_pass"] = True
                if query_prefix is not None:
                    kw["query_prefix"] = query_prefix
                return recall.search(query, **kw)
            return navstore.search(query, n=K)

        return search


    commit = _git(repo, "rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(_git(repo, "status", "--porcelain"))
    fp = _golden_fp()

    runs_dir = BENCH_DIR / "runs"
    runs_dir.mkdir(exist_ok=True)
    for config in configs:
        result = _run_config(repo, make(NEEDS[config]), config, _load_golden())

        record = {
            "set": set_name,
            "config": config,
            "commit": commit,
            "dirty": dirty,
            "mode": "fake" if fake else "real",
            "model": "hash-embed" if fake else navconfig.EMBED_MODEL,
            **({"query_prefix": effective_prefix} if effective_prefix else {}),
            **({"doc_prefix": navconfig.EMBED_DOC_PREFIX}
               if not fake and navconfig.EMBED_DOC_PREFIX else {}),
            **({"doc_shape": navstore.doc_shape()} if not fake else {}),
            "files": navstore.count(),
            "k": K,
            "golden": fp,
            **{kk: v for kk, v in result.items() if kk != "per_query"},
            "per_query": result["per_query"],
        }
        out = runs_dir / f"{set_name}-{config}.json"
        out.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(
            f"{set_name}/{config}: hit@1={result['hit@1']} hit@5={result['hit@5']} "
            f"hit@10={result['hit@10']} mrr={result['mrr']} "
            f"reach@5={result['reach@5']} reach@10={result['reach@10']}"
        )
    render()
    return 0


def sweep(repo: Path) -> int:
    """λ × RRF-k grid over the golden set (issue #73): `both` + the
    graph-neighbor boost at every (λ, k) cell, one rescan up front,
    deterministic grid order (λ outer, k inner). Real embeds only —
    the sweep arbitrates quality; fake mode is a plumbing battery, not
    a signal. Records land in bench/runs/sweep-*.json."""
    code = _boot(repo)
    if code:
        return code
    if not HAVE_RECALL:
        raise RuntimeError("sweep needs recall.py (the grid knobs live there)")

    queries = _load_golden()
    commit = _git(repo, "rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(_git(repo, "status", "--porcelain"))
    fp = _golden_fp()

    runs_dir = BENCH_DIR / "runs"
    runs_dir.mkdir(exist_ok=True)
    for lam in GB_LAMBDAS:
        for kk in GB_RRF_KS:
            name = f"gb{lam:g}-k{kk:g}"

            def search(query: str, _lam=lam, _kk=kk):
                return recall.search(query, k=K, graph_boost=_lam, rrf_k=_kk)

            result = _run_config(repo, search, name, queries)
            record = {
                "set": "sweep",
                "config": name,
                "commit": commit,
                "dirty": dirty,
                "mode": "real",
                "model": navconfig.EMBED_MODEL,
                "files": navstore.count(),
                "k": K,
                "golden": fp,
                "gb_lambda": lam,
                "gb_rrf_k": kk,
                **{key: v for key, v in result.items() if key != "per_query"},
                "per_query": result["per_query"],
            }
            out = runs_dir / f"sweep-{name}.json"
            out.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8", newline="\n")
            print(
                f"sweep/{name}: hit@1={result['hit@1']} hit@5={result['hit@5']} "
                f"hit@10={result['hit@10']} mrr={result['mrr']} "
                f"reach@5={result['reach@5']} reach@10={result['reach@10']}"
            )
    render()
    return 0

def ceiling_sweep(repo: Path) -> int:
    """Issue #228 grids on the golden set, real embeds, one rescan up
    front: exp 1 = graph-boost fusion λ × RRF-k × (vec, bm25) weights
    (set `gbw`), exp 2 = two-pass pool × budget × imports × pass-2
    weight (set `tps`). Both ride the shipped qprefix wire; the
    sha-incremental store means cells re-embed queries only.
    Deterministic grid order (λ outer, k, weights inner; pool outer,
    budget, imports, weight inner). Records land in
    bench/runs/gbw-*.json and tps-*.json, rendered into the issue-#228
    RESULTS.md section with the easy/hard split."""
    code = _boot(repo)
    if code:
        return code
    if not HAVE_RECALL:
        raise RuntimeError("ceiling sweep needs recall.py (the fusion/two-pass knobs live there)")

    queries = _load_golden()
    commit = _git(repo, "rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(_git(repo, "status", "--porcelain"))
    fp = _golden_fp()

    runs_dir = BENCH_DIR / "runs"
    runs_dir.mkdir(exist_ok=True)

    def measure(cell_set: str, name: str, search, extra: dict) -> None:
        result = _run_config(repo, search, name, queries)
        record = {
            "set": cell_set,
            "config": name,
            "commit": commit,
            "dirty": dirty,
            "mode": "real",
            "model": navconfig.EMBED_MODEL,
            "files": navstore.count(),
            "k": K,
            "golden": fp,
            **extra,
            **{key: v for key, v in result.items() if key != "per_query"},
            "per_query": result["per_query"],
        }
        (runs_dir / f"{cell_set}-{name}.json").write_text(
            json.dumps(record, indent=1) + "\n", encoding="utf-8", newline="\n"
        )
        print(
            f"{cell_set}/{name}: hit@1={result['hit@1']} hit@5={result['hit@5']} "
            f"hit@10={result['hit@10']} mrr={result['mrr']} "
            f"reach@5={result['reach@5']} reach@10={result['reach@10']}"
        )

    # exp 1 — λ × RRF-k × weights; λ=0 rows are the pure fusion sweep
    for lam in GBC_LAMBDAS:
        for kk in GBC_RRF_KS:
            for wv, wl in GBC_WEIGHTS:
                name = f"gb{lam:g}-k{kk:g}-w{wv:g}-{wl:g}"

                def search(query: str, _lam=lam, _kk=kk, _wv=wv, _wl=wl):
                    return recall.search(
                        query, k=K, graph_boost=_lam, rrf_k=_kk, weights=(_wv, _wl)
                    )

                measure(
                    "gbw", name, search,
                    {"gb_lambda": lam, "gb_rrf_k": kk, "gb_wv": wv, "gb_wl": wl},
                )

    # exp 2 — pool × budget × imports × pass-2 weight, boost OFF
    for pool in TPC_POOLS:
        for budget in TPC_BUDGETS:
            for imports in TPC_IMPORTS:
                for w2 in TPC_WEIGHTS:
                    name = f"p{pool}-b{budget}-i{int(imports)}-w{w2:g}"

                    def search(query: str, _pool=pool, _budget=budget,
                               _imports=imports, _w2=w2):
                        return recall.search(query, k=K, two_pass={
                            "pool": _pool, "budget": _budget,
                            "imports": _imports, "weight": _w2,
                        })

                    measure(
                        "tps", name, search,
                        {"tp_pool": pool, "tp_budget": budget,
                         "tp_imports": imports, "tp_weight": w2},
                    )
    render()
    return 0


def _records() -> dict[str, dict]:
    runs = BENCH_DIR / "runs"
    out: dict[str, dict] = {}
    if runs.is_dir():
        for p in sorted(runs.glob("*.json")):
            if p.name.startswith("agent_ab-"):
                continue  # agent-level A/B records (issue #72): own schema
                # and renderer (bench/agent_ab/run.py), not golden-set
                # evidence — must not trip the issue #104 stale-record gate
            rec = json.loads(p.read_text(encoding="utf-8"))
            out[f"{rec['set']}-{rec['config']}"] = rec
    return out


def _metrics_row(rec: dict) -> str:
    return (
        f"| {rec['config']} | {rec['hit@1']:.3f} | {rec['hit@5']:.3f} | {rec['hit@10']:.3f} "
        f"| {rec['mrr']:.3f} | {rec['reach@5']:.3f} | {rec['reach@10']:.3f} |"
    )


def _set_table(prefix: str, recs: dict[str, dict], note: str) -> list[str]:
    present = [recs[f"{prefix}-{c}"] for c in CONFIGS if f"{prefix}-{c}" in recs]
    if not present:
        return []
    head = present[0]
    lines = [
        f"### {note}",
        "",
        f"commit `{head['commit']}`{' (dirty tree)' if head['dirty'] else ''} · "
        f"mode **{head['mode']}** · model `{head['model']}` · {head['files']} indexed files · k={head['k']}",
        "",
        "| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [_metrics_row(r) for r in present]
    return lines

def _kind_table(prefix: str, recs: dict[str, dict]) -> list[str]:
    present = [c for c in CONFIGS if f"{prefix}-{c}" in recs]
    if not present:
        return []
    if "by_kind" not in recs[f"{prefix}-{present[0]}"]:
        return []  # pre-by_kind record: nothing to break down
    lines = [
        "by kind (hit@5 / MRR):",
        "",
        "| kind | n | " + " | ".join(present) + " |",
        "|---|---|" + "---|" * len(present),
    ]
    for kind in ("exact", "symbol", "prose", "cross"):
        cells = []
        for c in present:
            m = recs[f"{prefix}-{c}"]["by_kind"][kind]
            cells.append(f"{m['hit@5']:.3f} / {m['mrr']:.3f}")
        n = recs[f"{prefix}-{present[0]}"]["by_kind"][kind]["n"]
        lines.append(f"| {kind} | {n} | " + " | ".join(cells) + " |")
    return lines + [""]


def _per_query_table(prefix: str, recs: dict[str, dict]) -> list[str]:
    present = [c for c in CONFIGS if f"{prefix}-{c}" in recs]
    if not present:
        return []
    golden = _load_golden()
    by_q = {
        recs[f"{prefix}-{c}"]["config"]: {r["q"]: r for r in recs[f"{prefix}-{c}"]["per_query"]}
        for c in present
    }
    lines = [
        "<details><summary>per-query first-target rank (· = not in top-12; c = only via hop ctx)</summary>",
        "",
        "| query | kind | " + " | ".join(present) + " |",
        "|---|---|" + "---|" * len(present),
    ]
    for row in golden:
        cells = []
        for c in present:
            r = by_q[c][row["q"]]  # exact: _assert_records_current gates coverage (issue #104)
            cells.append("·" if r["rank"] is None else str(r["rank"]))
        lines.append(f"| `{row['q']}` | {row['kind']} | " + " | ".join(cells) + " |")
    lines += ["", "</details>", ""]
    return lines


def _sweep_table(recs: dict[str, dict]) -> list[str]:
    rows = [r for r in recs.values() if r.get("set") == "sweep"]
    if not rows:
        return []
    rows.sort(key=lambda r: (r["gb_lambda"], r["gb_rrf_k"]))
    head = rows[0]
    lines = [
        "### λ × RRF-k sweep — graph-neighbor rank boost (issue #73)",
        "",
        f"commit `{head['commit']}`{' (dirty tree)' if head['dirty'] else ''} · "
        f"mode **{head['mode']}** · model `{head['model']}` · {head['files']} indexed files · k={head['k']}",
        "",
        "Boost: each fused top-k source adds λ/(rrf_k+1)/(source rank) to every",
        "distinct 1-hop file neighbor (accumulated across sources; docs outside",
        "both rank lists enter with src=graph). Baseline row = `both` (λ 0) at the",
        "same commit and store as the winner. Deterministic grid, every cell",
        "double-run — wins inside the documented Ollama ±jitter are treated as",
        "ties.",
        "",
        "Grid measured at 2a1f231 with the RAW query (pre-#217 prefix",
        "cutover): the sweep arbitrates λ against its own both-baseline",
        "inside one store, so the cutover does not invalidate the grid,",
        "but sweep cells are not comparable to the prefixed after/gb",
        "rows. Verdict (re-swept at 2a1f231 on the 58-file index after the issue #75",
        "golden re-justify — the retired 44-file sweep at 2b9cbbe crowned the",
        "same cell): λ 0.25 @ rrf_k 30 sits in a three-cell top tier — hit@1",
        "0.560 here vs 0.600 at gb0.25-k60 and gb0.5-k30, a one-query gap well",
        "inside the documented jitter — and it carries the tier's best hit@10",
        "(0.920) with MRR 0.692 vs the k60 cell's 0.702. Every λ ≥ 1 loses",
        "monotonically (hub files crowd out precise matches). The tier held",
        "on one corpus, so #73 kept `recall.GRAPH_BOOST` at 0.0 — the",
        "opted-in `gb` config pinned λ 0.25 @ rrf_k 30. AMENDED by issue",
        "#228 (see the ceiling section below): the #228 4λ × 3k ×",
        "4-weights grid re-swept the question on the prefixed wire and",
        "cleared the census bar (hit@5 +0.08, MRR +0.075, double-run",
        "stable) — λ 0.25 @ rrf_k 30 is the shipped default since #228;",
        "the after-table `both` row now carries it. Cross-store deltas",
        "(across commits) carry ±jitter; the in-grid ceiling baseline",
        "gb0-k60-w1-1 vs the boosted cells is the attribution.",
        "",
        "| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    base = recs.get("after-both")
    if base:
        lines.append(_metrics_row(dict(base, config="both (λ=0)")))
    lines += [_metrics_row(r) for r in rows]
    return lines + [""]

CEILING_BASELINE = "gbw-gb0-k60-w1-1"  # the shipped `both` wire, measured in-grid


def _split_metrics(rec: dict, hard_qs: set[str]) -> tuple[float, float, int]:
    """(hit@5, MRR, n) over the hard subset — queries whose pass-1
    fused rank missed or landed beyond 5 in the in-grid baseline
    (RepoBench ICLR 2024 easy/hard split, issue #228)."""
    rows = [r for r in rec["per_query"] if r["q"] in hard_qs]
    n = len(rows)
    if not n:
        return 0.0, 0.0, 0
    hit5 = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 5) / n
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / n
    return round(hit5, 3), round(mrr, 3), n


def _ceiling_section(recs: dict[str, dict]) -> list[str]:
    """Issue #228 evidence section: the two census grids. Tables are
    record-derived; the verdict prose is appended below the measured
    numbers when the grids land."""
    gbw = [r for r in recs.values() if r.get("set") == "gbw"]
    tps = [r for r in recs.values() if r.get("set") == "tps"]
    if not gbw and not tps:
        return []
    out = [
        "## Recall ceiling push — census exp 1+2 (issue #228)",
        "",
        "Grids from `.team_scratch/paper_census.md` (Hydra arXiv 2602.11671;",
        "RepoBench ICLR 2024; RepoCoder EMNLP 2023). Both ride the shipped",
        "qprefix wire on the qwen3 store; win bar = hit@5 or MRR lift",
        "≥ +0.05 over the committed qprefix `both` row (0.520 / 0.880 / 0.960,",
        "MRR 0.672). Every cell double-run under the fp-jitter protocol.",
    ]
    if gbw:
        r0 = sorted(gbw, key=lambda r: (r["gb_lambda"], r["gb_rrf_k"], r["gb_wv"], r["gb_wl"]))
        out += [
            "",
            "### Exp 1 — graph-boost fusion grid (λ × RRF-k × vec/bm25 weights)",
            "",
            f"Set `gbw`, commit {r0[0]['commit']}{' (dirty)' if r0[0].get('dirty') else ''},"
            f" {r0[0]['mode']}, {r0[0]['model']}, {r0[0]['files']} files, k={r0[0]['k']}.",
            "λ=0 rows are the pure weights×k fusion sweep; `0 / 60 / (1, 1)` is",
            "the shipped `both` wire measured in-grid.",
            "",
            "| λ | rrf_k | weights | hit@1 | hit@5 | hit@10 | MRR |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in r0:
            out.append(
                f"| {r['gb_lambda']:g} | {r['gb_rrf_k']:g}"
                f" | ({r['gb_wv']:g}, {r['gb_wl']:g}) | {r['hit@1']:.3f}"
                f" | {r['hit@5']:.3f} | {r['hit@10']:.3f} | {r['mrr']:.3f} |"
            )
    if tps:
        t0 = sorted(tps, key=lambda r: (r["tp_pool"], r["tp_budget"], r["tp_imports"], r["tp_weight"]))
        base = recs.get(CEILING_BASELINE)
        hard_qs: set[str] = set()
        if base:
            hard_qs = {r["q"] for r in base["per_query"] if r["rank"] is None or r["rank"] > 5}
        n_all = len(base["per_query"]) if base else len(t0[0]["per_query"])
        out += [
            "",
            "### Exp 2 — two-pass RepoCoder loop grid (pool × budget × imports × pass-2 weight)",
            "",
            f"Set `tps`, commit {t0[0]['commit']}{' (dirty)' if t0[0].get('dirty') else ''},"
            " boost off (the two questions stay isolated). Hard split seeded by",
            f"the in-grid `both` baseline: hard = pass-1 rank miss or > 5 →"
            f" {len(hard_qs)} of {n_all} queries.",
            "",
            "| pool | budget | imports | w2 | hit@5 | MRR | hard h@5 | hard MRR |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in t0:
            h5, hm, _ = _split_metrics(r, hard_qs) if hard_qs else (0.0, 0.0, 0)
            out.append(
                f"| {r['tp_pool']} | {r['tp_budget']} | {int(r['tp_imports'])}"
                f" | {r['tp_weight']:g} | {r['hit@5']:.3f} | {r['mrr']:.3f}"
                f" | {h5:.3f} | {hm:.3f} |"
            )
        if base and hard_qs:
            bh5, bhm, bn = _split_metrics(base, hard_qs)
            out += [
                "",
                f"Baseline `both` on the same hard {bn}: hit@5 {bh5:.3f},"
                f" MRR {bhm:.3f} (0 by construction on hit@5 — hard is defined",
                "by that record's own rank > 5; MRR still credits rank 6–12).",
            ]
    # jitter audit + verdict: appended with the grids (issue #228) —
    # the numbers below are the double-run-verified cells; cells that
    # flipped across the double-run and never settled are excluded
    # (listed here) rather than committed as false precision.
    out += [
        "",
        "### Jitter audit (double-run gate)",
        "",
        "76 of 84 cells were byte-identical across the two full runs. 8",
        "cells flipped a ±1-rank near-tie (`_has_exact`, the agent-protocol",
        "prose query, `find_functions`, `sha256_of`, subsystem-names); a",
        "third pass settled tps-p12-b640-i1-w1 (kept, pass2 == pass3) and",
        "left 7 cells still flipping across every re-measure — dropped",
        "from `runs/` and excluded from arbitration, not reported as",
        "false precision: gbw-gb0.25-k30-w1-0.7 (both lines ≥ 0.92 h@5,",
        "0.738–0.741 MRR — would not change any verdict),",
        "gbw-gb0.75-k120-w0.7-1 (a losing λ anyway), and tps-p5/p12-",
        "b320-w1 / p5-b640 (all inside the settled b160/b640 tiers' band,",
        "±0.003 MRR). No winner cell and no baseline was unstable.",
        "",
        "### Verdict",
        "",
        "**Exp 1 — graph-boost fusion: WIN, shipped.** λ 0.25 @ rrf_k 30,",
        "weights (1, 1) lifts the in-grid baseline 0.520/0.920/0.960/MRR",
        "0.654 to 0.640/0.960/0.960/MRR 0.747 — +0.040 hit@1, +0.040",
        "hit@5, +0.093 MRR, double-run byte-stable, and it recovers one",
        "of the two hard queries. Against the committed qprefix `both` row",
        "(0.520/0.880/0.960/0.672, different store — cross-store deltas",
        "carry ±jitter) the same cell reads +0.120 hit@1 / +0.080 hit@5 /",
        "+0.075 MRR: both bars (hit@5, MRR) clear +0.05. The λ 0.25 tier",
        "from #73 holds at k30 on the prefixed wire; every λ ≥ 0.5 still",
        "loses monotonically (hub files crowd out precise matches).",
        "`recall.GRAPH_BOOST`/`recall.RRF_K` ship 0.25/30.0; `both` and",
        "`gb` are the same wire post-#228. The grid max λ 0.25 @ k30",
        "(0.7, 1) (0.680/0.960/0.960/0.777) exceeds the shipped cell by",
        "+0.030 MRR — a vec-downweight, sub-bar single cell, and in the",
        "jitter-excluded cell's own band; not shipped (same law as #73:",
        "no sub-bar single-cell ships).",
        "",
        "**Exp 2 — two-pass RepoCoder loop: NEGATIVE on its census win",
        "condition.** The hard split (2 of 25 queries: pass-1 rank miss or",
        "> 5 in the in-grid baseline) is where RepoCoder promised the",
        "lift; hard hit@5 stays 0.000 for every cell — no tuning of pool",
        "(3/5/12), budget (160/320/640), pass-2 weight (0.5/1.0) or",
        "imports recovers either hard query into the top 5 (hard MRR",
        "0.050–0.062 = ranks 8–12). The loop does lift easy-query ranks",
        "(b160 cells: 0.680 hit@1, MRR 0.765 vs baseline 0.520/0.654)",
        "but drops overall hit@5 to 0.840–0.880 vs 0.920 in-grid — the",
        "augmented query outranks the prose target's competitors on some",
        "easy queries. Budget is monotone-better as it shrinks (640 < 320",
        "< 160 — RepoBench's short-context prior transfers); pool and",
        "weight are flat. The `imports` axis is a measurement of a",
        "near-no-op: resolved from-imports already fold into the importer's",
        "surface via the graph's consts folding, so i0/i1 rows are",
        "byte-identical almost everywhere (5 residual names corpus-wide).",
        "TWO_PASS_* stays at #74 values; `two_pass={pool, budget,",
        "imports, weight}` tuning knobs ship for future sweeps, default",
        "off. Shipped as a negative result per bench law.",
    ]
    return out + [""]


AB_LEGS = [
    ("ab", "A/B baseline — qwen3-embedding:0.6b, raw query pinned (`query_prefix=''`) at the #217 cutover; same store as `qprefix`"),
    ("qprefix", "A/B leg 1 — qwen3 + the shipped nl2code query-instruction default (#217; these `both` numbers are the shipped baseline)"),
    ("jina", "A/B leg 2 — jina-code-embeddings-0.5b Q8_0 (llama-server `--pooling last`, openai wire), raw query (measured at 2a1f231, pre-#217)"),
    ("jinaq", "A/B leg 2b — jina + nl2code query instruction (same store as `jina`; measured at 2a1f231, pre-#217)"),
    ("jinap", "A/B leg 2c — jina paper recipe: query + `Candidate code snippet:` passage instruction at index time (fresh store; measured at 2a1f231, pre-#217)"),
]


def _ab_delta_table(recs: dict[str, dict]) -> list[str]:
    base = recs.get("ab-both")
    legs = [(s, recs[f"{s}-both"]) for s, _ in AB_LEGS[1:] if f"{s}-both" in recs]
    if not base or not legs:
        return []

    def pts(leg: dict, key: str) -> str:
        return f"{(leg[key] - base[key]) * 100:+.1f}"

    lines = [
        "Δ vs the `ab` baseline (`both` config), in points (1 pt = 0.010);",
        "the ab row shows absolutes, leg rows show deltas:",
        "",
        "| set | model | hit@1 | hit@5 | hit@10 | MRR |",
        "|---|---|---|---|---|---|",
        f"| ab | `{base['model']}` | {base['hit@1']:.3f} | {base['hit@5']:.3f} "
        f"| {base['hit@10']:.3f} | {base['mrr']:.3f} |",
    ]
    for name, r in legs:
        lines.append(
            f"| {name} | `{r['model']}` | {pts(r, 'hit@1')} | {pts(r, 'hit@5')} "
            f"| {pts(r, 'hit@10')} | {pts(r, 'mrr')} |"
        )
    return lines + [""]


def _ab_section(recs: dict[str, dict]) -> list[str]:
    present = [s for s, _ in AB_LEGS if any(f"{s}-{c}" in recs for c in CONFIGS)]
    if not present:
        return []  # no A/B records committed yet (or hermetic sandbox)
    lines = [
        "## Embedding A/B (issue #75)",
        "",
        "Question: does jina-code-embeddings-0.5b (JCE, arXiv 2508.21290) beat the",
        "shipped qwen3-embedding:0.6b on this golden set by the ≥ +3-point margin",
        "the paper's 25-task aggregate suggests (78.41 vs 73.49 overall)? JCE Q8_0",
        "(official jinaai GGUF) is served by llama-server with the card's",
        "`--pooling last` contract on the #17 openai wire — Ollama imports the",
        "same GGUF as a completion model (no pooling metadata) and refuses",
        "`/api/embed`, so the A/B needed a sidecar server, not a provider swap.",
        "Every leg is real embeds, double-run, on its own state store: `ab`/",
        "`qprefix` share the qwen3 store (prefixes are query-side only, no",
        "re-index), `jina`/`jinaq` share the JCE store, `jinap` re-indexes with",
        "the passage instruction prepended to embedded docs (stored documents",
        "stay raw — the prefix is an embed-input transform). Records stamp",
        "the effective `query_prefix`/`doc_prefix`. Since #217 the qprefix",
        "wire is the shipped recall default and `ab` pins `query_prefix=''`",
        "as the raw-query baseline; the jina rows stay as measured at",
        "2a1f231 (pre-#217) — re-running them needs the llama-server",
        "sidecar, not the cutover. Same-store legs are",
        "the attribution unit; cross-store deltas ride the double-run floors",
        "below.",
        "",
    ]
    for prefix, note in AB_LEGS:
        chunk = _set_table(prefix, recs, note)
        if chunk:
            lines += chunk + [""]
        lines += _kind_table(prefix, recs)
    lines += _ab_delta_table(recs)
    lines += [
        "Verdict — two rounds, each double-run (every metric line identical",
        "across passes; the Ollama fp-jitter flipped nothing in either round).",
        "Model swap (round 1, measured at 2a1f231): FAILS the ≥ +3-point win",
        "condition on the shipped `both` config — plain jina loses hit@5 by",
        "12.0 pts (0.72 vs 0.84), jinaq by 16.0, and the full paper recipe",
        "jinap still trails hit@5 by 4.0 (0.80 vs 0.84) despite winning hit@1",
        "(+20.0) and MRR (+12.5); the vec-only rows show the same shape",
        "(jina/vec hit@5 0.44 vs ab/vec 0.52), so the paper's aggregate edge",
        "does not transfer to whole-file retrieval on this corpus at Q8_0.",
        "qwen3-embedding:0.6b stays; jinap topping the default-off `gb` column",
        "(0.72/0.96, MRR 0.817) is noted, not shipped. Query prefix (round 2,",
        "measured at the #217 cutover): the free leg wins again, same qwen3",
        "model, fresh store — the raw-query `ab` baseline runs 0.36/0.76/0.88",
        "with MRR 0.509 (hit@5 sits 8 pts under round 1's 0.84 on the same",
        "wire: the corpus moved under the v0.1.2 merge, not the retrieval), and",
        "the shipped prefix lifts `both` to 0.52/0.88/0.96 with MRR 0.672 —",
        "+16.0 hit@1 / +12.0 hit@5 / +8.0 hit@10 / +16.3 MRR — plus `gb` to",
        "0.64/0.92/0.96 (MRR 0.747) and `twopass` to 0.64/0.88/0.92 (MRR",
        "0.746). `after` is byte-identical to `qprefix` on every config: the",
        "shipped default wire IS the measured leg. #217 ships the prefix.",
        "",
    ]
    return lines



CAST_LEGS = [
    ("castq", "cast leg — cAST file docs (chunk_file_doc=1.0, the shipped default; default wire, default store)"),
    ("rawq", "raw leg — raw file docs (chunk_file_doc=0, own .tmp store; the pre-#229 surface at the same commit)"),
]


def _cast_delta_table(recs: dict[str, dict]) -> list[str]:
    base = recs.get("qprefix-both")
    legs = [(s, recs[f"{s}-both"]) for s, _ in CAST_LEGS if f"{s}-both" in recs]
    if not base or not legs:
        return []

    def pts(leg: dict, key: str) -> str:
        return f"{(leg[key] - base[key]) * 100:+.1f}"

    return [
        "Δ vs the committed `qprefix` baseline (`both` config, raw file docs",
        "at c0343ab), in points (1 pt = 0.010):",
        "",
        "| set | docs | hit@1 | hit@5 | hit@10 | MRR |",
        "|---|---|---|---|---|---|",
        f"| qprefix (baseline) | raw | {base['hit@1']:.3f} | {base['hit@5']:.3f} "
        f"| {base['hit@10']:.3f} | {base['mrr']:.3f} |",
    ] + [
        f"| {name} | {r.get('doc_shape', 'raw')} | {pts(r, 'hit@1')} | "
        f"{pts(r, 'hit@5')} | {pts(r, 'hit@10')} | {pts(r, 'mrr')} |"
        for name, r in legs
    ] + [""]


def _cast_section(recs: dict[str, dict]) -> list[str]:
    present = [s for s, _ in CAST_LEGS if any(f"{s}-{c}" in recs for c in CONFIGS)]
    if not present:
        return []  # no #229 records committed yet (or hermetic sandbox)
    lines = [
        "## cAST file-doc shaping (issue #229)",
        "",
        "Question: do size-aware, signature-first FILE docs (cAST 2025 — merge",
        "micro-fns into their carrier, split monsters at block boundaries, keep",
        "every chunk signature-first) lift recall where the fn-layer chunking",
        "(#141) could not? The fn collection is invisible to `recall.search` —",
        "the file collection is what the vector side queries — so #229 shapes",
        "the docs nav embeds per file instead. Self-index calibration: 12/58",
        "files exceed the 30k embed cap (viz.py 462k → 6.5% visible; server.py,",
        "nav.py, graph.py all >50k) and fn bodies sit at p50=547 chars with 28%",
        "micro / 15% monster — the #76 thresholds (220/2000) already match the",
        "p25/p90 boundaries, so the file layer reuses them. The head carries the",
        "path, class/extends, the full symbol surface (capped, `(+N)` tail), and",
        "the module intro; sections flatten (chunk index, source line) so chunk 1",
        "of every fn embeds before chunk 2 of any fn; the whole doc assembles",
        "under the 30k cap. Store lineage rides the #220 law extended to doc",
        "construction: the `doc_shape` stamp (cast<rev>@<scale>) forces a loud",
        "full re-embed on shape flips — sha-gating alone would serve stale",
        "vectors built from the other shape. Doc count is unchanged (one doc",
        "per file; the shaping rewrites the doc text, not the id grammar) —",
        "the ≤2x index-growth budget holds trivially at 1.0x.",
        "",
    ]
    for prefix, note in CAST_LEGS:
        chunk = _set_table(prefix, recs, note)
        if chunk:
            lines += chunk + [""]
        lines += _kind_table(prefix, recs)
    lines += _cast_delta_table(recs)
    lines += [
        "Verdict (double-run fp-jitter protocol, both runs agree on hit@5",
        "for every config on both legs; jitter only wobbles hit@1 by one",
        "rank-1/2 flip and MRR by ≤0.02): the committed `qprefix` baseline",
        "moves 0.88 → **0.92 hit@5** on `both` (+4.0 pts, MRR 0.672 →",
        "0.727/0.747) and 0.88 → 0.92 on `twopass`; `gb` rides 0.92 → 0.96.",
        "The isolated doc-shape effect is the vector leg: castq `vec` hits",
        "0.80 vs rawq 0.56 (+24 pts) — raw docs at this commit truncate the",
        "enlarged nav.py/graph.py even harder than at c0343ab (qprefix `vec`",
        "was 0.60), while the shaped docs keep every file under the 30k cap",
        "with its symbol surface in the head. The same-commit control also",
        "separates corpus drift from shaping: rawq `both` measures 0.92 too,",
        "and the `bm25` leg reads 0.92 vs 0.88 at c0343ab although its",
        "vector side got no better — the BM25F corpus (graph FileSym",
        "fields, doc-shape-independent by construction) drifted with the",
        "enlarged files, so the +4 vs the",
        "committed baseline conflates the two; at the fused level the shape",
        "contribution lands in MRR (0.727/0.747 vs 0.654) and hit@1",
        "(0.56/0.60 vs 0.52), and on `twopass` hit@5 (+8 pts: 0.92 vs 0.84).",
        "Per-query: the two 30k-truncation victims recover on the vector leg",
        "(`import_base` ∅→6, the agent-protocol query ∅→in), leaving",
        "`sync_functions` (6) and `FileSym` (∅) as the residual `both`",
        "misses. Index growth 1.0x (58 docs, one per file) — inside the ≤2x",
        "cAST budget by construction.",
        "",
    ]
    return lines

def _assert_records_current(recs: dict[str, dict]) -> None:
    """Loud coherence gate (issue #104): a golden swap without re-running
    left records whose per_query rows no longer matched the golden set —
    render KeyErrored on them, then silently drew '?' cells. Refuse to
    render mixed evidence; name every stale record."""
    fp = _golden_fp()
    stale = [f"{key}.json" for key in sorted(recs) if recs[key].get("golden") != fp]
    if stale:
        raise RuntimeError(
            f"stale bench records vs the current golden set (issue #104): {', '.join(stale)}\n"
            "these were measured against a different (or unfingerprinted) golden set.\n"
            "Re-run each stale set on the current golden — see the Rerun section of\n"
            "bench/RESULTS.md — then render; refusing to mix evidence."
        )


def render() -> None:
    recs = _records()
    _assert_records_current(recs)
    lines = [
        "# Recall benchmark — neuronav self-index",
        "",
        "Golden set: 25 queries (`bench/golden.json`) over this repo's `.py` files;",
        "every target justified by a code anchor (verified by `--verify-only`).",
        "hit@k = any golden target in the top-k ranked files; MRR over first-target",
        "rank; reach@k additionally credits a target appearing in the 1-hop ctx of a",
        "top-k hit (0 for configs without expansion). Metrics are rank-derived, so",
        "FAKE-mode reruns are byte-identical. Real-mode reruns embed queries fresh",
        "each time: Ollama fp non-determinism can flip a near-tie — observed once",
        "on the baseline (hit@5 0.72 vs 0.76, MRR ±0.005, one query); docs embed",
        "once (sha-incremental store), so document-side ranks stay fixed. Documented",
        "±jitter is the ceiling; all committed records below were double-run. Every",
        "record stamps the golden-set fingerprint it was measured against; render",
        "refuses to mix in records from a different golden set (issue #104).",
        "",
        "Configs: `vec` = cosine only · `bm25` = +BM25F reciprocal-rank fusion ·",
        "`expand` = +bidirectional 1-hop ctx · `both` = the shipped default",
        "(since #228 that includes the graph-neighbor boost λ 0.25 @ rrf_k",
        "30 — see the ceiling section) · `wfused` = `both` with weighted RRF",
        "(vec 1.0 / bm25 0.7) instead of the pinned unweighted k=30 · `gb` =",
        "the boost pinned explicitly (same wire as `both` post-#228) ·",
        "`twopass` = `both` + the deterministic second retrieve (issue #74:",
        "pass-1 lexical top hits donate their identifier surface to the",
        "re-embedded augmented query, 2 embeds/query). Pre-#228 `after`",
        "records measured the unboosted default; the #228 cutover re-ran",
        "the set on the shipped wire.",
        "",
    ]
    sets = [
        ("after", "After — current main (nl2code query prefix default-on per #217; graph-boost λ 0.25 @ rrf_k 30 default-on per #228; two-pass in `twopass`)"),
        ("fake", "FAKE mode — `NEURONAV_EMBED_FAKE=1` plumbing battery"),
    ]
    retired = [
        "## Retired evidence (issue #104)",
        "",
        "The pre-boost `before` set (merge-base 63b6f1f) and the two-pass `tp` set",
        "(`feat/two-pass-recall` head 62ef727) were measured against a golden set",
        "whose seeded-rng query targeted `viz.py`. Issue #86 moved that code to",
        "`layout.py`, so `--verify-only` fails at those commits and the sets cannot",
        "be re-run coherently — their records were dropped rather than kept stale.",
        "The verdicts they justified (boost default-off, two-pass plumbed behind a",
        "flag) are merged; the historical tables live in git history, and the",
        "ablation rows (vec/bm25/expand/both) are re-measured at the current",
        "commit inside the After table below.",
        "",
    ]
    for prefix, note in sets:
        chunk = _set_table(prefix, recs, note)
        if chunk:
            lines += chunk + [""]
        lines += _kind_table(prefix, recs)
    lines += _ab_section(recs)
    lines += _cast_section(recs)
    lines += retired
    lines += _sweep_table(recs)
    lines += _ceiling_section(recs)
    lines += _per_query_table("after", recs)
    lines += [
        "## Rerun",
        "",
        "```",
        "git worktree add --detach ../bench-measure <commit>",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set sweep --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set ceiling --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set ab --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set qprefix --repo ../bench-measure",
        "# issue #229 doc-shape legs: castq re-embeds the default store once",
        "# (the doc_shape stamp heals), rawq builds its own .tmp store:",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set castq --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set rawq --repo ../bench-measure",
        "# JCE legs: serve the official jinaai Q8_0 GGUF first (Ollama imports",
        "# it as a completion model — /api/embed refuses the unpooled GGUF):",
        "llama-server -m jina-code-embeddings-0.5b-Q8_0.gguf --embeddings --pooling last --host 127.0.0.1 --port 18081 -c 32768",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set jina --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set jinaq --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set jinap --repo ../bench-measure",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake --repo ../bench-measure",
        "```",
        "",
        "All sets are measured in a detached worktree (`git worktree add --detach",
        "<dir> <commit>`), with its own `.neuronav/` state store, so the live shared",
        "index is never touched and attribution is by commit. Ordering: run real sets",
        "first, fake last — fake mode wipes the worktree store for embed-mode",
        "coherence, and a real run after it would embed queries against sha-equal",
        "fake docs. A/B legs (issue #75): run `ab` (pins `query_prefix=''`) then",
        "`qprefix` (the #217 shipped default) first, both on the qwen3",
        "store, then the jina legs (their own `.tmp/` stores); `jinap` re-embeds",
        "the corpus with the passage instruction, the others reuse it. Records",
        "carry a golden fingerprint; a golden edit without a",
        "re-run makes `--render-only` fail loudly naming the stale records.",
    ]
    (BENCH_DIR / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    scratch = DEFAULT_REPO / ".team_scratch" / "bench"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"rendered {BENCH_DIR / 'RESULTS.md'} ({len(recs)} records)")


def main() -> int:
    ap = argparse.ArgumentParser(description="recall benchmark over the self-index")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--set", choices=["after", "fake", "sweep", "ceiling", *SET_FLAGS],
                    default="after")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    if args.verify_only:
        return verify_golden(repo)
    if args.render_only:
        render()
        return 0

    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    bad = [c for c in configs if c not in CONFIGS]
    if bad:
        print(f"ERROR: unknown configs {bad}")
        return 2
    fake = args.fake or args.set == "fake"
    if fake and repo == DEFAULT_REPO:
        print(
            "ERROR: fake mode must run in a detached worktree (--repo): the main\n"
            "checkout's .neuronav store holds real embeddings and a fake rescan would\n"
            "leave them (sha-unchanged) -> fake query vectors vs real docs."
        )
        return 2
    if args.set == "sweep":
        return sweep(repo)
    if args.set == "ceiling":
        return ceiling_sweep(repo)
    flags = SET_FLAGS.get(args.set, {})
    if flags and fake:
        print("ERROR: A/B sets are real-embed legs (no --fake)")
        return 2
    return run(repo, args.set, configs, fake,
               flags.get("query_prefix"), flags.get("embed"))


if __name__ == "__main__":
    sys.exit(main())
