# python-extractor hardening fixtures — fresh-process dead_code run:
#   .venv/Scripts/python.exe -X utf8 tests/test_pyhard.py
#
# Builds the graph over tests/fixtures/pyhard ONLY (its own generated
# config), then asserts per-feature liveness: every construct the
# hardening covers stays alive, while each fixture's dead control func
# stays dead (the run is not vacuously all-alive).
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = HERE / "tests" / "fixtures" / "pyhard"

FAILS = []


def check(name: bool | str, cond: bool, detail: str = "") -> None:
    label = f"PASS {name}" if cond else f"FAIL {name}"
    print(label + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(str(name))


cfg = Path(tempfile.gettempdir()) / "gdnav_pyhard_config.json"
cfg.write_text(
    json.dumps(
        {
            "root": str(FIX),
            "collection": "pyhard",
            "include_dirs": ["."],
            "extensions": [".py"],
            "exclude_dirs": ["__pycache__"],
        }
    ),
    encoding="utf-8",
)
os.environ["GDNAV_CONFIG"] = str(cfg)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402

g = graph.get_graph(rebuild=True)
dead = g.dead_code(100000)
DEAD = {(d["path"], d["func"]) for d in dead["candidates"]}


def alive(name: str, path: str, funcs: list[str]) -> None:
    bad = sorted(f for f in funcs if (path, f) in DEAD)
    check(name, not bad, f"unexpected dead: {bad}" if bad else "")


def stays_dead(name: str, path: str, func: str) -> None:
    check(name, (path, func) in DEAD, f"{path}::{func} not in dead set")


# --- fixture: decor.py -------------------------------------------------
alive("decor: property getter+setter alive", "decor.py", ["total"])
alive("decor: static/class methods alive", "decor.py", ["describe", "with_start"])
alive("decor: attribute-dispatch caller alive", "decor.py", ["use_all"])
stays_dead("decor: control stays dead", "decor.py", "unused_helper")

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
