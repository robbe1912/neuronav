# Parallel battery driver — the promoted scratch driver from issue #280's
# impact session, now a checked-in dev gate (issue #286): run every
# tests/test_*.py suite with per-suite exit codes and one summary line.
#   .venv/Scripts/python.exe -X utf8 tools/run_battery.py          (exit 0 = 0 fail)
#   .venv/Scripts/python.exe -X utf8 tools/run_battery.py --only walkguard,stdio
#   .venv/Scripts/python.exe -X utf8 tools/run_battery.py -j 6 -C E:/some/worktree
# Why parallel (issue #286): the serial loop made a 0F battery proof a
# ~35-minute single-core crawl, so it rarely ran between changes. The pool
# below runs the parallel-eligible suites concurrently (default 4; they are
# self-contained hermetic scratch trees by convention — distinct temp-dir
# names, own state dirs).
# Classification (recomputed per run from tests/test_*.py at HEAD):
#   pool      — every suite except the serial/gated names below, plus
#               test_target_regression whenever the checkout-local
#               config.json exists (issue #321 lever 1: the treg leg
#               overlaps into the pool — its profile store is
#               checkout-local and CPU-only, no port/store/scratch
#               overlap with any pool member). Pool legs get FAKE
#               embeds and a scrubbed NEURONAV_CONFIG so no ambient
#               profile rebinds a hermetic suite mid-battery.
#   serial    — test_qa_smoke: leg A only (NEURONAV_QA_SMOKE_NO_BROWSER=1);
#               the full-browser leg B belongs to the CI viz job;
#               test_crosslang: the chroma store race (issue #301 C) — its
#               self-index leg rides config/neuronav.json's
#               "state_dir": "default" (<root>/.neuronav) and its fn-level
#               toy leg pins the same default, making it the only pool suite
#               on the DEFAULT chroma store while test_selfindex (same
#               profile, rebuild=True) and test_explore (self-bootstrap)
#               hold that store at -j4; observed as chroma InternalError
#               "Error finding id" (PR #312 battery; attribution GK). One
#               entry — the #283 parallelization win stands.
#   gated     — test_viz: runs only when the machine-local vizcorpus
#               profile exists (~/vizcorpus/config.json, CI viz job shape),
#               STRICTLY LAST, post-pool on a quiet machine —
#               viz-under-concurrent-load is the documented flake class
#               (#299-era triage). test_target_regression without the
#               checkout-local config.json skips loudly (issue #97).
# Dispatch (issue #321 lever 2): the pool runs longest-first (LPT) off a
# persisted per-suite timing cache (gitignored .tmp/battery_timings.json,
# last run's dt; cache miss = alphabetical cold start) — makespan was set
# by slow suites starting mid-pool. Default -j stays 4; a -j 6 trial is
# unlocked by the mkdtemp fixes (issue #321 lever 3).
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SERIAL = {
    "test_qa_smoke.py",
    # races test_selfindex/test_explore on the DEFAULT chroma store
    # (<root>/.neuronav) — see the serial bullet above for the schedule
    "test_crosslang.py",
}
GATED = {
    "test_viz.py": "~/vizcorpus/config.json (CI viz job builds the corpus)",
}
TIMEOUT_S = 1200  # per-suite ceiling, the scratch-driver precedent
TAIL_OUT, TAIL_ERR = 25, 15  # fail-dump tails, same precedent


def classify(root: Path) -> tuple[list[str], dict[str, str]]:
    """(pool, {suite: skip-reason}) for every tests/test_*.py at HEAD."""
    all_suites = sorted(
        p.name for p in (root / "tests").glob("test_*.py") if p.is_file()
    )
    if not all_suites:
        sys.exit(f"run_battery: no tests/test_*.py under {root} (pass -C <repo root>)")
    pool, skipped = [], {}
    for name in all_suites:
        if name in SERIAL:
            continue
        if name == "test_target_regression.py":
            # issue #321 lever 1: pool the treg leg when the profile is
            # present; without it, a loud skip beats a silent pool entry
            if (root / "config.json").is_file():
                pool.append(name)
            else:
                skipped[name] = ("checkout-local config.json "
                                 "(owner .gd target profile, issue #97)")
            continue
        if name in GATED:
            if name == "test_viz.py" and (Path.home() / "vizcorpus" / "config.json").is_file():
                continue  # strictly last: post-pool, quiet machine
            skipped[name] = GATED[name]
            continue
        pool.append(name)
    return pool, skipped


def run_suite(root: Path, name: str, extra_env: dict[str, str]) -> tuple[str, object, float, str]:
    """(name, rc, seconds, tail) — rc is an int, or 'TIMEOUT'."""
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env["NEURONAV_EMBED_FAKE"] = "1"
    env.update(extra_env)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(root / "tests" / name)],
            cwd=root, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT_S, check=False,
        )
        rc, dt = proc.returncode, time.perf_counter() - t0
        tail = ""
        if rc != 0:
            tail = "".join((proc.stdout or "").splitlines(True)[-TAIL_OUT:])
            tail += "".join((proc.stderr or "").splitlines(True)[-TAIL_ERR:])
    except subprocess.TimeoutExpired as exc:
        rc, dt = "TIMEOUT", time.perf_counter() - t0
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        tail = out + err
    return name, rc, dt, tail


