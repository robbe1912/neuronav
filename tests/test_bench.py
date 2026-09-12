# bench record/golden coherence (issue #104): the golden set was swapped
# without re-running, so committed records carried retired queries — render
# KeyErrored, then silently drew '?' cells. These checks pin the loud guard:
# records stamp a golden fingerprint at write time and render refuses stale
# ones by name. Hermetic: imports run_bench only (its import surface is
# stdlib; nav/recall load lazily inside run()/sweep()) — no index, no
# embeds, no config. Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_bench.py
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))

import run_bench  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")
    if not cond:
        FAILURES.append(name)


def synth_record(golden: list[dict], fp: str, stamp: bool = True) -> dict:
    """A minimal but honest after/both record: every golden query ranked 1."""
    per_query = [{"q": r["q"], "rank": 1, "ctx5": False, "ctx10": False} for r in golden]
    n = len(per_query)
    by_kind = {}
    for kind in ("exact", "symbol", "prose", "cross"):
        rows = [r for r, q in zip(per_query, golden) if q["kind"] == kind]
        by_kind[kind] = {"n": len(rows), "hit@1": 1.0, "hit@5": 1.0, "hit@10": 1.0, "mrr": 1.0}
    rec = {
        "set": "after", "config": "both", "commit": "testsynth", "dirty": False,
        "mode": "real", "model": "test-embed", "files": n, "k": run_bench.K,
        "hit@1": 1.0, "hit@5": 1.0, "hit@10": 1.0, "mrr": 1.0,
        "reach@5": 1.0, "reach@10": 1.0, "by_kind": by_kind, "per_query": per_query,
    }
    if stamp:
        rec["golden"] = fp
    return rec


def render_in_sandbox(records: dict[str, dict]) -> tuple[str | None, Exception | None]:
    """render() against a scratch bench dir (patched module paths); returns
    (RESULTS.md text if written, exception if any)."""
    tmp = Path(tempfile.mkdtemp(prefix="neuronav_bench_test_"))
    bench = tmp / "bench"
    (bench / "runs").mkdir(parents=True)
    shutil.copy(REPO / "bench" / "golden.json", bench / "golden.json")
    for name, rec in records.items():
        (bench / "runs" / f"{name}.json").write_text(json.dumps(rec), encoding="utf-8")
    old_bench, old_repo = run_bench.BENCH_DIR, run_bench.DEFAULT_REPO
    run_bench.BENCH_DIR, run_bench.DEFAULT_REPO = bench, tmp
    try:
        run_bench.render()
        return (bench / "RESULTS.md").read_text(encoding="utf-8"), None
    except Exception as e:  # the loud guard raises RuntimeError
        return None, e
    finally:
        run_bench.BENCH_DIR, run_bench.DEFAULT_REPO = old_bench, old_repo
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    golden = run_bench._load_golden()
    fp = run_bench._golden_fp()

    check("fingerprint is deterministic", run_bench._golden_fp() == fp)
    check("fingerprint ignores golden row order",
          run_bench._golden_fp(list(reversed(golden))) == fp)
    moved = [dict(r) for r in golden]
    moved[0]["targets"] = ["some/other_file.py"]
    check("fingerprint tracks target moves", run_bench._golden_fp(moved) != fp)
    dropped = [dict(r) for r in golden[:-1]]
    check("fingerprint tracks query count", run_bench._golden_fp(dropped) != fp)

    text, err = render_in_sandbox({"after-both": synth_record(golden, fp)})
    check("render succeeds on fingerprinted records", err is None and text is not None,
          repr(err))
    if text is not None:
        check("per-query table covers the first golden query", f"`{golden[0]['q']}`" in text)
        check("no silent '?' cells on coherent records", "?" not in text.split("per-query")[-1])

    out, err = render_in_sandbox({"after-both": synth_record(golden, "deadbeefdeadbeef")})
    check("mismatched fingerprint fails render loudly",
          isinstance(err, RuntimeError) and "after-both.json" in str(err), repr(err))
    check("failure names the golden set", isinstance(err, RuntimeError) and "golden" in str(err))

    out, err = render_in_sandbox({"after-both": synth_record(golden, fp, stamp=False)})
    check("missing fingerprint (pre-#104 record) fails render loudly",
          isinstance(err, RuntimeError) and "after-both.json" in str(err), repr(err))

    before_rec = synth_record(golden, "stale0123stale0123")
    before_rec["set"], before_rec["config"] = "before", "vec"
    out, err = render_in_sandbox({"after-both": synth_record(golden, fp), "before-vec": before_rec})
    check("mixed fresh/stale sets name only the stale record",
          isinstance(err, RuntimeError) and "before-vec.json" in str(err)
          and "after-both.json" not in str(err), repr(err))

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
