# agent-level A/B harness (issue #72) — coherence battery.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_agent_ab.py
#
# Hermetic: generated temp target tree + config (never the real index),
# NEURONAV_EMBED_FAKE=1 (deterministic hash embeddings, no Ollama). Pins:
#   - task derivation gates every class and skips loudly (never silently)
#   - both scripted arms answer real questions on the synthetic corpus
#   - the ground-truth check HAS TEETH: a fabricated answer (wrong file,
#     partial/padded set, wrong dead tier, no answer) cannot pass
#   - double-run determinism on stable fields, wall_ms advisory
#   - agent_ab records never leak into run_bench's record set (issue #104
#     gate keeps working with agent_ab-*.json sitting in bench/runs/)
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TMP = Path(tempfile.mkdtemp(prefix="neuronav_agentab_"))
(TMP / "src").mkdir(parents=True)

# synthetic corpus: exercises all four classes with unambiguous answers
#  - widget_maker: unique def, cross-file callers (find-symbol / refactor)
#  - compose_ui: calls render_text (beta) + pack_row (gamma) (trace)
#  - orphaned_helper: dead:likely (no mention anywhere)
#  - harness_main: alive via the __main__ guard, no static callers
#  - test_harness_boot: alive via the test-entry rule, no static callers
(TMP / "src" / "alpha.py").write_text(
    "from beta import render_text\n"
    "from gamma import pack_row\n"
    "\n"
    "\n"
    "def orphaned_helper(count):\n"
    "    return count + 1\n"
    "\n"
    "\n"
    "def widget_maker(size):\n"
    "    return size * 2\n"
    "\n"
    "\n"
    "def compose_ui(size):\n"
    "    made = widget_maker(size)\n"
    "    return render_text(made) + pack_row(made)\n",
    encoding="utf-8",
)
(TMP / "src" / "beta.py").write_text(
    "def render_text(made):\n"
    "    return str(made)\n",
    encoding="utf-8",
)
(TMP / "src" / "gamma.py").write_text(
    "def pack_row(made):\n"
    "    return [made]\n",
    encoding="utf-8",
)
(TMP / "src" / "delta.py").write_text(
    "from alpha import widget_maker\n"
    "\n"
    "\n"
    "def harness_main():\n"
    "    return widget_maker(3)\n"
    "\n"
    "\n"
    "def test_harness_boot():\n"
    "    return widget_maker(2)\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    harness_main()\n",
    encoding="utf-8",
)

# issue #321 lever 3: the fixed config name raced twin runs (its bytes
# embed this run's mkdtemp TMP) — inside TMP it is unique per run
CFG = TMP / "config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "agentab",
            "include_dirs": ["src"],
            "extensions": [".py"],
            "state_dir": str(TMP / "state"),
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ["NEURONAV_EMBED_FAKE"] = "1"

import graph  # noqa: E402  (binds the temp config above via nav)
import navconfig, navindex
from bench import run_bench  # noqa: E402
from bench.agent_ab import arms as ab_arms  # noqa: E402
from bench.agent_ab import run as ab_run  # noqa: E402
from bench.agent_ab import tasks as ab_tasks  # noqa: E402



from harness import FAILURES as FAILS, styled

check = styled("bracket")  # byte pin: [PASS]/[FAIL] tag lines

# -- boot the index the same way run.py does (rescan -> graph -> fns) --------
stats = navindex.rescan()
g = graph.get_graph(rebuild=True)
graph.sync_functions(stats.get("changed", []), stats.get("deleted_paths", []))

# -- derivation: every class exercisable, no silent drops ---------------------
tasks, notes = ab_tasks.derive_tasks(g, navconfig.ROOT)
by_cls = {t.cls: t for t in tasks}
for cls in ab_tasks.CLASSES:
    present = [t for t in tasks if t.cls == cls]
    check(f"derive:{cls}", bool(present), f"notes={notes}")
skips = [n for n in notes if n.startswith("SKIP")]
check("derive:no-skip", not skips, "; ".join(skips))

t_find = next((t for t in tasks if t.cls == "find-symbol"
               and t.facts["name"] == "widget_maker"), None)
t_trace = next((t for t in tasks if t.cls == "trace-call-path"
                and t.facts["name"] == "compose_ui"), None)
t_refactor = [t for t in tasks if t.cls == "locate-refactor-site"
              and t.facts["name"] == "render_text"]
