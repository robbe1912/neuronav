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
#   - topk_desc (#393) stays byte-identical to the argsort reference
#     ``np.argsort(-sim, axis=1)[:, :k]`` on tie-heavy corpora while its
#     transient memory is chunk-bounded — the monolithic rows·n arrays
#     (negation, argpartition index slab, tie scan, tied-row fallback
#     argsort) peaked ~21GB at 33k tie-heavy docs
#   - the repair is a no-op on healthy partitions and deterministic
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]



from harness import FAILURES as FAILS, check

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
# method + confidence already ride navstore.clusters() output; crosstalk must
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

# -------------------------- 8. engine contract defects (issue #294)
# (a) pass-1 PRIMARY welds carry the same member cap as pass 2: 80
# scenes attaching one shared root script must not weld into a single
# 81-member unit. _split_units and cap-enforce's chunk fallback group
# WHOLE welds — an oversized weld can never be divided downstream, so
# PART_CAP=70 was unenforceable the moment it formed.
class _FS294:
    def __init__(self, att="", scripts=()):
        self.attached_script = att
        self.scripts = list(scripts)


class _G294:
    def __init__(self, files):
        self.files = files


_scenes = [f"art/scene_{i:02}.tscn" for i in range(80)]
_shared = "scripts/shared.gd"
_files = {s: _FS294(att="res://scripts/shared.gd") for s in _scenes}
_files[_shared] = _FS294()
_ids = sorted(_scenes + [_shared])
_id_of = {p: i for i, p in enumerate(_ids)}
_, _members = C._weld_units(_ids, set(), _id_of, _G294(_files))
_big = max(len(m) for m in _members.values())
check(
    "pass-1 welds capped: shared primary script can't weld 80 scenes",
    _big <= C.PART_CAP,
    f"largest welded unit = {_big} (PART_CAP={C.PART_CAP})",
)
# below the cap the canonical weld still fires: a small scene set
# sharing one primary stays ONE atomic unit
_files3 = {s: _FS294(att="res://scripts/shared.gd") for s in _scenes[:3]}
_files3[_shared] = _FS294()
_ids3 = sorted(_scenes[:3] + [_shared])
_, _mem3 = C._weld_units(_ids3, set(), {p: i for i, p in enumerate(_ids3)}, _G294(_files3))
check(
    "pass-1 cap keeps small legitimate welds whole",
    max(len(m) for m in _mem3.values()) == 4,
    str(sorted(len(m) for m in _mem3.values())),
)
# end-to-end: the welded units flow into finalize and the 81-file blob
# raw part must leave cap-enforce under PART_CAP (identical vectors, so
# no similarity cut divides and the unit-aware chunking path decides)
_units81 = [sorted(_ids[m] for m in mem) for mem in _members.values()]
_mat81 = np.tile(np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32), (81, 1))
_cs81 = C.finalize(
    [{"id": 0, "size": 81, "paths": [(p, "") for p in _ids]}],
    _ids, _mat81, adj=None, units=_units81,
)
_max81 = max(c["size"] for c in _cs81)
check(
    "cap-enforce bounds the 80-scene blob end-to-end (PART_CAP real)",
    _max81 <= C.PART_CAP,
    f"largest final part = {_max81} (PART_CAP={C.PART_CAP})",
)

