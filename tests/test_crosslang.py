# cross-language verification (gdnav self-index config) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_crosslang.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["GDNAV_CONFIG"] = str(Path(__file__).resolve().parents[1] / "config" / "gdnav.json")

import graph  # noqa: E402  (binds gdnav config via GDNAV_CONFIG)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


g = graph.get_graph(rebuild=True)

# 0. function vector index: upsert every parsed function explicitly so the
# check is deterministic regardless of collection state (bootstrap vs
# already-populated exercise the same parse+embed+upsert path)
_all = sorted(rel for rel, fs in graph._all_filesyms().items() if fs.funcs)
sync = graph.sync_functions(_all, [])
check("py fns synced", sync["fns_upserted"] >= 80, str(sync))

# 1. graph layer parsed python
py_files = [f for f in g.files.values() if f.ext == ".py"]
py_funcs = sum(len(f.funcs) for f in py_files)
check("py graph parsed", len(py_files) >= 10 and py_funcs >= 80,
      f"{len(py_files)} files, {py_funcs} funcs")

# 2. function-level semantic search stays inside this config's collection
hits = graph.find_functions("cluster label", n=3)
check("py find_functions scoped", bool(hits) and all(h["path"].endswith(".py") for h in hits),
      str([(h["path"], h["func"]) for h in hits]))

# 3. symbol graph on a python function
res = g.symbol_graph("label_cluster", 1)
check("py symbol_graph", "label_cluster" in str(res)[:400], str(res)[:160])

# 4. dead-code run is sane on python
dead = g.dead_code(100000)
check("py dead_code runs", 0 < dead["total"] < len(g.edges),
      f"total={dead['total']} edges={len(g.edges)}")

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