t_dead = [t for t in tasks if t.cls == "dead-code-check"
          and t.facts["name"] == "orphaned_helper"]
t_alive = [t for t in tasks if t.cls == "dead-code-check"
           and t.facts["name"] == "harness_main"]
check("derive:find-widget", t_find is not None
      and t_find.facts["name"] == "widget_maker"
      and t_find.expected == "src/alpha.py")
check("derive:trace-compose", t_trace is not None
      and t_trace.facts["name"] == "compose_ui"
      and sorted(map(str, t_trace.expected)) == ["src/beta.py::render_text",
                                                 "src/gamma.py::pack_row"],
      f"expected={getattr(t_trace, 'expected', None)}")
check("derive:refactor-render", bool(t_refactor)
      and t_refactor[0].expected == ["src/alpha.py"],
      f"expected={t_refactor[0].expected if t_refactor else None}")
check("derive:dead-orphan", bool(t_dead)
      and t_dead[0].expected == "dead:likely",
      f"expected={t_dead[0].expected if t_dead else None}")
check("derive:alive-main", bool(t_alive) and t_alive[0].expected == "alive")

# -- both arms answer on the synthetic corpus ---------------------------------
arms = {"grep": ab_arms.make_grep_arm(navconfig.ROOT),
        "neuronav": ab_arms.make_nav_arm()}
rows = ab_arms.run_battery(tasks, arms)
by_key = {(r["task"], r["arm"]): r for r in rows}

g_find = by_key.get((t_find.tid, "grep")) if t_find is not None else None
check("grep:find-symbol", bool(g_find and g_find["success"]
      and g_find["answer"] == "src/alpha.py"), str(g_find))
g_trace = by_key.get((t_trace.tid, "grep")) if t_trace is not None else None
check("grep:trace", bool(g_trace and g_trace["success"]), str(g_trace))
g_ref = by_key.get((t_refactor[0].tid, "grep")) if t_refactor else None
check("grep:refactor", bool(g_ref and g_ref["success"]), str(g_ref))
g_dead = by_key.get((t_dead[0].tid, "grep")) if t_dead else None
check("grep:dead-likely", bool(g_dead and g_dead["success"]
      and g_dead["answer"] == "dead:likely"), str(g_dead))
g_alive = by_key.get((t_alive[0].tid, "grep")) if t_alive else None
check("grep:alive-guard", bool(g_alive and g_alive["success"]), str(g_alive))

nav_rows = [r for r in rows if r["arm"] == "neuronav"]
check("neuronav:all-success",
      len(nav_rows) == len(tasks) and all(r["success"] for r in nav_rows),
      "; ".join(f"{r['task']}={r['answer']}" for r in nav_rows
                if not r["success"]))
check("neuronav:repo-map-preamble",
      all(r["calls"] >= 1 for r in nav_rows))

# -- the ground-truth check has teeth ------------------------------------------
probe = ab_tasks.Task("find-symbol", "probe", "p", "src/alpha.py",
                      {"name": "widget_maker", "file": "src/alpha.py"})
check("teeth:wrong-file", not probe.check("src/beta.py"))
check("teeth:no-answer", not probe.check(None))
check("teeth:right-file", probe.check("src/alpha.py"))

setp = ab_tasks.Task("trace-call-path", "probe2", "p",
                     ["src/beta.py::render_text", "src/gamma.py::pack_row"],
                     {})
check("teeth:partial-set", not setp.check(["src/beta.py::render_text"]))
check("teeth:padded-set",
      not setp.check(["src/beta.py::render_text", "src/gamma.py::pack_row",
                      "src/zeta.py::extra"]))
check("teeth:full-set", setp.check(["src/gamma.py::pack_row",
                                    "src/beta.py::render_text"]))

tierp = ab_tasks.Task("dead-code-check", "probe3", "p", "dead:review",
                      {"name": "x", "file": "y"})
check("teeth:wrong-tier", not tierp.check("dead:likely"))
check("teeth:alive-lie", not tierp.check("alive"))
check("teeth:tier", tierp.check("dead:review"))

# a lying arm (structurally valid answers, wrong content) cannot score:
def liar(task):
    exp = task.expected
    if isinstance(exp, list) and len(exp) > 1:
        return exp[:-1], ab_arms.Cost(calls=1)  # plausible but partial
    return "src/fabricated.py", ab_arms.Cost(calls=1)