# (b) scene-majority anchor: the once-per-unit gate must anchor on the
# unit's first SCENE member. Sorted unit lists put .gd before .tscn, so
# u[0] silently skipped the canonical foo.gd+foo.tscn weld — the pass
# never ran for exactly the layout it was written for.
_adj_ctl = {"aaa/s.tscn": {"tgt/a.gd": 6.0, "tgt/b.gd": 6.0}, "zzz/a.gd": {}}
_unit_ctl = ["aaa/s.tscn", "zzz/a.gd"]  # scene sorts first: old anchor fired
_parts_ctl = [
    {"paths": [("aaa/s.tscn", ""), ("zzz/a.gd", "")], "size": 2},
    {"paths": [("tgt/a.gd", ""), ("tgt/b.gd", ""), ("tgt/c.gd", "")], "size": 3},
]
_out_ctl = C._pass_scene_majority(_parts_ctl, _adj_ctl, {p: _unit_ctl for p in _unit_ctl})
check(
    "scene-majority control moves a scene-anchored unit",
    len(_out_ctl) == 1
    and {e[0] for e in _out_ctl[0]["paths"]}
    == {"aaa/s.tscn", "zzz/a.gd", "tgt/a.gd", "tgt/b.gd", "tgt/c.gd"},
    str(_out_ctl),
)
_adj_maj = {"ui/s.tscn": {"tgt/a.gd": 6.0, "tgt/b.gd": 6.0}, "ui/s.gd": {}}
_unit_maj = ["ui/s.gd", "ui/s.tscn"]  # script sorts first: the canonical pair
_parts_maj = [
    {"paths": [("ui/s.gd", "S"), ("ui/s.tscn", "")], "size": 2},
    {"paths": [("tgt/a.gd", ""), ("tgt/b.gd", ""), ("tgt/c.gd", "")], "size": 3},
]
_out_maj = C._pass_scene_majority(_parts_maj, _adj_maj, {p: _unit_maj for p in _unit_maj})
check(
    "scene-majority runs for script-anchored units (.gd < .tscn)",
    len(_out_maj) == 1
    and {e[0] for e in _out_maj[0]["paths"]}
    == {"ui/s.gd", "ui/s.tscn", "tgt/a.gd", "tgt/b.gd", "tgt/c.gd"},
    str(_out_maj),
)
# ------------------ 9. hash-seed byte stability of the partition (#373)
# g.edges values are SETS: sweeping them unsorted let PYTHONHASHSEED
# order flow into the pair-Counter key order -> networkx edge-insertion
# order -> louvain's float-sum gain comparisons, so near-tie communities
# could flip across processes — a hash-seed channel into partition
# identity (byte-stability law; the module docstring promises
# determinism). Both sweep sites sort now (:366 region, :1620 twin);
# this leg pins the CLASS, not the site: two subprocesses under
# different hash seeds must agree byte-for-byte on a graph whose
# src->dsts sets permute per seed, and each run must equal its own
# immediate repeat.
_HS_CHILD = """
import hashlib
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
import numpy as np

import graph
import clusters as C

# corpus 1 — realistic mix: each source fn wires a SET of 8 cross-dir
# destinations (set order permutes per hash seed); weights mix capped
# call counts with semantic s*0.7 floats
ids = sorted(f"{d}/{n}.gd" for d in ("core", "net", "ui") for n in "abcdefgh")
edges, etypes = {}, {}
for si, s in enumerate(ids):
    dsts = {f"{ids[(si + k * 3 + 3) % len(ids)]}::fn" for k in range(8)}
    dsts.discard(f"{s}::fn")
    edges[f"{s}::fn"] = dsts
    for d in dsts:
        etypes[(f"{s}::fn", d)] = {"call"} if (si + len(d)) % 2 else {"signal"}

# corpus 2 — tie forge: graduated cliques (a_i<->a_j weight i+j+1,
# tie-free intra-clique merges) plus border node 0x.gd wired into a0
# and b0 with exactly equal weight. Pre-fix, the border node's home
# flipped with PYTHONHASHSEED (seeds 1-4/6/10/11 -> A clique, seeds
# 5/7-9/12 -> B): louvain's strictly-greater gain loop breaks exact
# ties on the first-encountered community, which rode adjacency
# insertion order, which rode the unsorted dst-set sweep
ids2 = ["0x.gd"] + sorted(f"{c}{i}.gd" for c in "ab" for i in range(5))
edges2, etypes2 = {}, {}


def _wiren(s, d, n):
    k = f"{s}::fn"
    edges2.setdefault(k, set()).update(f"{d}::g{j}" for j in range(n))
    for j in range(n):
        etypes2[(k, f"{d}::g{j}")] = {"call"}


for c in "ab":
    for i in range(5):
        for j in range(5):
            if i != j:
                _wiren(f"{c}{i}.gd", f"{c}{j}.gd", min(i + j + 1, 5))
_wiren("0x.gd", "a0.gd", 5)
_wiren("0x.gd", "b0.gd", 5)

def _corpus(ids, edges, etypes, mat):
    graph.get_graph = lambda: SimpleNamespace(
        edges=edges, edge_types=etypes, files={}
    )
    sim = (mat @ mat.T).astype(np.float32)
    np.fill_diagonal(sim, -1.0)
    return ids, mat, sim, C.topk_desc(sim, 6)


mat1 = np.random.default_rng(7).random((len(ids), 16), dtype=np.float32)
mat1 /= np.linalg.norm(mat1, axis=1, keepdims=True)
corpus1 = _corpus(ids, edges, etypes, mat1)
# centered vectors keep corpus 2 free of incidental semantic structure;
# min_sim above 1 leaves the forged integer-weight ties as the only
# forces on x
mat2 = np.random.default_rng(11).standard_normal((len(ids2), 16)).astype(
    np.float32
)
def run(c_ids, c_mat, c_sim, c_knn, c_min_sim):
    out, adj, units = C.communities_graph(
        c_ids, [{} for _ in c_ids], c_mat, c_sim, c_knn, min_sim=c_min_sim
    )
    out.sort(key=lambda c: -int(c["size"]))
    for i, c in enumerate(out):
        c["id"] = i
    raw = sorted((p, c["id"]) for c in out for p, _ in c["paths"])
    cs = C.finalize(out, c_ids, c_mat, adj=adj, units=units)
    fin = sorted((p, c["id"], c.get("label")) for c in cs for p, _ in c["paths"])
    return [raw, fin]


mat2 /= np.linalg.norm(mat2, axis=1, keepdims=True)
corpus2 = _corpus(ids2, edges2, etypes2, mat2)


canon1 = run(*corpus1, 0.6)
canon2 = run(*corpus2, 1.1)
digest = hashlib.sha256(json.dumps([canon1, canon2]).encode("utf-8")).hexdigest()
print(digest, digest)
"""