def main() -> None:
    ap = argparse.ArgumentParser(description="run every tests/test_*.py suite, parallel pool + serial/gated legs (issue #286)")
    ap.add_argument("-C", "--cwd", default=str(Path.cwd()),
                    help="repo root to run the battery against (default: invocation dir)")
    ap.add_argument("-j", "--jobs", type=int, default=4,
                    help="parallel pool width (default 4; suites are single-core)")
    ap.add_argument("--only", action="append", default=[],
                    metavar="SUBSTR", help="run only suites whose stem contains SUBSTR (repeatable)")
    ap.add_argument("--list", action="store_true", help="print the classification and exit")
    args = ap.parse_args()

    root = Path(args.cwd).resolve()
    pool, skipped = classify(root)

    # issue #321 lever 2: LPT dispatch — the 7a54b9e baseline had strata
    # 208.8s and server_stdio 99.2s starting mid-pool (alphabetical), so
    # the makespan waited on them. Longest first, name tie-break, off the
    # persisted timing cache; PASS lines still print in completion order
    # (#298), so output is submit-order-insensitive.
    cache = root / ".tmp" / "battery_timings.json"
    try:
        loaded = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
    except (OSError, ValueError):
        loaded = {}
    timings = loaded if isinstance(loaded, dict) else {}
    pool.sort(key=lambda n: (-float(timings.get(n) or 0.0), n))
    serial = [n for n in sorted(SERIAL) if (root / "tests" / n).is_file()]
    gated_run = [n for n in GATED
                 if n not in skipped and (root / "tests" / n).is_file()]

    if args.only:
        want = lambda n: any(s in n for s in args.only)
        pool = [n for n in pool if want(n)]
        serial = [n for n in serial if want(n)]
        gated_run = [n for n in gated_run if want(n)]
        skipped = {n: r for n, r in skipped.items() if want(n)}

    if args.list:
        print(f"root: {root}")
        print(f"pool ({len(pool)}, -j {args.jobs}): " + ", ".join(pool))
        print(f"serial ({len(serial)}): " + ", ".join(serial))
        print(f"gated-run ({len(gated_run)}): " + ", ".join(gated_run))
        print(f"gated-skip ({len(skipped)}): " + ", ".join(f"{n} — {r}" for n, r in skipped.items()))
        return

    results: list[tuple[str, object, float]] = []

    def report(name: str, rc: object, dt: float, tail: str = "") -> None:
        verdict = "PASS" if rc == 0 else "FAIL"
        print(f"{verdict} {name[:-3]} rc={rc} {dt:.1f}s", flush=True)
        if tail:
            print(f"--- {name} tail " + "-" * 40, file=sys.stderr, flush=True)
            print(tail, end="", file=sys.stderr, flush=True)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = [ex.submit(run_suite, root, n, {}) for n in pool]
        # #298: consume in COMPLETION order — submit order withheld later
        # PASS lines behind a slow early suite
        for fut in as_completed(futs):
            name, rc, dt, tail = fut.result()
            results.append((name, rc, dt))
            report(name, rc, dt, tail)

    for name in serial:
        extra = {"NEURONAV_QA_SMOKE_NO_BROWSER": "1"} if name == "test_qa_smoke.py" else {}
        name_, rc, dt, tail = run_suite(root, name, extra)
        results.append((name_, rc, dt))
        report(name_, rc, dt, tail)

    for name in gated_run:
        extra = ({"NEURONAV_CONFIG": str(Path.home() / "vizcorpus" / "config.json")}
                 if name == "test_viz.py" else {})
        name_, rc, dt, tail = run_suite(root, name, extra)
        results.append((name_, rc, dt))
        report(name_, rc, dt, tail)

    for name, reason in sorted(skipped.items()):
        print(f"SKIP {name[:-3]} — gated: {reason}", flush=True)

    for name, _rc, dt in results:
        timings[name] = round(float(dt), 1)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(timings, indent=1, sort_keys=True) + "\n",
                         encoding="utf-8")
    except OSError:
        pass  # advisory cache — a failed persist never fails the battery

    fails = [(n, rc) for n, rc, _ in results if rc != 0]
    wall = time.perf_counter() - t0
    print(f"battery: {len(results) + len(skipped)} suites — "
          f"{len(results) - len(fails)} pass, {len(fails)} fail, "
          f"{len(skipped)} skip  (pool j={args.jobs}, wall {wall:.1f}s)", flush=True)
    if fails:
        print("failed: " + ", ".join(f"{n} rc={rc}" for n, rc in fails), flush=True)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
