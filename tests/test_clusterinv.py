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

# ---------------------------------- 5. scene->scene wiring parity (issue #267)
# scene->scene resource references are composition (a preload scene
# instancing pack scenes), not subsystem coupling: they are tallied in
# their own counter and feed NO cluster number — cross, internal, and
# rule-visible (archrules rides the same tallies). scene<->script wiring
# (attach/signal) stays counted.
cs_sc = [
    {"id": 0, "label": "Hub", "size": 1, "paths": [("main.tscn", "")]},
    {"id": 1, "label": "Pack", "size": 2, "paths": [("pack/a.tscn", ""), ("pack/b.tscn", "")]},
    {"id": 2, "label": "Code", "size": 2, "paths": [("code/c.gd", ""), ("code/d.gd", "")]},
]
g_sc = SimpleNamespace(
    edges={
        "main.tscn::tscn": {
            "pack/a.tscn::tscn": 1,  # scene -> scene cross: carved out
            "code/c.gd::handler": 1,  # scene -> script signal: counted
        },
        "pack/a.tscn::tscn": {"pack/b.tscn::tscn": 1},  # scene -> scene internal: carved out
        "code/d.gd::x": {"code/c.gd::g": 1},  # code internal: counted
    }
)
t_sc = C.cross_tallies(cs_sc, g_sc)
rep_sc = C.crosstalk(cs_sc, g_sc)
check(
    "scene->scene edges tallied in their own counter",
    t_sc.get("scene_scene") == 2,
    str(t_sc.get("scene_scene")),
)
check(
    "scene->scene edges feed no cluster number",
    rep_sc.get("scene_scene_edges") == 2
    and rep_sc["external_edges"] == 1
    and rep_sc["internal_edges"] == 1,
    f"ext {rep_sc['external_edges']} int {rep_sc['internal_edges']}",
)
pack_row = next(r for r in rep_sc["by_cluster"] if r["label"] == "Pack")
check("internal scene composition counts as no wiring", pack_row["internal"] == 0, str(pack_row))
check(
    "worst pairs contain no scene-composition pair",
    len(rep_sc["worst_pairs"]) == 1
    and {rep_sc["worst_pairs"][0]["a"], rep_sc["worst_pairs"][0]["b"]} == {"Hub", "Code"},
    str(rep_sc["worst_pairs"]),
)
out_sc = C.fmt_crosstalk(rep_sc, align=True)
check(
    "fmt names the separately counted scene->scene edges",
    "2 scene->scene resource edges counted separately" in out_sc,
    out_sc,
)
check("fmt silent when no scene->scene edges counted", "scene->scene" not in out2, out2)


# ------------------------- 6. edge-unit semantics + per-pair breakdown (#269)
# one "edge" = one distinct (src fn, dst fn) pair — call-site multiplicity
# collapses in the structural graph; per-file-pair `x<N>` counts the same
# unit per ordered file pair; the per-pair type breakdown counts an edge
# once per carried type, so its sum may exceed the pair total.
cs_sem = [
    {"id": 0, "label": "Ui", "size": 2, "paths": [("ui/panel.gd", ""), ("ui/hud.gd", "")],
     "method": "dir", "confidence": 0.85},
    {"id": 1, "label": "Net", "size": 1, "paths": [("net/client.gd", "")],
     "method": "autoload", "confidence": 1.0},
]
g_sem = SimpleNamespace(
    edges={
        "ui/panel.gd::a": {"net/client.gd::x": 1, "net/client.gd::y": 1},
        "ui/panel.gd::b": {"net/client.gd::x": 1},
        "ui/hud.gd::c": {"net/client.gd::x": 1},
    },
    edge_types={
        ("ui/panel.gd::a", "net/client.gd::x"): {"call"},
        ("ui/panel.gd::a", "net/client.gd::y"): {"call", "var"},  # fn pair carries 2 types
        ("ui/panel.gd::b", "net/client.gd::x"): {"call"},
        ("ui/hud.gd::c", "net/client.gd::x"): {"signal"},
    },
)
rep_sem = C.crosstalk(cs_sem, g_sem)
wp_sem = rep_sem["worst_pairs"][0]
check("edges unit stays distinct fn pairs", wp_sem["edges"] == 4, str(wp_sem["edges"]))
check(
    "per-pair edge-type breakdown attached",
    wp_sem.get("edge_types") == {"call": 3, "var": 1, "signal": 1},
    str(wp_sem.get("edge_types")),
)
check(
    "breakdown order deterministic (count desc, type asc)",
    list(wp_sem.get("edge_types", {})) == ["call", "signal", "var"],
    str(wp_sem.get("edge_types")),
)
out_sem = C.fmt_crosstalk(rep_sem, align=True)
check(
    "fmt renders the per-pair type breakdown",
    "(call 3, signal 1, var 1 | top: ui/panel.gd -> net/client.gd x3" in out_sem,
    out_sem,
)
check(
    "fmt states the edge-unit semantics",
    "edge = one distinct fn pair" in out_sem and "call-kind multiplicity collapsed" in out_sem,
    out_sem,
)


# ------------------------ 7. cluster derivation rides the crosstalk rows (#267)
# method + confidence already ride nav.clusters() output; crosstalk must
# surface them so consumers can discount filename/stem-derived clusters.
rows_sem = {r["label"]: r for r in rep_sem["by_cluster"]}
check(
    "by-cluster rows carry method + confidence",
    rows_sem["Ui"].get("method") == "dir" and rows_sem["Ui"].get("confidence") == 0.85
    and rows_sem["Net"].get("method") == "autoload",
    str(rep_sem["by_cluster"]),
)

check(
    "fmt renders the derivation tag",
    "[dir 0.85]" in out_sem and "[autoload 1.00]" in out_sem,
    out_sem,
)
check(
    "rows without derivation render no tag",
    all(r.get("method") is None for r in rep["by_cluster"]) and "[dir" not in out,
    "",
)

print()
if FAILS:
    print(f"{len(FAILS)} FAIL: {FAILS}")
    sys.exit(1)
print("clusterinv: all checks passed")
