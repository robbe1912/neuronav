# rescan walk + write robustness QA (issue #117) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_walkguard.py
#
# Hermetic: scratch corpus + FAKE store under the system temp dir (no
# self-index pollution, no model server). Four cases from the issue:
#   1. build()'s parse pass isolates a file vanishing between iter_files
#      and parse — skip + stderr note, not an index-wide abort
#   2. the .tres wiring walk prunes config exclude_dirs AND the standard
#      cache floor (.venv/node_modules/.tmp/.neuronav), not just .git/.godot
#   3. overlapping include_dirs ([".", "tests"]) yield each file once —
#      no double "added", no DuplicateIDError from one upsert batch
#   4. sync_functions purge of deleted files still resolves + deletes
#      (all chroma writes now ride nav._db_lock — grep-audited in review)
# plus a fresh-store determinism leg: two independent stores over the same
# corpus embed byte-identically (FAKE vectors are content-seeded).
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---- scratch corpus + config -------------------------------------------------
SCRATCH = Path(tempfile.gettempdir()) / "neuronav_walkguard_scratch"
shutil.rmtree(SCRATCH, ignore_errors=True)
SRC = SCRATCH / "src"
(SRC / "tests").mkdir(parents=True)
(SRC / "keep").mkdir()
(SRC / "vendored").mkdir()
(SRC / ".venv").mkdir()
(SRC / ".tmp").mkdir()
(SRC / "app.py").write_text(
    "def alpha(x: int) -> int:\n    return x + 1\n\n\ndef beta() -> str:\n    return 'b'\n",
    encoding="utf-8",
)
(SRC / "tests" / "test_thing.py").write_text(
    "def probe():\n    return 1\n", encoding="utf-8"
)
# the kept .tres carries BOTH wiring shapes: an ext_resource Script ref
# (harvested into tres_scripts) and a StringName route (referenced_names)
(SRC / "keep" / "wiring.tres").write_text(
    '[gd_resource type="Resource"]\n'
    '[ext_resource type="Script" path="res://app.py"]\n'
    'start_method_name = &"marker_kept"\n',
    encoding="utf-8",
)
# excluded by the CONFIG's exclude_dirs; pre-fix these were read anyway
(SRC / "vendored" / "dep.tres").write_text(
    'start_method_name = &"marker_cfg_excluded"\n', encoding="utf-8"
)
# excluded by the standard floor only (no config entry names them)
(SRC / ".venv" / "junk.tres").write_text(
    'start_method_name = &"marker_venv"\n', encoding="utf-8"
)
(SRC / ".tmp" / "junk.tres").write_text(
    'start_method_name = &"marker_tmp"\n', encoding="utf-8"
)

CFG = {
    "root": str(SRC),
    "collection": "walkguard_fix",
    "state_dir": "default",
    "include_dirs": [".", "tests"],  # overlap is the point (case 3)
    "extensions": [".py"],
    "exclude_dirs": ["vendored"],
}
cfg_a = SCRATCH / "config.json"
cfg_a.write_text(json.dumps(CFG), encoding="utf-8")
os.environ["NEURONAV_CONFIG"] = str(cfg_a)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (binds the scratch config via NEURONAV_CONFIG)
import nav  # noqa: E402

EXPECTED = {"app.py", "tests/test_thing.py"}

# ---- case 3: overlapping include_dirs yield each file exactly once -----------
walked = [nav.file_id(p) for p in nav.iter_files()]
check(
    "overlap: each file yields once (first include wins)",
    len(walked) == len(set(walked)) and set(walked) == EXPECTED,
    str(walked),
)
check(
    "overlap: walk order is the sorted per-dir walk",
    walked == sorted(EXPECTED),
    str(walked),
)
stats = nav.rescan()  # pre-fix: DuplicateIDError from one upsert batch
check(
    "overlap: rescan counts each file once",
    stats["added"] == len(EXPECTED) and nav.count() == len(EXPECTED),
    f"added={stats['added']} count={nav.count()}",
)
warm = nav.rescan()
check(
    "overlap: warm rescan adds nothing (dedupe holds)",
    warm["added"] == 0 and warm["changed"] == [],
    f"added={warm['added']} changed={warm['changed']}",
)

