"""agent-level A/B harness — entry (issue #72). See README.md beside this
file for the design contract. Typical run (self-index, real embeds):

    .venv/Scripts/python.exe -X utf8 bench/agent_ab/run.py

Every invocation runs the battery TWICE and demands identical rows on every
deterministic field (wall_ms is advisory); on mismatch it fails loudly and
writes no record. Exit codes: 0 ok (loud SKIPs allowed) · 2 config/usage ·
3 no exercisable task class · 5 double-run nondeterminism.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        ).stdout.strip()
    except OSError:
        return ""


def _aggregate(rows: list[dict]) -> dict:
    agg: dict[str, dict] = {}
    for arm in ("grep", "neuronav"):
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        agg[arm] = {
            "tasks": len(rs),
            "success": sum(r["success"] for r in rs),
            "calls": sum(r["calls"] for r in rs),
            "files": sum(r["files"] for r in rs),
            "read_kb": round(sum(r["read_bytes"] for r in rs) / 1024.0, 1),
            "ret_kb": round(sum(r["ret_bytes"] for r in rs) / 1024.0, 1),
            "wall_ms": round(sum(r["wall_ms"] for r in rs), 1),
        }
    return agg


def _by_class(rows: list[dict]) -> dict:
    """Per-class success counts (grep / neuronav)."""
    out: dict[str, dict[str, int]] = {}
    for cls in ("find-symbol", "trace-call-path", "locate-refactor-site",
                "dead-code-check"):
        rs = [r for r in rows if r["cls"] == cls]
        if not rs:
            continue
        n = len([r for r in rs if r["arm"] == "grep"])
        out[cls] = {
            "n": n,
            "grep": sum(r["success"] for r in rs if r["arm"] == "grep"),
            "neuronav": sum(r["success"] for r in rs if r["arm"] == "neuronav"),
        }
    return out


def _print_report(n_tasks: int, notes: list[str], rows: list[dict]) -> None:
    print(f"tasks: {n_tasks}" + (f"  [{'; '.join(notes)}]" if notes else ""))
    print()
    hdr = (f"{'task':62} {'arm':9} {'ok':2} {'calls':>5} {'files':>5} "
           f"{'rdKB':>8} {'retKB':>7} {'ms':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['task'][:62]:62} {r['arm']:9} {1 if r['success'] else 0:2} "
              f"{r['calls']:5} {r['files']:5} "
              f"{r['read_bytes'] / 1024.0:8.1f} "
              f"{r['ret_bytes'] / 1024.0:7.1f} {r['wall_ms']:8.1f}")
    print()
    agg = _aggregate(rows)
    print(f"{'arm':9} {'success':>9} {'calls':>7} {'files':>8} "
          f"{'rdKB':>9} {'retKB':>8} {'ms(sum)':>9}")
    for arm, a in agg.items():
        print(f"{arm:9} {a['success']:>4}/{a['tasks']:<4} {a['calls']:7} "
              f"{a['files']:8} {a['read_kb']:9.1f} {a['ret_kb']:8.1f} "
              f"{a['wall_ms']:9.1f}")
    bc = _by_class(rows)
    if bc:
        print()
        print("success by class (grep / neuronav):")
        for cls, c in bc.items():
            print(f"  {cls:22} {c['grep']}/{c['n']}  {c['neuronav']}/{c['n']}")


def write_record(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n",
                    encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="agent-level A/B: grep-only vs neuronav-wired scripted "
                    "agents (issue #72)")
    ap.add_argument("--config", default=str(REPO / "config" / "neuronav.json"),
                    help="nav config to boot (default: the self-index)")
    ap.add_argument("--fake", action="store_true",
                    help="fake embeds — plumbing battery only, no signal")
    ap.add_argument("--label", default="selfindex",
                    help="record filename suffix: agent_ab-<label>.json")
    ap.add_argument("--per-class", type=int, default=3)
    ap.add_argument("--dead-each", type=int, default=2,
                    help="tasks per dead-code-check side (dead / alive)")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args()

    cfg = Path(args.config).resolve()
    if not cfg.is_file():
        print(f"ERROR: config not found: {cfg}")
        return 2
    os.environ["NEURONAV_CONFIG"] = str(cfg)
    if args.fake:
        os.environ["NEURONAV_EMBED_FAKE"] = "1"

    sys.path.insert(0, str(REPO))
    import graph  # noqa: E402  (binds the booted profile via nav)
    import nav  # noqa: E402

    from bench.agent_ab import arms as ab_arms
    from bench.agent_ab import tasks as ab_tasks

    stats = nav.rescan()
    g = graph.get_graph(rebuild=True)
    graph.sync_functions(stats.get("changed", []),
                         stats.get("deleted_paths", []))
    n_funcs = sum(len(fs.funcs) for fs in g.files.values())
    n_edges = sum(len(e) for e in g.edges.values())
    print(f"index: {nav.count()} files, {n_funcs} funcs, {n_edges} call "
          f"edges (rescan {stats['added']}+/{stats['updated']}~/"
          f"{stats['deleted']}-, embed {nav.embed_mode()})")

    tasks, notes = ab_tasks.derive_tasks(
        g, nav.ROOT, per_class=args.per_class, dead_each=args.dead_each)
    for note in notes:
        print(note)
    if not tasks:
        print("ERROR: no task class could be exercised on this index")
        return 3

    arms = {"grep": ab_arms.make_grep_arm(nav.ROOT),
            "neuronav": ab_arms.make_nav_arm()}
    rows1 = ab_arms.run_battery(tasks, arms)
    rows2 = ab_arms.run_battery(tasks, arms)
    diffs = ab_arms.compare_rows(rows1, rows2)
    if diffs:
        print("ERROR: double-run nondeterminism (stable fields differ):")
        for d in diffs:
            print(f"  {d}")
        return 5
    for r1, r2 in zip(rows1, rows2):
        r1["wall_ms2"] = r2["wall_ms"]

    _print_report(len(tasks), notes, rows1)

    if not args.no_record:
        payload = {
            "schema": 1,
            "harness": "agent_ab",
            "label": args.label,
            "commit": _git("rev-parse", "--short", "HEAD") or "unknown",
            "dirty": bool(_git("status", "--porcelain")),
            "mode": "fake" if args.fake else "real",
            "model": "hash-embed" if args.fake else nav.EMBED_MODEL,
            "index": {
                "files": nav.count(), "funcs": n_funcs, "edges": n_edges,
                "tasks": len(tasks),
            },
            "notes": notes,
            "aggregate": _aggregate(rows1),
            "by_class": _by_class(rows1),
            "rows": rows1,
            "double_run": True,
            "wall_note": "wall_ms is machine-local and advisory; it is "
                         "excluded from the double-run determinism contract "
                         "(stable fields: task/cls/arm/success/answer/"
                         "calls/files/read_bytes/ret_bytes). wall_ms2 = "
                         "second pass.",
        }
        out = REPO / "bench" / "runs" / f"agent_ab-{args.label}.json"
        write_record(out, payload)
        print(f"\nrecord: {out.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
