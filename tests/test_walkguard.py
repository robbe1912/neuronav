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
#      (all chroma writes now ride navstore._db_lock — grep-audited in review)
# plus a fresh-store determinism leg: two independent stores over the same
# corpus embed byte-identically (FAKE vectors are content-seeded).
# plus issue #286: bare config-less defaults exclude .tmp/.team_scratch on
# both walk surfaces, and the once-only scope hint fires only for oversized
# bare walks at a git-root cwd (silent under config, FAKE, or non-repo cwd)
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



from harness import FAILURES as FAILS, check

# ---- scratch corpus + config -------------------------------------------------
# issue #321 lever 3: mkdtemp per run — fixed names collided across
# concurrent same-suite processes (twin battery/standalone runs); the
# end-of-suite rmtree below still cleans up
SCRATCH = Path(tempfile.mkdtemp(prefix="neuronav_walkguard_"))
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
import navconfig, navindex, navstore

EXPECTED = {"app.py", "tests/test_thing.py"}

# ---- case 3: overlapping include_dirs yield each file exactly once -----------
walked = [navindex.file_id(p) for p in navindex.iter_files()]
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
stats = navindex.rescan()  # pre-fix: DuplicateIDError from one upsert batch
check(
    "overlap: rescan counts each file once",
    stats["added"] == len(EXPECTED) and navstore.count() == len(EXPECTED),
    f"added={stats['added']} count={navstore.count()}",
)
warm = navindex.rescan()
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
_real_iter = navindex.iter_files


def _iter_with_ghost():
    yield from _real_iter()
    yield navconfig.ROOT / "ghost.py"  # vanishes between iter_files and parse


navindex.iter_files = _iter_with_ghost
err = io.StringIO()
try:
    with contextlib.redirect_stderr(err):
        g2 = graph.Graph().build()
finally:
    navindex.iter_files = _real_iter
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
    first["fns_upserted"] == 3 and navstore.fns_collection().count() == 3,
    str(first),
)
(SRC / "tests" / "test_thing.py").unlink()
stats3 = navindex.rescan()
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

# ---- issue #374: file vanishing between listing and read mid-rescan ---------
# Case 1 guards the parse pass; the rescan's own sha/read leg must guard
# the same race — skip with the stderr note and drop the id from `seen`
# so the purge leg reconciles it in the SAME pass (pre-fix: the
# FileNotFoundError aborted the whole rescan).
(SRC / "vanish.py").write_text("def gone():\n    return 1\n", encoding="utf-8")
grow = navindex.rescan()
check(
    "vanish-mid-rescan: setup leg indexes the file",
    grow["added"] == 1 and navstore.count() == 2,
    f"added={grow['added']} count={navstore.count()}",
)
# drift the stat fingerprint so the warm gate cannot skip the read leg
(SRC / "vanish.py").write_text(
    "def gone(x: int) -> int:\n    return x + 1\n", encoding="utf-8"
)
_real_sha = navindex.sha256_of
_vanish_path = SRC / "vanish.py"


def _sha_deletes(path):
    if path == _vanish_path:
        _vanish_path.unlink()  # the vanish: between listing and read
    return _real_sha(path)


navindex.sha256_of = _sha_deletes
err374 = io.StringIO()
try:
    with contextlib.redirect_stderr(err374):
        vanish_stats = navindex.rescan()
finally:
    navindex.sha256_of = _real_sha
check(
    "vanish-mid-rescan: rescan completes, file purged same pass (#374)",
    vanish_stats["deleted_paths"] == ["vanish.py"] and navstore.count() == 1,
    f"deleted={vanish_stats['deleted_paths']} count={navstore.count()}",
)
check(
    "vanish-mid-rescan: skip noted on stderr (#19 convention)",
    "rescan skipped" in err374.getvalue() and "vanish.py" in err374.getvalue(),
    repr(err374.getvalue()),
)