# ---- case 2: .tres wiring walk prunes config + standard floor ----------------
g = graph.Graph().build()
check(
    "tres: kept .tres harvested (script ref + StringName route)",
    "app.py" in g.tres_scripts and "marker_kept" in g.referenced_names,
    f"scripts={sorted(g.tres_scripts)} names={sorted(g.referenced_names)}",
)
check(
    "tres: config exclude_dirs pruned from the traversal",
    "marker_cfg_excluded" not in g.referenced_names,
    str(sorted(g.referenced_names)),
)
check(
    "tres: standard floor pruned without config entries",
    "marker_venv" not in g.referenced_names
    and "marker_tmp" not in g.referenced_names,
    str(sorted(g.referenced_names)),
)

# ---- case 1: parse pass isolates a file vanishing mid-build ------------------
_real_iter = nav.iter_files


def _iter_with_ghost():
    yield from _real_iter()
    yield nav.ROOT / "ghost.py"  # vanishes between iter_files and parse


nav.iter_files = _iter_with_ghost
err = io.StringIO()
try:
    with contextlib.redirect_stderr(err):
        g2 = graph.Graph().build()
finally:
    nav.iter_files = _real_iter
check(
    "vanish: build completes, ghost skipped, real files kept",
    "ghost.py" not in g2.files and EXPECTED <= set(g2.files),
    str(sorted(g2.files)),
)
check(
    "vanish: skip noted on stderr (#19 convention)",
    "parse skipped" in err.getvalue() and "ghost.py" in err.getvalue(),
    repr(err.getvalue()),
)

# ---- case 4: purge of deleted files through the locked write phase ----------
# populate the fn store first (first build indexes every parsed fn) —
# the purge leg below must find something to purge
first = graph.sync_functions(stats["changed"], [])
check(
    "purge: first fn sync populates 3 fns",
    first["fns_upserted"] == 3 and nav.fns_collection().count() == 3,
    str(first),
)
(SRC / "tests" / "test_thing.py").unlink()
stats3 = nav.rescan()
check(
    "purge: rescan reports the deleted path once",
    stats3["deleted_paths"] == ["tests/test_thing.py"],
    str(stats3["deleted_paths"]),
)
fns = graph.sync_functions(stats3["changed"], stats3["deleted_paths"])
check(
    "purge: sync purges the deleted file's fns",
    fns["purged_paths"] == 1 and fns["purged_fns"] == 1,
    str(fns),
)

# ---- fresh-store determinism: two stores, same corpus, same bytes -----------
CHILD = SCRATCH / "child_probe.py"
CHILD.write_text(
    "import hashlib\n"
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "\n"
    "import graph\n"
    "import nav\n"
    "\n"
    "stats = nav.rescan()\n"
    "graph.sync_functions(stats['changed'], stats['deleted_paths'])\n"
    "rows = []\n"
    "for label, col in (('files', nav._collection()), ('fns', nav.fns_collection())):\n"
    "    got = col.get(include=['embeddings', 'metadatas'])\n"
    "    for rid, emb, met in zip(got['ids'], got['embeddings'], got['metadatas']):\n"
    "        e = hashlib.sha256('|'.join(f'{x:.9g}' for x in emb).encode()).hexdigest()[:16]\n"
    "        m = hashlib.sha256(repr(sorted((met or {}).items())).encode()).hexdigest()[:16]\n"
    "        rows.append(f'{label} {rid} emb={e} meta={m}')\n"
    "print('\\n'.join(sorted(rows)))\n",
    encoding="utf-8",
)
outs = []
for i in (1, 2):
    cfg_i = SCRATCH / f"config_store{i}.json"
    cfg_i.write_text(json.dumps({**CFG, "state_dir": str(SCRATCH / f"store{i}")}), encoding="utf-8")
    env = {**os.environ, "NEURONAV_CONFIG": str(cfg_i)}
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(CHILD)],
        capture_output=True, text=True, env=env, cwd=str(HERE),
    )
    check(f"determinism: store{i} child completed", proc.returncode == 0, proc.stderr[-400:])
    outs.append(proc.stdout)
check(
    "determinism: two fresh stores embed + store identical rows",
    outs[0] == outs[1] and len(outs[0].splitlines()) >= 3,
    f"rows={len(outs[0].splitlines())} identical={outs[0] == outs[1]}",
)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(("WALKGUARD OK" if not FAILS else f"WALKGUARDFAILS: {FAILS}"))
sys.exit(1 if FAILS else 0)