lie_rows = []
for t in tasks:  # liar is CALLED, not passed as a bare value — a value
    answer, _ = liar(t)  # reference is invisible to the static call graph
    lie_rows.append({"task": t.tid, "success": bool(t.check(answer))})  # (dead:likely)
check("teeth:liar-arm-0%",
      bool(lie_rows) and not any(r["success"] for r in lie_rows),
      f"{sum(r['success'] for r in lie_rows)}/{len(lie_rows)} passed")

# -- determinism contract ------------------------------------------------------
rows2 = ab_arms.run_battery(tasks, arms)
check("determinism:double-run", ab_arms.compare_rows(rows, rows2) == [])
tampered = [dict(r) for r in rows2]
tampered[0]["answer"] = "tampered"
check("determinism:answer-diff-caught",
      bool(ab_arms.compare_rows(rows, tampered)))
wall_only = [dict(r) for r in rows2]
wall_only[0]["wall_ms"] += 999.0
check("determinism:wall-advisory",
      ab_arms.compare_rows(rows, wall_only) == [])

# -- records: writer roundtrip + run_bench isolation ---------------------------
rec_path = TMP / "runs" / "agent_ab-probe.json"
ab_run.write_record(rec_path, {"schema": 1, "harness": "agent_ab", "rows": rows})
loaded = json.loads(rec_path.read_text(encoding="utf-8"))
check("record:roundtrip", loaded["harness"] == "agent_ab"
      and len(loaded["rows"]) == len(rows))

scratch = Path(tempfile.mkdtemp(prefix="neuronav_agentab_bench_"))
(scratch / "runs").mkdir(parents=True)
(scratch / "runs" / "agent_ab-selfindex.json").write_text(
    json.dumps({"schema": 1, "harness": "agent_ab"}), encoding="utf-8")
(scratch / "runs" / "after-both.json").write_text(
    json.dumps({"set": "after", "config": "both", "golden": "fp"}),
    encoding="utf-8")
old_bench = run_bench.BENCH_DIR
run_bench.BENCH_DIR = scratch
try:
    recs = run_bench._records()
finally:
    run_bench.BENCH_DIR = old_bench
check("record:run_bench-isolation", list(recs) == ["after-both"],
      f"recs={list(recs)}")

shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(scratch, ignore_errors=True)
CFG.unlink(missing_ok=True)

print()
# ---- #298: battery pool reports in COMPLETION order --------------------------
# The old loop consumed futures in submit order, so a slow early suite
# withheld every later PASS line — the "prints as they finish" contract lied.
import importlib as _imp298
import io as _io298, contextlib as _cx298
import time as _t298
_rb298 = _imp298.import_module("tools.run_battery")
_real298 = (_rb298.run_suite, _rb298.classify, _rb298.SERIAL, _rb298.GATED, sys.argv)
def _fake298(root, name, extra_env):
    _d298 = {"test_slow.py": 0.30, "test_fast.py": 0.02}[name]
    _t298.sleep(_d298)
    return name, 0, _d298, ""
_rb298.run_suite = _fake298
_rb298.classify = lambda root: (["test_slow.py", "test_fast.py"], {})
_rb298.SERIAL, _rb298.GATED = set(), {}
sys.argv = ["run_battery.py"]
_buf298 = _io298.StringIO()
try:
    with _cx298.redirect_stdout(_buf298):
        _rb298.main()
except SystemExit:
    pass
finally:
    (_rb298.run_suite, _rb298.classify, _rb298.SERIAL, _rb298.GATED) = _real298[:4]
    sys.argv = _real298[4]
_order298 = [l for l in _buf298.getvalue().splitlines()
             if l.startswith(("PASS ", "FAIL "))]
_i_fast = _order298.index("PASS test_fast rc=0 0.0s") if "PASS test_fast rc=0 0.0s" in _order298 else -1
_i_slow = _order298.index("PASS test_slow rc=0 0.3s") if "PASS test_slow rc=0 0.3s" in _order298 else -1
if not (0 <= _i_fast < _i_slow):
    FAILS.append("battery completion order")
    print(f"FAIL battery pool reports in completion order (#298): {_order298}")
else:
    print("ok - battery pool reports in completion order (#298)")

# byte pin: FAIL-list summary + banner — kept local
if FAILS:
    print(f"{len(FAILS)} FAIL: {FAILS}")
    sys.exit(1)
print("agent_ab coherence: all green")
