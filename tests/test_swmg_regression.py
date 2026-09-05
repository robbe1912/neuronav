# SWMG .gd-pipeline regression — fresh process, default config:
#   .venv/Scripts/python.exe -X utf8 tests/test_swmg_regression.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph  # noqa: E402  (default config.json -> SWMG)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


g = graph.get_graph(rebuild=True)
gd = [f for f in g.files.values() if f.ext == ".gd"]
check("swmg total files", len(g.files) >= 630, f"{len(g.files)} files ({len(gd)} .gd)")
type_sum = sum(len(tys) for tys in g.edges.values())
check("swmg edges stable", 7300 <= type_sum <= 8100,
      f"type-sum={type_sum} unique-pairs={len(g.edges)}")

dead = g.dead_code(100000)
check("swmg dead stable", 60 <= dead["total"] <= 130, f"dead={dead['total']}")
cands = dead["candidates"]
check("canary get_all_items dead",
      any("item_registry" in c["path"] and "get_all_items" in c["func"] for c in cands))
check("encrypt_env alive", not any("encrypt_env" in c["path"] for c in cands))
check("corner_pillar alive", not any("corner_pillar" in c["path"] for c in cands))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
