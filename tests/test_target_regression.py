# external-target .gd-pipeline regression — fresh process, default config:
#   .venv/Scripts/python.exe -X utf8 tests/test_target_regression.py
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph  # noqa: E402  (default config.json -> external target)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


g = graph.get_graph(rebuild=True)
gd = [f for f in g.files.values() if f.ext == ".gd"]
check("target total files", len(g.files) >= 630, f"{len(g.files)} files ({len(gd)} .gd)")
type_sum = sum(len(tys) for tys in g.edges.values())
check("target edges stable", 6500 <= type_sum <= 8100,
      f"type-sum={type_sum} unique-pairs={len(g.edges)}")

dead = g.dead_code(100000)
check("target dead stable", 60 <= dead["total"] <= 130, f"dead={dead['total']}")
cands = dead["candidates"]
# canaries live in the machine-local config (gitignored) — they name
# functions in the private target repo and must never be committed:
#   "regression_canaries": {"dead": [["<file token>", "<func>"]],
#                           "alive": ["<file token>", "<file token>"]}
_cfgp = Path(os.environ.get("NEURONAV_CONFIG")
             or Path(__file__).resolve().parents[1] / "config.json")
_cans: dict = {}
if _cfgp.is_file():
    _cans = json.loads(_cfgp.read_text(encoding="utf-8")).get(
        "regression_canaries", {})
for _ptok, _ftok in _cans.get("dead", []):
    check(f"canary {_ftok} dead",
          any(_ptok in c["path"] and _ftok in c["func"] for c in cands))
for _tok in _cans.get("alive", []):
    check(f"canary {_tok} alive", not any(_tok in c["path"] for c in cands))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
