"""bench/run_bench.py — recall-quality benchmark over the neuronav self-index.

Measures 4 configs x the golden query set (bench/golden.json):

  vec      recall.search(bm25=False, expand=False)   baseline cosine
  bm25     recall.search(bm25=True,  expand=False)   +BM25F fusion (RRF)
  expand   recall.search(bm25=False, expand=True)    +1-hop graph ctx on hits
  both     recall.search(bm25=True,  expand=True)    the shipped default

Config switching is INTERNAL (kwargs on recall.search; falls back to plain
nav.search where recall.py does not exist yet, e.g. the pre-fusion baseline
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
K = 12  # contract: nav.search(q, k=12)
CONFIGS = ("vec", "bm25", "expand", "both", "wfused", "gb", "twopass")
NEEDS = {"vec": (), "bm25": ("bm25",), "expand": ("expand",), "both": ("bm25", "expand"), "wfused": ("bm25", "expand", "weights"), "gb": ("bm25", "expand", "gboost"), "twopass": ("bm25", "expand", "two_pass")}
WFUSED_WEIGHTS = (1.0, 0.7)  # (vec, bm25) — Main-pinned weighted fusion vs unweighted RRF k=60
# graph-neighbor boost (issue #73): gb = both + boost at the pinned
# winner below. λ multiplies the RRF unit 1/(rrf_k+1); each fused top-k
# source adds λ·unit/(source rank) to every distinct 1-hop file
# neighbor. Winner picked from the deterministic GB_LAMBDAS × GB_RRF_KS
# sweep (--set sweep) on the golden set, real embeds, double-run —
# numbers in bench/RESULTS.md.
GB_LAMBDA = 0.25  # swept winner: λ 0.25 @ rrf_k 30 (h1 +0.08 vs λ=0, double-run stable)
GB_RRF_K = 30.0  # every λ ≥ 0.5 lost to plain fusion; GRAPH_BOOST default stays 0.0
GB_LAMBDAS = (0.0, 0.25, 0.5, 1.0, 2.0)
GB_RRF_KS = (30.0, 60.0, 120.0)
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

def _verify_store_vectors(nav, sample: int = 5) -> bool:
    """Issue #220 belt-and-braces: before measuring, re-embed a sample
    of stored documents and compare against the stored vectors. A
    coherent store returns ~1.0 cosine per doc (embedding inference is
    deterministic per text+model); a store whose vectors came from the
    OTHER embed mode — or any other path that swapped the vector space
    under sha-gating's feet — lands near 0 and would publish garbage
    hit rates. Refuse loudly, naming the store path and its stamp."""
    import math

    col = nav._collection()
    if col.count() == 0:
        print(f"ERROR: store {nav.DB_DIR} is empty — rescan produced nothing "
              "to measure")
        return False
    got = col.get(limit=sample, include=["embeddings", "documents"])
    docs = list(got["documents"])
    texts = [nav.EMBED_DOC_PREFIX + d for d in docs] if nav.EMBED_DOC_PREFIX else docs
    fresh = nav.embed(texts)

    def _cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    sims = [round(_cos(s, v), 4) for s, v in zip(got["embeddings"], fresh)]
    if min(sims) < 0.5:
        meta = col.metadata or {}
        print(f"ERROR: store {nav.DB_DIR} (embed_model={meta.get('embed_model')!r}, "
              f"embed_mode={meta.get('embed_mode')!r}, "
              f"embed_provider={meta.get('embed_provider')!r}) holds doc vectors "
              f"that do not match fresh embeds of the same text (sampled cosines "
              f"{sims}, expected ~1.0) — poisoned store (#220). Wipe it "
              "(delete the state dir or `python nav.py drop`) and rescan")
        return False
    return True


def run(repo: Path, set_name: str, configs: list[str], fake: bool,
        query_prefix: str | None = None, embed: dict | None = None) -> int:
    if fake:
        os.environ["NEURONAV_EMBED_FAKE"] = "1"
    if embed:
        os.environ["NEURONAV_CONFIG"] = str(_override_config(repo, embed))
    else:
        os.environ["NEURONAV_CONFIG"] = str(repo / "config" / "neuronav.json")
    sys.path.insert(0, str(repo))

    import nav  # noqa: E402  (binds the self-index profile via NEURONAV_CONFIG)

    try:
        import recall  # noqa: E402

        have_recall = True
    except ImportError:
        have_recall = False

    missing = [c for c in configs if NEEDS[c] and not have_recall]
    if missing:
        print(f"ERROR: config(s) {missing} need recall.py, which this checkout lacks")
        return 2
    if query_prefix and not have_recall:
        print("ERROR: query_prefix needs recall.py (vector-side prefixing)")
        return 2

    # effective embedded-query prefix for the record stamp (issue #217):
    # None = recall's shipped default (QUERY_PREFIX); "" = the explicit
    # raw leg (ab/jina); a literal = a forced leg.
    effective_prefix = (
        recall.QUERY_PREFIX if query_prefix is None else query_prefix
    ) if have_recall else ""

    if not fake:
        import httpx

        probe = (embed or {}).get("embed_url",
                                  "http://127.0.0.1:11434/api/tags")
        try:
            httpx.get(probe, timeout=10)
        except Exception as e:
            raise RuntimeError(f"real mode needs the embed endpoint at {probe}: {e}") from e

    if verify_golden(repo):
        return 3

    if fake and repo != DEFAULT_REPO:
        import shutil

        db = nav.STATE_DIR  # the worktree's OWN state store (<root>/.neuronav): a
        if db.is_dir():  # store would poison fake runs (sha-unchanged rescans skip
            shutil.rmtree(db)  # re-embed -> fake queries vs real docs). Real runs
            # keep the sha-incremental store: docs embed once, reruns only re-embed
            # queries, so Ollama fp jitter cannot shift document-side near-ties.

    stats = nav.rescan()  # coherent index for this mode in this checkout's .neuronav
    print(f"index: {nav.count()} files (rescan {stats['added']}+/{stats['updated']}~/{stats['deleted']}-)")
    if not _verify_store_vectors(nav):
        return 4

    def make(flags):
        def search(query: str):
            if have_recall:
                kw = {"k": K, "bm25": "bm25" in flags, "expand": "expand" in flags}
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
            return nav.search(query, n=K)

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
            "model": "hash-embed" if fake else nav.EMBED_MODEL,
            **({"query_prefix": effective_prefix} if effective_prefix else {}),
            **({"doc_prefix": nav.EMBED_DOC_PREFIX}
               if not fake and nav.EMBED_DOC_PREFIX else {}),
            **({"doc_shape": nav.doc_shape()} if not fake else {}),
            "files": nav.count(),
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
    os.environ["NEURONAV_CONFIG"] = str(repo / "config" / "neuronav.json")
    sys.path.insert(0, str(repo))

    import nav  # noqa: E402  (binds the self-index profile via NEURONAV_CONFIG)
    import recall  # noqa: E402

    import httpx

    try:
        httpx.get("http://127.0.0.1:11434/api/tags", timeout=10)
    except Exception as e:
        raise RuntimeError(f"sweep needs Ollama on 11434: {e}") from e

    if verify_golden(repo):
        return 3

    stats = nav.rescan()  # sha-incremental: docs embed once, cells only re-embed queries
    print(f"index: {nav.count()} files (rescan {stats['added']}+/{stats['updated']}~/{stats['deleted']}-)")
    if not _verify_store_vectors(nav):
        return 4

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
                "model": nav.EMBED_MODEL,
                "files": nav.count(),
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


def _records() -> dict[str, dict]:
    runs = BENCH_DIR / "runs"
    out: dict[str, dict] = {}
    if runs.is_dir():
        for p in sorted(runs.glob("*.json")):
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
        "monotonically (hub files crowd out precise matches). The win stays a",
        "single-cell-tier result on one corpus, so `recall.GRAPH_BOOST` stays",
        "0.0 — default-off — and the `gb` config keeps pinning λ 0.25 @",
        "rrf_k 30 for opted-in evaluation; the after-table `gb` row pins it.",
        "Cross-store deltas (across commits) carry ±jitter; the same-store `gb`",
        "vs `both` rows are the attribution.",
        "",
        "| config | hit@1 | hit@5 | hit@10 | MRR | reach@5 | reach@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    base = recs.get("after-both")
    if base:
        lines.append(_metrics_row(dict(base, config="both (λ=0)")))
    lines += [_metrics_row(r) for r in rows]
    return lines + [""]

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
        "carried by the lexical side (`bm25` 0.92 on untouched lexical",
        "machinery vs 0.88 at c0343ab — the corpus grew), so the +4 vs the",
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
        "`expand` = +bidirectional 1-hop ctx · `both` = the shipped default ·",
        "`wfused` = `both` with weighted RRF (vec 1.0 / bm25 0.7) instead of the",
        "pinned unweighted k=60 · `gb` = `both` + the swept graph-neighbor",
        "boost (λ winner, see the λ × RRF-k sweep section) · `twopass` =",
        "`both` + the deterministic second retrieve (issue #74: pass-1",
        "lexical top hits donate their identifier surface to the",
        "re-embedded augmented query, 2 embeds/query).",
        "",
    ]
    sets = [
        ("after", "After — current main (nl2code query prefix default-on per #217; graph-boost winner in `gb`, two-pass in `twopass`)"),
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
    lines += _per_query_table("after", recs)
    lines += [
        "## Rerun",
        "",
        "```",
        "git worktree add --detach ../bench-measure <commit>",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set sweep --repo ../bench-measure",
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
    ap.add_argument("--set", choices=["after", "fake", "sweep", *SET_FLAGS],
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
    flags = SET_FLAGS.get(args.set, {})
    if flags and fake:
        print("ERROR: A/B sets are real-embed legs (no --fake)")
        return 2
    return run(repo, args.set, configs, fake,
               flags.get("query_prefix"), flags.get("embed"))


if __name__ == "__main__":
    sys.exit(main())
