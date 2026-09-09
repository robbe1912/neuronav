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
config-leakage trap). Each measured checkout gets its own .chroma (DB_DIR is
checkout-relative), so runs never touch the live shared index.

Runs are recorded as bench/runs/<set>-<config>.json (rank-level evidence,
not raw scores); RESULTS.md is re-rendered from all records, so reruns with
identical metrics produce byte-identical output.

Usage:
  .venv/Scripts/python.exe -X utf8 bench/run_bench.py --set before --configs vec
  .venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake
  .venv/Scripts/python.exe -X utf8 bench/run_bench.py --verify-only

--repo PATH  checkout to measure (default: this script's repo). Point it at
             a detached worktree for before/after attribution by commit.
--set NAME   record set: before | after | fake (fake implies CI plumbing mode).
--fake       NEURONAV_EMBED_FAKE=1 (deterministic hash embeddings; coherent
             only against a collection built in the same mode — use a fresh
             checkout/.chroma per mode).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = BENCH_DIR.parent
K = 12  # contract: nav.search(q, k=12)
CONFIGS = ("vec", "bm25", "expand", "both")
NEEDS = {"vec": (), "bm25": ("bm25",), "expand": ("expand",), "both": ("bm25", "expand")}


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


def verify_golden(repo: Path) -> int:
    """Every target must still contain its anchor substring — the mechanical
    justification of the golden set. Fails loudly on drift."""
    fails = 0
    for row in _load_golden():
        anchor = row["anchor"]
        anchors = anchor if isinstance(anchor, list) else [anchor] * len(row["targets"])
        for tgt, a in zip(row["targets"], anchors):
            path = repo / tgt
            if not path.is_file():
                print(f"FAIL  target missing: {tgt} ({row['q']})")
                fails += 1
                continue
            if a not in path.read_text(encoding="utf-8", errors="replace"):
                print(f"FAIL  anchor '{a}' not in {tgt} ({row['q']})")
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
    flags = NEEDS[config]
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

    return {
        "config": config,
        "hit@1": hit_at(1),
        "hit@5": hit_at(5),
        "hit@10": hit_at(10),
        "mrr": round(sum(1 / r["rank"] for r in per_query if r["rank"]) / n, 3),
        "reach@5": reach_at(5),
        "reach@10": reach_at(10),
        "per_query": per_query,
    }


def run(repo: Path, set_name: str, configs: list[str], fake: bool) -> int:
    if fake:
        os.environ["NEURONAV_EMBED_FAKE"] = "1"
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

    if not fake:
        import httpx

        try:
            httpx.get("http://127.0.0.1:11434/api/tags", timeout=10)
        except Exception as e:
            raise RuntimeError(f"real mode needs Ollama on 11434: {e}") from e

    if verify_golden(repo):
        return 3

    stats = nav.rescan()  # coherent index for this mode in this checkout's .chroma
    print(f"index: {nav.count()} files (rescan {stats['added']}+/{stats['updated']}~/{stats['deleted']}-)")

    def make(flags):
        def search(query: str):
            if have_recall:
                return recall.search(query, k=K, bm25="bm25" in flags, expand="expand" in flags)
            return nav.search(query, n=K)

        return search

    commit = _git(repo, "rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(_git(repo, "status", "--porcelain"))

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
            "files": nav.count(),
            "k": K,
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
            r = by_q[c][row["q"]]
            cells.append("·" if r["rank"] is None else str(r["rank"]))
        lines.append(f"| `{row['q']}` | {row['kind']} | " + " | ".join(cells) + " |")
    lines += ["", "</details>", ""]
    return lines


def render() -> None:
    recs = _records()
    lines = [
        "# Recall benchmark — neuronav self-index",
        "",
        "Golden set: 25 queries (`bench/golden.json`) over this repo's `.py` files;",
        "every target justified by a code anchor (verified by `--verify-only`).",
        "hit@k = any golden target in the top-k ranked files; MRR over first-target",
        "rank; reach@k additionally credits a target appearing in the 1-hop ctx of a",
        "top-k hit (0 for configs without expansion). Metrics are rank-derived, so",
        "reruns are byte-stable unless ranking changes.",
        "",
        "Configs: `vec` = cosine only · `bm25` = +BM25F reciprocal-rank fusion ·",
        "`expand` = +bidirectional 1-hop ctx · `both` = the shipped default.",
        "",
    ]
    real_rows = _set_table("before", recs, "Before — pre-fusion baseline (27c437b era, `nav.search`)")
    after_rows = _set_table("after", recs, "After — recall-hybrid (`recall.search`)")
    fake_rows = _set_table("fake", recs, "FAKE mode — `NEURONAV_EMBED_FAKE=1` plumbing battery")
    for chunk in (real_rows, after_rows, fake_rows):
        if chunk:
            lines += chunk + [""]
    lines += _per_query_table("after", recs)
    lines += _per_query_table("before", recs)
    lines += [
        "## Rerun",
        "",
        "```",
        "git worktree add --detach ../bench-before 27c437b",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set before --configs vec --repo ../bench-before",
        "git worktree add --detach ../bench-after <after-commit>",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set after --repo ../bench-after",
        ".venv/Scripts/python.exe -X utf8 bench/run_bench.py --set fake --fake --repo ../bench-after",
        "```",
        "",
        "Before/after are measured in detached worktrees (`git worktree add --detach",
        "<dir> <commit>`), each with its own `.chroma`, so the live shared index is",
        "never touched and attribution is by commit.",
    ]
    (BENCH_DIR / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    scratch = DEFAULT_REPO / ".team_scratch" / "bench"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"rendered {BENCH_DIR / 'RESULTS.md'} ({len(recs)} records)")


def main() -> int:
    ap = argparse.ArgumentParser(description="recall benchmark over the self-index")
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--set", choices=["before", "after", "fake"], default="after")
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
            "checkout's .chroma holds real embeddings and a fake rescan would\n"
            "leave them (sha-unchanged) -> fake query vectors vs real docs."
        )
        return 2
    return run(repo, args.set, configs, fake)


if __name__ == "__main__":
    sys.exit(main())