# ---- fresh-store determinism: two stores, same corpus, same bytes -----------
CHILD = SCRATCH / "child_probe.py"
CHILD.write_text(
    "import hashlib\n"
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "\n"
    "import graph\n"
    "import nav, navconfig, navstore, navindex\n"
    "\n"
    "stats = navindex.rescan()\n"
    "graph.sync_functions(stats['changed'], stats['deleted_paths'])\n"
    "rows = []\n"
    "for label, col in (('files', navstore._collection()), ('fns', navstore.fns_collection())):\n"
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

# ---- issue #286: bare-defaults scratch exclusion + once-only scope hint -----
# The suite process is config-bound (cfg_a above), so these legs run as
# children under a second scratch root; the guard threshold is lowered in
# the child to keep the fixture tiny — the crossing logic is the contract.
SCRATCH2 = Path(tempfile.mkdtemp(prefix="neuronav_walkguard_bare_"))
TREE = SCRATCH2 / "tree"
for sub in (".git", ".tmp", ".team_scratch", "team_scratch", "src"):
    (TREE / sub).mkdir(parents=True)
(TREE / "src" / "app.py").write_text(
    "def main():\n    pass\n", encoding="utf-8"
)
(TREE / ".team_scratch" / "civenv").mkdir()
(TREE / ".tmp" / "junk.py").write_text("tmp_junk = 1\n", encoding="utf-8")
(TREE / ".team_scratch" / "civenv" / "junk.py").write_text(
    "scratch_junk = 1\n", encoding="utf-8"
)
(TREE / "team_scratch" / "junk.py").write_text(
    "near_miss = 1\n", encoding="utf-8"
)

GUARD_PROBE = SCRATCH2 / "guard_probe.py"
GUARD_PROBE.write_text(
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "import nav, navconfig, navstore, navindex\n"
    "navindex.WALK_SCOPE_WARN_N = 2\n"
    "names = sorted(str(p.relative_to(navconfig.ROOT)).replace(chr(92), '/')\n"
    "               for p in navindex.iter_files())\n"
    "sum(1 for _ in navindex.iter_files())\n"
    "navindex.stat_fingerprint()\n"
    "print('|'.join(names))\n",
    encoding="utf-8",
)
CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k not in ("NEURONAV_CONFIG", "NEURONAV_EMBED_FAKE", "NEURONAV_STAT_TTL_S")
}


def _guard_lines(proc):
    return [ln for ln in proc.stderr.splitlines() if "walk scope" in ln]


proc = subprocess.run(
    [sys.executable, "-X", "utf8", str(GUARD_PROBE)],
    capture_output=True, text=True, env=CLEAN_ENV, cwd=str(TREE),
)
check("bare walk: child completed", proc.returncode == 0, proc.stderr[-400:])
check(
    "bare walk: .tmp/.team_scratch excluded, near-miss dir kept (issue #286)",
    proc.stdout.strip() == "src/app.py|team_scratch/junk.py",
    proc.stdout.strip(),
)
warns = _guard_lines(proc)
check(
    "bare walk: scope hint fires exactly once across walk + refingerprint",
    len(warns) == 1 and "bare defaults" in warns[0] and "NEURONAV_CONFIG" in warns[0],
    f"lines={len(warns)}",
)

cfg_bare = SCRATCH2 / "config_bare.json"
cfg_bare.write_text(
    json.dumps({
        "root": str(TREE),
        "collection": "walkguard_bare",
        "state_dir": "default",
        "include_dirs": ["."],
        "extensions": [".py"],
        "exclude_dirs": [],
    }),
    encoding="utf-8",
)
for label, env in (
    ("configured walk", {**CLEAN_ENV, "NEURONAV_CONFIG": str(cfg_bare)}),
    ("FAKE/hermetic run", {**CLEAN_ENV, "NEURONAV_EMBED_FAKE": "1"}),
    ("non-repo cwd", CLEAN_ENV),
):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(GUARD_PROBE)],
        capture_output=True, text=True, env=env,
        cwd=str(SCRATCH2 if label == "non-repo cwd" else TREE),
    )
    warns = _guard_lines(proc)
    check(f"{label}: scope hint stays silent", len(warns) == 0, f"lines={len(warns)}")

