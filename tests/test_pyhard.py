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


cfg = Path(tempfile.gettempdir()) / "neuronav_pyhard_config.json"
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
os.environ["NEURONAV_CONFIG"] = str(cfg)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402

g = graph.get_graph(rebuild=True)
dead = g.dead_code(100000)
DEAD = {(d["path"], d["func"]) for d in dead["candidates"]}


def alive(name: str, path: str, funcs: list[str]) -> None:
    bad = sorted(f for f in funcs if (path, f) in DEAD)
    check(name, not bad, f"unexpected dead: {bad}" if bad else "")


def stays_dead(name: str, path: str, func: str) -> None:
    key = f"{path}::{func}"
    detail = f"{path}::{func} not in dead set"
    if (path, func) not in DEAD:
        detail += (
            f"; callers={sorted(g.reverse.get(key, ()))}"
            f"; is_root={key in g.roots}"
            f"; tiers={[(d['path'], d['func'], d.get('tier')) for d in dead['candidates'] if d['path'] == path][:4]}"
        )
    check(name, (path, func) in DEAD, detail)


# --- fixture: decor.py -------------------------------------------------
alive("decor: property getter+setter alive", "decor.py", ["total"])
alive("decor: static/class methods alive", "decor.py", ["describe", "with_start"])
alive("decor: attribute-dispatch caller alive", "decor.py", ["use_all"])
stays_dead("decor: control stays dead", "decor.py", "unused_helper")

# --- fixture: vec2.py + dataclass_fields.py ----------------------------
_v2 = g.files["vec2.py"].members
check(
    "dataclass: lowercase-typed fields captured",
    _v2.get("x") == "float" and _v2.get("y") == "float",
    f"vec2 members={_v2}",
)
_mv = g.files["dataclass_fields.py"].members
check(
    "dataclass: consumer field members captured",
    _mv.get("delta") == "Vec2" and _mv.get("label") == "str",
    f"move members={_mv}",
)
alive("dataclass: typed-field chain caller alive", "dataclass_fields.py", ["use_move"])
alive("dataclass: typed-field chain target alive", "dataclass_fields.py", ["length"])
alive(
    "dataclass: cross-file chain reaches provider",
    "vec2.py",
    ["norm"],
)
stays_dead("dataclass: control stays dead", "vec2.py", "unused_vec")
stays_dead("dataclass: consumer control stays dead", "dataclass_fields.py", "unused_move")

# --- fixture: exports.py ------------------------------------------------
alive("__all__: exported funcs are roots", "exports.py", ["public_api", "public_two"])
stays_dead("__all__: non-export stays dead", "exports.py", "private_helper")

# --- fixture: typehints.py ----------------------------------------------
alive("hints: subscript value-type calls alive", "typehints.py", ["sweep", "sweep_all"])
alive("hints: hint-referenced methods alive", "typehints.py", ["refresh", "retire"])
stays_dead("hints: control stays dead", "typehints.py", "unused_hint")

# --- fixture: async_bodies.py -------------------------------------------
alive(
    "async: with-target callers alive",
    "async_bodies.py",
    ["run_upgrade", "run_check"],
)
alive("async: with-target methods alive", "async_bodies.py", ["migrate", "verify"])
alive(
    "async: async-for/with body calls scanned",
    "async_bodies.py",
    ["stream_rows", "handle_row"],
)
stays_dead("async: control stays dead", "async_bodies.py", "unused_async")

# --- fixture: stdlib_overrides.py ---------------------------------------
# http.server handler hooks on an unresolved base are framework
# dispatch (review tier, not likely); the unrelated control stays likely
_tiers = {
    (d["func"]): d["tier"]
    for d in dead["candidates"]
    if d["path"] == "stdlib_overrides.py"
}
check(
    "overrides: stdlib hooks demoted to review",
    _tiers.get("end_headers") == "review"
    and _tiers.get("log_message") == "review"
    and _tiers.get("do_GET") == "review",
    f"tiers={_tiers}",
)
stays_dead("overrides: non-hook control stays dead", "stdlib_overrides.py", "scratch_helper")
check(
    "overrides: control is likely tier (exemption not blanket)",
    _tiers.get("scratch_helper") == "likely",
    f"tiers={_tiers}",
)

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