with tempfile.TemporaryDirectory(prefix="neuronav_hashseed_") as td:
    _child = Path(td) / "seed_child.py"
    _child.write_text(_HS_CHILD, encoding="utf-8")
    _digests = []
    for _seed in ("1", "5"):
        _r = subprocess.run(
            [sys.executable, "-X", "utf8", str(_child), str(HERE)],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONHASHSEED=_seed),
            cwd=str(HERE),
        )
        check(
            f"hashseed child ran clean (PYTHONHASHSEED={_seed})",
            _r.returncode == 0 and len(_r.stdout.split()) == 2,
            (_r.stderr or _r.stdout)[-400:],
        )
        _digests.append(_r.stdout.split())
    check(
        "partition byte-identical across hash seeds (#373)",
        bool(_digests[0]) and _digests[0] == _digests[1],
        f"seed1={_digests[0]} seed2={_digests[1]}",
    )
    check(
        "partition byte-identical on consecutive runs",
        len(_digests[0]) == 2 and _digests[0][0] == _digests[0][1],
        str(_digests[0]),
    )

# ------------------ 10. topk_desc chunked exactness + memory (#393)
# The selection engine feeds louvain's edge insertion order, so its law is
# byte identity with the argsort reference ``np.argsort(-sim, axis=1)[:, :k]``
# on EVERY row — ties included (the fallback carries them: quicksort's tie
# order is part of the pinned bytes). #393 chunks the pass; chunks may not
# shift one byte (every op is row-independent), and the chunking must
# actually bound the rows·n transients the monolithic form allocated —
# negation, argpartition's int64 index slab, the tie scan, the tied-row
# fallback argsort — which peaked ~21GB at 33k tie-heavy docs on the rig.
import hashlib
import tracemalloc

_t_rng = np.random.default_rng(393)
# tie forge A: 7-value alphabet -> duplicates straddle every k-cut (the
# degenerate/fake-embed shape: EVERY row falls to the argsort fallback);
# 1500 rows > the 512-row chunk cap, so the last block is ragged
_TIE = _t_rng.integers(0, 7, size=(1500, 1500)).astype(np.float32)
np.fill_diagonal(_TIE, -1.0)
# tie forge B: banded floats (values in {0, .25, .5, .75, 1}) — near-real
# distributions with wide flat plateaus; C: sub-chunk corpus stays on the
# single-block path
_BAND = np.round(_t_rng.random((600, 600), dtype=np.float32) * 4) / 4
np.fill_diagonal(_BAND, -1.0)
_SMALL = _TIE[:300, :300].copy()
for _s, _label in ((_TIE, "alphabet"), (_BAND, "banded"), (_SMALL, "sub-chunk")):
    for _k in (1, 6, _s.shape[1] - 1):
        _got = C.topk_desc(_s, _k)
        _ref = np.argsort(-_s, axis=1)[:, :_k]
        check(
            f"topk_desc byte-identical to argsort ({_label}, k={_k})",
            _got.dtype == _ref.dtype and np.array_equal(_got, _ref),
        )

# identity law, digest form: same data -> same neighbor bytes, run to run
_d1 = hashlib.sha256(C.topk_desc(_TIE, 6).tobytes()).hexdigest()
_d2 = hashlib.sha256(C.topk_desc(_TIE, 6).tobytes()).hexdigest()
check("topk_desc double-run digest stable (#393)", _d1 == _d2, f"{_d1} != {_d2}")

# memory ceiling: only the pass's transients are traced (the corpus is
# built first). Monolithic at 3000^2 holds ~108MB live at once (36MB
# negation + 72MB index slab; the fallback repeats the pair); the chunked
# pass holds <=512*3000*(4+8+1 + fallback 4+8)B ~ 37MB. The 64MB bar
# fails the monolithic form with wide margin, passes the chunked one.
_MEM = _t_rng.integers(0, 7, size=(3000, 3000)).astype(np.float32)
np.fill_diagonal(_MEM, -1.0)
tracemalloc.start()
C.topk_desc(_MEM, 6)
_pk = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
check(
    "topk_desc transient peak chunk-bounded (#393)",
    _pk < 64 << 20,
    f"traced peak {_pk >> 20}MB >= 64MB (rows*n monolithic form)",
)

# summary tail is a pre-#301 byte pin (names failures)
print()
if FAILS:
    print(f"{len(FAILS)} FAIL: {FAILS}")
    sys.exit(1)
print("clusterinv: all checks passed")
