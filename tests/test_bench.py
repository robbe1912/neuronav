# bench record/golden coherence (issue #104): the golden set was swapped
# without re-running, so committed records carried retired queries — render
# KeyErrored, then silently drew '?' cells. These checks pin the loud guard:
# records stamp a golden fingerprint at write time and render refuses stale
# ones by name. Issue #391 teeth: the #344/#345/#352 splits moved 7 golden
# anchors into the nav*/server* leaves, --verify-only aborted every full
# bench run, and no CI leg pinned verify_golden — so these also check the
# golden set against the tree at HEAD and a re-rotted target's loud refusal.
# Hermetic: imports run_bench only (its import surface is stdlib; nav/recall
# load lazily inside run()/sweep()) — no index, no embeds, no config.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_bench.py
import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))

import run_bench  # noqa: E402



from harness import finish, styled

check = styled("wide")  # byte pin: two-space tag, fail-only detail

def verify_in_sandbox(rows: list[dict]) -> tuple[int, str]:
    """verify_golden() against a scratch bench dir (patched module path);
    returns (returncode, captured stdout)."""
    tmp = Path(tempfile.mkdtemp(prefix="neuronav_bench_verify_"))
    (tmp / "bench").mkdir()
    (tmp / "bench" / "golden.json").write_text(
        json.dumps({"queries": rows}), encoding="utf-8")
    old_bench = run_bench.BENCH_DIR
    run_bench.BENCH_DIR = tmp / "bench"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = run_bench.verify_golden(REPO)
    finally:
        run_bench.BENCH_DIR = old_bench
        shutil.rmtree(tmp, ignore_errors=True)
    return rc, buf.getvalue()


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

    # issue #391: golden coherence vs the tree itself — the splits moved
    # anchors and nothing failed until a full bench run aborted at boot
    rc, out = verify_in_sandbox(golden)
    check("verify_golden justifies all 25 rows against the tree at HEAD",
          rc == 0 and "25/25" in out, out)

    rotted = [dict(r) for r in golden]
    rotted[1]["targets"] = ["nav.py"]  # sha256_of's pre-split home, deliberately
    rc, out = verify_in_sandbox(rotted)
    check("a re-rotted target fails verify loudly, naming anchor+file+query",
          rc == 1 and "anchor 'def sha256_of' not in nav.py" in out
          and "(sha256_of)" in out and "DRIFT" in out, out)

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

    ab = synth_record(golden, fp)
    ab["set"] = "ab"
    jina = synth_record(golden, fp)
    jina["set"] = "jina"
    jina["model"] = "jina-code-embeddings-0.5b:Q8_0"
    for key in ("hit@1", "hit@5", "hit@10", "mrr", "reach@5", "reach@10"):
        jina[key] = 0.9
    text, err = render_in_sandbox({"ab-both": ab, "jina-both": jina})
    check("A/B section renders from leg records (issue #75)",
          err is None and text is not None and "## Embedding A/B (issue #75)" in text,
          repr(err))
    if text is not None:
        check("A/B delta table credits the jina leg",
              "jina-code-embeddings-0.5b:Q8_0" in text and "-10.0" in text)

    # issue #228: the ceiling grids render with the RepoBench easy/hard
    # split — hard = pass-1 rank miss or > 5 in the in-grid baseline
    # (gbw-gb0-k60-w1-1), and _split_metrics credits only rank <= 5
    base = synth_record(golden, fp)
    base.update({"set": "gbw", "config": "gb0-k60-w1-1", "gb_lambda": 0.0,
                 "gb_rrf_k": 60.0, "gb_wv": 1.0, "gb_wl": 1.0})
    base["per_query"][0]["rank"] = 7   # hard: beyond 5
    base["per_query"][1]["rank"] = None  # hard: miss
    cell = synth_record(golden, fp)
    cell.update({"set": "tps", "config": "p3-b320-i0-w1", "tp_pool": 3,
                 "tp_budget": 320, "tp_imports": False, "tp_weight": 1.0})
    cell["per_query"][0]["rank"] = 1   # hard query recovered to rank 1
    cell["per_query"][1]["rank"] = None  # still missed
    text, err = render_in_sandbox(
        {"gbw-gb0-k60-w1-1": base, "tps-p3-b320-i0-w1": cell})
    check("ceiling section renders from gbw/tps records (issue #228)",
          err is None and text is not None
          and "## Recall ceiling push — census exp 1+2 (issue #228)" in text
          and "### Exp 1" in text and "### Exp 2" in text, repr(err))
    hard_qs = {golden[0]["q"], golden[1]["q"]}
    h5, hm, hn = run_bench._split_metrics(cell, hard_qs)
    check("hard split: n, hit@5 and MRR over the hard subset only",
          hn == 2 and h5 == 0.5 and hm == 0.5, f"{hn} {h5} {hm}")
    if text is not None:
        check("exp-2 rows carry the hard-split columns",
              "| 3 | 320 | 0 | 1 |" in text and "0.500 | 0.500 |" in text)
        check("baseline hard row documented (0 by construction on hit@5)",
              "Baseline `both` on the same hard 2" in text)

    finish()


if __name__ == "__main__":
    raise SystemExit(main())