# ---- issue #296: include fallback walks everything; gitignore-aware
# walking; one canonical prune set -------------------------------------------
# Children under a third scratch root (the suite process stays bound to
# cfg_a): (A) a config WITHOUT include_dirs must walk + index a generic
# tree — the pre-#296 fallback to one Godot repo's layout ("scripts",
# "scenes", "VFX", "ai", "tests", "tools") indexed nothing on any other
# tree; (B) the root .gitignore's dir-entry lines prune like .neuroignore
# names — bare names (trailing slash optional); anchored ("/src/"), glob
# ("*.log") and "!" negation lines stay git's business, and a .neuroignore
# name still excludes despite a "!name" negation; (C) gitignore-sourced
# prunes stay loud: ONE stderr note when a name keeps >= WARN_N files
# out of the walk, once per process, silent under FAKE embeds; (D) the
# walk-all exclude default and the wiring floor derive from one canonical
# set (post-#290 drift: __pycache__/.team_scratch missing floor-side,
# .godot missing defaults-side).
SCRATCH3 = Path(tempfile.mkdtemp(prefix="neuronav_walkguard_296_"))
T3 = SCRATCH3 / "tree"
for sub in ("src", "tools", "scripts", "target", "genout", ".tmp", ".git", "keepme"):
    (T3 / sub).mkdir(parents=True)
