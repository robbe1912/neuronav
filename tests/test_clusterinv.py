# cluster partition + crosstalk parity QA (issue #114) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_clusterinv.py
#
# Hermetic: drives finalize() on crafted shapes (no index; chroma is only
# touched by the clusters->nav import) and crosstalk() on a stub graph, so
# the checks pin the invariants themselves:
#   - finalize ends with every file in exactly ONE cluster — the routing
#     passes move partial families (_family_unit leaves other-pack scenes
#     behind) while cap-enforce's split/chunk paths regroup FULL welded
#     units, so a weld broken earlier plus a part split later re-pulls
#     members that already moved: #114's double assignment
#   - crosstalk counts only edges the clusterer's structural graph could
#     see: tests/ endpoints (which communities_graph never wires) are
#     tallied separately and feed no cluster number (#114)
#   - the repair is a no-op on healthy partitions and deterministic
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


cfg = Path(tempfile.gettempdir()) / "neuronav_clusterinv_config.json"
cfg.write_text(
    json.dumps(
        {
            "root": str(HERE),
            "collection": "clusterinv_fix",
            "state_dir": "default",
            "include_dirs": ["tests"],
            "extensions": [".gd"],
            "exclude_dirs": [],
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(cfg)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

import clusters as C  # noqa: E402  (binds the temp config via NEURONAV_CONFIG)

# ---------------------------------------------------------------- fixture
# big part = welded unit + 5 other-pack scenes + 65 fillers (73 > PART_CAP),
# home part = 3 same-pack scenes; identical embedding vectors so no pass
# divides by similarity and cap-enforce falls to the deterministic chunking
# path (the strongest re-pull: it regroups by FULL welded units).
WELD = ["src/aa_earth_rumble.tscn", "src/ff_fire_burst.tscn", "src/mm_shared.gd"]
FILLERS = [f"src/f{i:02d}.gd" for i in range(65)]
GRASS = [f"src/gg_grass_{c}.tscn" for c in "abcde"]
HOME = ["src/ff_fire_glow.tscn", "src/ff_fire_howl.tscn", "src/ff_fire_spark.tscn"]


def dup_shape():
    ids = sorted(FILLERS + GRASS + WELD + HOME)
    mat = np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), (len(ids), 1))
    raw = [
        {
            "id": 0,
            "size": len(FILLERS) + len(GRASS) + len(WELD),
            "paths": [(p, "Cls") for p in sorted(FILLERS + GRASS + WELD)],
        },
        {"id": 1, "size": len(HOME), "paths": [(p, "Cls") for p in sorted(HOME)]},
    ]
    return ids, mat, raw, [sorted(WELD)]


def canon(cs):
    return sorted((p, c["id"]) for c in cs for p, _ in c["paths"])


# ---------------------------------------------- 1. double assignment repaired
ids, mat, raw, units = dup_shape()
cs = C.finalize(raw, ids, mat, adj=None, units=units)
entries = [(p, c["id"]) for c in cs for p, _ in c["paths"]]
counts = Counter(p for p, _ in entries)
check(
    "double-assign repaired: every file exactly one cluster",
    len(entries) == len(set(entries)) and max(counts.values()) == 1,
    f"entries={len(entries)} unique={len(set(entries))} "
    f"dups={sorted(p for p, n in counts.items() if n > 1)}",
)
check(
    "no part holds a path twice",
    all(len({p for p, _ in c["paths"]}) == len(c["paths"]) for c in cs),
)
check(
    "repair drops no file",
    {p for p, _ in entries} == set(ids),
    f"{len(set(ids)) - len({p for p, _ in entries})} files lost",
)
which = dict((p, cid) for p, cid in entries)
check(
    "weld plurality keeps the weld whole",
    len({which[q] for q in WELD}) == 1,
    f"weld spread over {[which[q] for q in WELD]}",
)
check(
    "home pack scenes stay together",
    len({which[q] for q in HOME}) == 1,
    f"home spread over {[which[q] for q in HOME]}",
)

# ------------------------------------- 2. no-op + determinism on healthy shape
healthy_parts = [
    {"id": 0, "size": 3, "paths": [(p, "A") for p in sorted(WELD)]},
    {"id": 1, "size": 3, "paths": [(p, "B") for p in sorted(HOME)]},
    {"id": 2, "size": 2, "paths": [("src/x.gd", "X"), ("src/y.gd", "Y")]},
]
same = C._pass_partition(
    [dict(p, paths=list(p["paths"])) for p in healthy_parts], {q: sorted(WELD) for q in WELD}
)
check(
    "partition pass is identity on disjoint input",
    [dict(p, paths=list(p["paths"])) for p in same]
    == [dict(p, paths=list(p["paths"])) for p in healthy_parts]
    and all(p["size"] == len(p["paths"]) for p in same),
)
cs2 = C.finalize(raw, ids, mat, adj=None, units=units)
check("finalize deterministic on dup shape", canon(cs2) == canon(cs))

# within-part amplification collapse: the kept part keeps ONE copy
amp = [dict(p) for p in healthy_parts]
amp[0]["paths"] = list(amp[0]["paths"]) + [amp[0]["paths"][0]]
amp[0]["size"] = len(amp[0]["paths"])
fixed = C._pass_partition(amp, {})
check(
    "amplified within-part copy collapses to one",
    len(fixed) == 3
    and len(fixed[0]["paths"]) == 3
    and fixed[0]["paths"][0] == (WELD[0], "A"),
    str(fixed[0]["paths"]),
)

# ------------------------------------------------ 3. crosstalk tests parity
cs_stub = [
    {"id": 0, "label": "Tests", "size": 1, "paths": [("tests/t1.gd", "")]},
    {
        "id": 1,
        "label": "Core",
        "size": 2,
        "paths": [("core/a.gd", ""), ("core/b.gd", "")],
    },
    {"id": 2, "label": "Util", "size": 1, "paths": [("util/u.gd", "")]},
]
g_stub = SimpleNamespace(
    edges={
        "tests/t1.gd::run": {"core/a.gd::fn": 1},  # tests -> prod: excluded
        "tests/t1.gd::x": {"tests/t2.gd::y": 1},  # tests -> tests: excluded
        "core/a.gd::fn": {  # internal Core + cross to Util + same-file
            "core/b.gd::fn": 1,
            "util/u.gd::fn": 1,
            "core/a.gd::fn2": 1,
        },
        "core/b.gd::fn": {"other/o.gd::fn": 1},  # unclustered endpoint
    }
)
rep = C.crosstalk(cs_stub, g_stub)
check("internal counts only clustered wiring", rep["internal_edges"] == 1, str(rep["internal_edges"]))
check(
    "external counts only clustered wiring",
    rep["external_edges"] == 1,
    str(rep["external_edges"]),
)
check("external ratio over counted only", rep["external_ratio"] == 0.5)
check("tests endpoints tallied separately", rep["tests_endpoint_edges"] == 2)
check("unclustered endpoints still tallied", rep["unclustered_endpoint_edges"] == 1)
tests_row = next(r for r in rep["by_cluster"] if r["id"] == 0)
check(
    "Tests cluster row carries no counted wiring",
    tests_row["internal"] == 0 and tests_row["external_out"] == 0 and tests_row["external_in"] == 0,
    str(tests_row),
)
check(
    "worst pairs contain no tests-driven pair",
    len(rep["worst_pairs"]) == 1
    and {rep["worst_pairs"][0]["a"], rep["worst_pairs"][0]["b"]} == {"Core", "Util"},
    str(rep["worst_pairs"]),
)

# ---------------------------------------------- 4. formatter carries the line
out = C.fmt_crosstalk(rep, align=True)
check(
    "fmt names the excluded tests edges",
    "2 edges touch tests/ files — excluded: the clusterer never wires tests" in out,
)
check("fmt keeps the unclustered line", "1 edges touch unclustered files" in out)
no_tests = C.crosstalk(cs_stub[1:], SimpleNamespace(edges={"core/a.gd::fn": {"core/b.gd::fn": 1}}))
out2 = C.fmt_crosstalk(no_tests)
check("fmt silent when no tests edges counted", "tests/" not in out2 and "unclustered" not in out2, out2)

print()
if FAILS:
    print(f"{len(FAILS)} FAIL: {FAILS}")
    sys.exit(1)
print("clusterinv: all checks passed")