(T3 / "src" / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
(T3 / "tools" / "helper.py").write_text("def helper():\n    pass\n", encoding="utf-8")
(T3 / "scripts" / "godot_side.py").write_text("def gd():\n    pass\n", encoding="utf-8")
(T3 / "target" / "gen.py").write_text("target_junk = 1\n", encoding="utf-8")
for i in range(5):
    (T3 / "genout" / f"g{i}.py").write_text("gen_junk = 1\n", encoding="utf-8")
    (T3 / ".tmp" / f"t{i}.py").write_text("tmp_junk = 1\n", encoding="utf-8")
    (T3 / ".git" / f"n{i}.py").write_text("git_junk = 1\n", encoding="utf-8")
(T3 / "keepme" / "keep.py").write_text("def kept():\n    pass\n", encoding="utf-8")
(T3 / ".gitignore").write_text(
    "# build output\ntarget/\ngenout/\n.tmp/\n/src/\n*.log\n!keepme\n",
    encoding="utf-8",
)
cfg_296 = SCRATCH3 / "config_296.json"
cfg_296.write_text(json.dumps({
    "root": str(T3),
    "collection": "walkguard_296",
    "state_dir": "default",
    "extensions": [".py"],  # no include_dirs: leg A's whole point
}), encoding="utf-8")

PROBE_A = SCRATCH3 / "probe_a.py"
PROBE_A.write_text(
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "import nav, navconfig, navstore, navindex\n"
    "names = sorted(str(p.relative_to(navconfig.ROOT)).replace(chr(92), '/')\n"
    "               for p in navindex.iter_files())\n"
    "print(navconfig.INCLUDE_DIRS)\n"
    "print('|'.join(names))\n"
    "navindex.rescan()\n"
    "print(navstore.count())\n",
    encoding="utf-8",
)
proc = subprocess.run(
    [sys.executable, "-X", "utf8", str(PROBE_A)],
    capture_output=True, text=True,
    env={**CLEAN_ENV, "NEURONAV_CONFIG": str(cfg_296), "NEURONAV_EMBED_FAKE": "1"},
    cwd=str(HERE),
)
check("296: include-fallback child completed", proc.returncode == 0, proc.stderr[-400:])
lines_a = [ln for ln in proc.stdout.strip().splitlines() if ln]
check(
    "296-A: config without include_dirs walks everything (opt-in only)",
    len(lines_a) == 3
    and lines_a[0] == "('.',)"
    and lines_a[1] == "keepme/keep.py|scripts/godot_side.py|src/app.py|tools/helper.py"
    and lines_a[2] == "4",
    proc.stdout.strip(),
)

# .neuroignore precedence: "keepme" excluded via .neuroignore even though
# the root .gitignore carries a "!keepme" negation — the exclude union is
# additive-only; gitignore processing can never re-include
CFGDIR = SCRATCH3 / "cfgdir"
CFGDIR.mkdir()
(CFGDIR / ".neuroignore").write_text("keepme\n", encoding="utf-8")
cfg_prec = CFGDIR / "config.json"
cfg_prec.write_text(json.dumps({
    "root": str(T3),
    "collection": "walkguard_296_prec",
    "state_dir": "default",
    "extensions": [".py"],
}), encoding="utf-8")
PROBE_D = SCRATCH3 / "probe_d.py"
PROBE_D.write_text(
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "import nav, navconfig, navstore, navindex\n"
    "names = sorted(str(p.relative_to(navconfig.ROOT)).replace(chr(92), '/')\n"
    "               for p in navindex.iter_files())\n"
    "print('|'.join(names))\n",
    encoding="utf-8",
)
proc = subprocess.run(
    [sys.executable, "-X", "utf8", str(PROBE_D)],
    capture_output=True, text=True,
    env={**CLEAN_ENV, "NEURONAV_CONFIG": str(cfg_prec), "NEURONAV_EMBED_FAKE": "1"},
    cwd=str(HERE),
)
check("296-B: precedence child completed", proc.returncode == 0, proc.stderr[-400:])
check(
    "296-B: .neuroignore beats a gitignore !negation (union is additive-only)",
    proc.stdout.strip() == "scripts/godot_side.py|src/app.py|tools/helper.py",
    proc.stdout.strip(),
)

# gitignore-sourced prunes stay loud: threshold lowered in the child (the
# crossing logic is the contract), .git junk stays baseline-silent, the
# second walk stays silent (once per process), FAKE stays silent
PROBE_C = SCRATCH3 / "probe_c.py"
PROBE_C.write_text(
    "import sys\n"
    f"sys.path.insert(0, {str(HERE)!r})\n"
    "import nav, navconfig, navstore, navindex\n"
    "navindex.GITIGNORE_PRUNE_WARN_N = 3\n"
    "list(navindex.iter_files())\n"
    "list(navindex.iter_files())\n",
    encoding="utf-8",
)


def _note_lines(proc):
    return [ln for ln in proc.stderr.splitlines() if "gitignore" in ln.lower()]


proc = subprocess.run(
    [sys.executable, "-X", "utf8", str(PROBE_C)],
    capture_output=True, text=True,
    env={**CLEAN_ENV, "NEURONAV_CONFIG": str(cfg_296)},
    cwd=str(T3),
)
check("296-C: note child completed", proc.returncode == 0, proc.stderr[-400:])
notes = _note_lines(proc)
check(
    "296-C: one-shot stderr note names gitignore-pruned dirs (genout; never baseline .git)",
    len(notes) == 1 and "genout" in notes[0] and "not indexed" in notes[0]
    and notes[0].count(".git") == 1,  # the ".gitignore" word alone — no .git dir named
    f"lines={len(notes)}",
)
proc = subprocess.run(
    [sys.executable, "-X", "utf8", str(PROBE_C)],
    capture_output=True, text=True,
    env={**CLEAN_ENV, "NEURONAV_CONFIG": str(cfg_296), "NEURONAV_EMBED_FAKE": "1"},
    cwd=str(HERE),
)
check(
    "296-C: hermetic FAKE run stays silent",
    proc.returncode == 0 and len(_note_lines(proc)) == 0,
    f"lines={len(_note_lines(proc))}",
)

check(
    "296-D: canonical prune set — WALK_DEFAULTS exclude == _PRUNE_FLOOR",
    set(navconfig.WALK_DEFAULTS["exclude_dirs"]) == set(navindex._PRUNE_FLOOR)
    and {"__pycache__", ".team_scratch", ".godot"} <= navindex._PRUNE_FLOOR,
    f"defaults={sorted(navconfig.WALK_DEFAULTS['exclude_dirs'])} "
    f"floor={sorted(navindex._PRUNE_FLOOR)}",
)

# ---- issue #375: one filter leaf, three walk engines -------------------------
# iter_files / iter_root_files / stat_fingerprint were kept equivalent only
# by comments (#296-C bit once). The per-directory prune (_kept_dirs) and the
# suffix predicate (_suffix_in) are the single truth now; these legs pin the
# identity on this ignore/prune-heavy tree AND the single truth itself — a
# deliberate filter change must propagate to all three engines at once.
(SRC / "keep" / "kept.py").write_text("def kept_py():\n    pass\n", encoding="utf-8")
(SRC / "vendored" / "dep.py").write_text("vendored_py = 1\n", encoding="utf-8")

EXPECT_375 = {"app.py", "keep/kept.py"}  # vendored/dep.py: config-pruned everywhere
ids_iter = {navindex.file_id(p) for p in navindex.iter_files()}
ids_root = {navindex.file_id(p) for p in navindex.iter_root_files(navconfig.EXTS)}
ids_fp = set(navindex.stat_fingerprint())
check(
    "375: same file set from all three engines (config prune honored)",
    ids_iter == ids_root == ids_fp == EXPECT_375,
    f"iter={sorted(ids_iter)} root={sorted(ids_root)} fp={sorted(ids_fp)}",
)

# the two os.walk engines share the suffix leaf; their only contract delta
# is iter_root_files' extra cache floor (cfg_a excludes just "vendored", so
# floor-dir content — the suite's own FAKE store under .neuronav, the junk
# .tres files — rides the all-suffixes walk and nothing else does)
ids_all = {navindex.file_id(p) for p in navindex.iter_files(all_suffixes=True)}
ids_root_all = {navindex.file_id(p) for p in navindex.iter_root_files({".py", ".tres"})}
_leak = {
    i for i in ids_all - ids_root_all
    if i.split("/", 1)[0] not in navindex._PRUNE_FLOOR
}
check(
    "375: root walk == all-suffixes walk minus exactly the cache floor",
    ids_root_all == {"app.py", "keep/kept.py", "keep/wiring.tres"}
    and not _leak and ids_all - ids_root_all,
    f"all={sorted(ids_all)} root={sorted(ids_root_all)} leak={sorted(_leak)}",
)

# single-truth sabotage: break the suffix leaf, every engine changes
_real_suffix_in = navindex._suffix_in
navindex._suffix_in = (
    lambda name, suffixes: _real_suffix_in(name, suffixes) and Path(name).suffix != ".py"
)
try:
    sab_iter = {navindex.file_id(p) for p in navindex.iter_files()}
    sab_root = {navindex.file_id(p) for p in navindex.iter_root_files({".py"})}
    sab_fp = set(navindex.stat_fingerprint())
finally:
    navindex._suffix_in = _real_suffix_in
check(
    "375: suffix-leaf sabotage propagates to all three engines",
    sab_iter == sab_root == sab_fp == set(),
    f"iter={sorted(sab_iter)} root={sorted(sab_root)} fp={sorted(sab_fp)}",
)

# single-truth sabotage: break the dir filter, every engine changes
_real_kept_dirs = navindex._kept_dirs
navindex._kept_dirs = (
    lambda names, prune: [dn for dn in _real_kept_dirs(names, prune) if dn != "keep"]
)
try:
    pr_iter = {navindex.file_id(p) for p in navindex.iter_files()}
    pr_root = {navindex.file_id(p) for p in navindex.iter_root_files(navconfig.EXTS)}
    pr_fp = set(navindex.stat_fingerprint())
    pr_tres = {navindex.file_id(p) for p in navindex.iter_root_files({".tres"})}
finally:
    navindex._kept_dirs = _real_kept_dirs
check(
    "375: dir-filter sabotage propagates to all three engines",
    pr_iter == pr_root == pr_fp == {"app.py"} and pr_tres == set(),
    f"iter={sorted(pr_iter)} root={sorted(pr_root)} fp={sorted(pr_fp)} "
    f"tres={sorted(pr_tres)}",
)

shutil.rmtree(SCRATCH3, ignore_errors=True)
shutil.rmtree(SCRATCH2, ignore_errors=True)

shutil.rmtree(SCRATCH, ignore_errors=True)
# byte pin: WALKGUARD OK/FAILS tail — kept local
print(("WALKGUARD OK" if not FAILS else f"WALKGUARDFAILS: {FAILS}"))
sys.exit(1 if FAILS else 0)
