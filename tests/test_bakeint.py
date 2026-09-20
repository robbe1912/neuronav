# bake integrity QA (issues #64/#108) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_bakeint.py
#
# Hermetic: scratch corpus + FAKE store under the system temp dir (no
# self-index pollution, no model server — the guard counts vectors, it
# never embeds). The real-provider refusal legs run as child processes
# with NEURONAV_EMBED_FAKE scrubbed from the environment, because this
# harness itself legitimately bakes under FAKE for its healthy legs.
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]



from harness import check, finish

# ---- scratch corpus + config (the vizcorpus_build shape, miniature) --------
# issue #321 lever 3: mkdtemp per run — the fixed name collided across
# concurrent same-suite processes; end-of-suite rmtree still cleans up
SCRATCH = Path(tempfile.mkdtemp(prefix="neuronav_bakeint_"))
SRC = SCRATCH / "src"
SRC.mkdir(parents=True)
for i in range(6):
    (SRC / f"node_{i}.gd").write_text(
        f"extends Node\n# module {i}\nfunc work_{i}() -> int:\n\treturn {i}\n",
        encoding="utf-8",
    )
(SRC / "main.tscn").write_text('[gd_scene]\n[node name="Main" type="Node2D"]\n', encoding="utf-8")

cfg = SCRATCH / "config.json"
cfg.write_text(
    json.dumps(
        {
            "root": str(SRC),
            "collection": "bakeint_fix",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".gd", ".tscn"],
            "exclude_dirs": [],
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(cfg)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import graph  # noqa: E402,F401  (binds the scratch config via NEURONAV_CONFIG)
import navconfig, navindex, navstore
import viz  # noqa: E402


def run_child(fake: bool = False, cfg_path: Path | None = None) -> subprocess.CompletedProcess:
    """Child bound to the same config; fake=False is the real-provider
    stance (FAKE waiver scrubbed from the environment)."""
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_EMBED_FAKE"}
    if fake:
        env["NEURONAV_EMBED_FAKE"] = "1"
    if cfg_path is not None:
        env["NEURONAV_CONFIG"] = str(cfg_path)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import viz\nprint(viz.generate())"],
        cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def wipe(keep: int) -> None:
    """Simulate the #91/#159 store wipe: keep only `keep` vectors."""
    col = navstore._collection()
    ids = col.get(limit=100000)["ids"]
    assert len(ids) >= keep, f"wipe(keep={keep}) but store holds {len(ids)}"
    if keep < len(ids):
        col.delete(ids=ids[keep:])


def refusal(fn, label, needles):
    try:
        fn()
        ok, msg = False, "(no refusal — it baked)"
    except RuntimeError as e:
        ok, msg = True, str(e)
        for n in needles:
            if n not in msg:
                ok = False
    check(label, ok, msg[:100].replace("\n", " "))


def no_constants(x):
    raise AssertionError(f"strict JSON violated: {x}")

try:  # callback sanity at column 0 — parse_constant refs are not call sites (#96 class)
    no_constants("NaN")
    check("strict-JSON callback raises on constants", False, "no raise")
except AssertionError:
    check("strict-JSON callback raises on constants", True, "")


# ---- 1. healthy store: bakes, strict JSON, importmap spliced ----------------
navindex.rescan()
walk_n = sum(1 for _ in navindex.iter_files())
check("scratch corpus indexed", navstore.count() == walk_n and walk_n == 7,
      f"count={navstore.count()} walk={walk_n}")

p1 = viz.generate()
html1 = p1.read_text(encoding="utf-8")
check("healthy bake writes graph.html", p1.is_file() and p1 == navconfig.STATE_DIR / "graph.html", str(p1))
m = re.search(r"const DATA = (.+);\n", html1)
try:
    data1 = json.loads(m.group(1), parse_constant=no_constants) if m else None
except AssertionError:
    data1 = None
check("DATA splices as strict JSON (no NaN/Infinity)", bool(m) and isinstance(data1, dict),
      "" if m else "DATA marker not found")
check("importmap spliced in", "data:text/javascript;base64" in html1
      and "__DATA__" not in html1 and "__IMPORTMAP__" not in html1, "")

# ---- 2. byte stability: two builds equal modulo the freshness stamp --------
p2 = viz.generate()
html2 = p2.read_text(encoding="utf-8")
norm = lambda h: re.sub(r'"generated_at":"[^"]*"', '"generated_at":""', h)  # noqa: E731
check("two builds byte-equal modulo generated_at", norm(html1) == norm(html2), "")
check("atomic write leaves no .tmp", not (navconfig.STATE_DIR / "graph.html.tmp").exists(), "")

# ---- 3. atomicity: crash mid-write keeps the previous bake whole -----------
target = SCRATCH / "atomic.html"
target.write_text("PREVIOUS BAKE", encoding="utf-8")
real_bd, real_replace = viz._build_data, os.replace
viz._build_data = lambda: {"nodes": [], "meta": {}}
os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash mid-bake"))
try:
    try:
        viz.generate(out=target)
        blew = False
    except OSError:
        blew = True
finally:
    viz._build_data, os.replace = real_bd, real_replace
check("crash mid-write raises", blew, "")
check("previous bake intact after crash", target.read_text(encoding="utf-8") == "PREVIOUS BAKE", "")
check("temp reaped after crash", not (SCRATCH / "atomic.html.tmp").exists(), "")

# ---- 4. #108 serializer boundary: breakout / token / NaN --------------------
refusal(lambda: viz._strict_json({"pos": [float("nan")]}, "DATA"),
        "NaN payload refused with path", ["DATA.pos[0]", "issue #108"])
refusal(lambda: viz._strict_json({"w": float("inf")}, "DATA"),
        "Infinity payload refused", ["DATA.w", "issue #108"])
refusal(lambda: viz._strict_json({"deep": [{"x": float("nan")}]}, "DATA"),
        "nested NaN refused", ["DATA.deep[0].x"])
refusal(lambda: viz._splice_safe(json.dumps({"name": "</script>alert(1)"}), "DATA"),
        "script breakout refused", ["</script", "issue #108"])
refusal(lambda: viz._splice_safe(json.dumps({"name": "</ScRiPt x"}), "DATA"),
        "script breakout case-insensitive", ["</script"])
refusal(lambda: viz._splice_safe(json.dumps({"doc": "x __IMPORTMAP__ y"}), "DATA"),
        "importmap token injection refused", ["__IMPORTMAP__", "issue #108"])
refusal(lambda: viz._splice_safe(json.dumps({"doc": "__DATA__"}), "importmap"),
        "data token injection refused", ["__DATA__"])
check("clean payload passes through",
      viz._splice_safe(viz._strict_json({"a": 1.5, "b": [None, True]}, "DATA"), "DATA")
      == '{"a":1.5,"b":[null,true]}', "")

# ---- 5. crafted payload through generate() (the wiring, not the helpers) ----
crafted = SCRATCH / "crafted.html"
crafted.unlink(missing_ok=True)
try:
    viz._build_data = lambda: {"nodes": [{"path": "e", "name": "</script>x"}]}
    refusal(lambda: viz.generate(out=crafted),
            "generate refuses </script> payload", ["</script"])
    viz._build_data = lambda: {"n": "__IMPORTMAP__"}
    refusal(lambda: viz.generate(out=crafted),
            "generate refuses token payload", ["__IMPORTMAP__"])
    viz._build_data = lambda: {"pos": [float("nan")]}
    refusal(lambda: viz.generate(out=crafted),
            "generate refuses NaN payload", ["issue #108"])
finally:
    viz._build_data = real_bd
check("crafted graph.html never written", not crafted.exists(), "")

# ---- 6. #64 zeroed store: refuses even under FAKE (wiped = never deliberate)
wipe(keep=0)
check("store zeroed for the refusal leg", navstore.count() == 0, str(navstore.count()))
# #202: re-capture so html1 IS the pre-refusal file text — section 2's p2
# rebuild re-stamped graph.html, and pinning the first build's bytes would
# flake on a second-boundary crossing. The exact byte compare below
# (generated_at stamp included) is the point: norm() would mask a
# refusal-path rewrite with a fresh stamp, the atomic-write law's failure
# mode. norm() stays on the two-builds check, where stamps may differ.
html1 = p1.read_text(encoding="utf-8")
refusal(lambda: viz.generate(), "zeroed store refused (even under FAKE)",
        ["0 vectors", f"{walk_n} files", "rescan", "bakeint_fix", str(navconfig.DB_DIR)])
check("refusal leaves the old bake untouched", p1.read_text(encoding="utf-8") == html1, "")
navindex.rescan()

# ---- 7. #64 partial store: FAKE waiver bakes in-process ---------------------
wipe(keep=2)
check("store partially wiped", navstore.count() == 2 and 2 * 2 < walk_n, str(navstore.count()))
try:
    viz.generate()
    waived = True
except RuntimeError:
    waived = False
check("FAKE hermetic rig still bakes a partial store", waived, "the #64 waiver")
navindex.rescan()

# ---- 8. real-provider stance (child, FAKE scrubbed): always a refusal -------
wipe(keep=0)
r = run_child()
check("real provider + zeroed store: child refuses",
      r.returncode != 0 and "refusing to bake" in r.stderr and "0 vectors" in r.stderr
      and "rescan" in r.stderr, (r.stdout + r.stderr).strip()[:120].replace("\n", " "))
navindex.rescan()
wipe(keep=2)
r = run_child()
check("real provider + partial store: child refuses",
      r.returncode != 0 and "near-empty" in r.stderr and "<50%" in r.stderr,
      (r.stdout + r.stderr).strip()[:120].replace("\n", " "))
navindex.rescan()

# ---- 9. the waiver is FAKE-only: child WITH FAKE prints the waiver note -----
wipe(keep=2)
r = run_child(fake=True)
check("FAKE child bakes partial store with waiver note",
      r.returncode == 0 and "waiver" in r.stderr, (r.stdout + r.stderr).strip()[:120].replace("\n", " "))
navindex.rescan()

# ---- 10. zero-walk config: nothing to bake ----------------------------------
empty_root = SCRATCH / "empty"
empty_root.mkdir()
empty_cfg = SCRATCH / "empty_config.json"
empty_cfg.write_text(
    json.dumps({"root": str(empty_root), "collection": "bakeint_fix", "state_dir": "default",
                "include_dirs": ["."], "extensions": [".gd", ".tscn"], "exclude_dirs": []}),
    encoding="utf-8",
)
r = run_child(cfg_path=empty_cfg)
check("zero-file walk refused",
      r.returncode != 0 and "found 0 files" in r.stderr, r.stderr.strip()[:120].replace("\n", " "))


# ---- 11. #299 C: the store-index space derives exactly once ------------------
# The walk-order × store-index mapping was rebuilt independently at three
# consumers (fetch/knn/semAff, plus the cluster matrix) — drift there wires
# kNN pairs and semAff ink to the wrong nodes with no loud failure (#64's
# silent-degradation class). #299 C derives it once (bake.embeddings
# _store_paths) and threads it through the emb triple; these legs pin it.
bake_src = "".join(
    p.read_text(encoding="utf-8")
    for p in sorted((HERE / "bake").glob("*.py")) + [HERE / "viz.py"]
)
restatements = re.findall(r"for p in paths if p in emb_idx", bake_src)
check("#299 store-index derivation restated exactly once (bake/embeddings)",
      len(restatements) == 1, f"{len(restatements)} occurrences in bake/ + viz.py")

try:
    from bake.embeddings import _knn_sims, _store_paths
    from bake.semantics import _sem_aff

    # walk order deliberately differs from store order; c.py unindexed
    paths = ["a.py", "b.py", "c.py", "d.py"]
    emb_idx = {"b.py": 0, "a.py": 1, "d.py": 2}
    emb_paths = _store_paths(paths, emb_idx)
    check("#299 _store_paths keeps walk order, filtered",
          emb_paths == ["a.py", "b.py", "d.py"], str(emb_paths))

    # emb rows in emb_paths order: a~d identical (the kNN pair), b orthogonal
    import numpy as np

    v = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    emb = (emb_idx, v, emb_paths)
    sims, _ = _knn_sims(emb)
    check("#299 kNN indices are emb_paths-local (a,d pair)",
          sims == [[0, 2, 1.0]], str(sims))

    idx = {"a.py": 10, "b.py": 3, "d.py": 7}   # node indices, unrelated order
    rows, dropped = _sem_aff(emb, sims, idx)
    check("#299 semAff maps through the shared space to NODE indices",
          rows == [[10, 7, 1.0]] and dropped == 0,
          f"rows={rows} dropped={dropped}")
except ImportError as e:
    check("#299 bake.embeddings/bake.semantics split exists", False, str(e))

# ---- 12. #367 loud-failures: in-flight stage errors abort the bake --------
# The pre-#367 broad excepts in bake/embeddings + bake/semantics swallowed
# real failures (chroma internal error, numpy/scipy, schema drift) into
# silent Nones/empties — a thinner graph with no marker. The #64 guard only
# covers the empty-STORE shape at entry; exceptions raised inside the
# fetch/kNN/matrix/supergroups stages must fail loud, naming their job. The
# only sanctioned degrades left: missing store (count()==0 -> None) and
# scipy unavailability in _supergroups (ImportError, marked on stderr).
import io

import numpy as np
from bake.embeddings import _knn_sims
from bake.semantics import _cluster_matrix, _supergroups

# a) fetch: a chroma read that raises mid-bake must abort generate(), not
#    ship a silently semantic-less graph (what-tagged so only the bake's
#    own fetch leg booms; recall/clusters reads stay real)
real_all = navstore.col_get_all


def boom(col, include, what="chunked read"):
    if what == "bake embeddings":
        raise RuntimeError("synthetic chroma failure")
    return real_all(col, include, what)


navstore.col_get_all = boom
try:
    refusal(lambda: viz.generate(), "chroma fetch error aborts the bake",
            ["embeddings fetch", "synthetic chroma failure"])
finally:
    navstore.col_get_all = real_all

# b/c) kNN + cluster matrix: poisoned emb (paths outnumber embedding rows)
# -> IndexError wrapped, job named — never silent empties
bad = ({"a.py": 0}, np.array([[1.0, 0.0]], dtype=np.float32),
       ["a.py", "b.py"])
refusal(lambda: _knn_sims(bad), "kNN stage error is loud and job-named",
        ["semantic kNN"])
refusal(lambda: _cluster_matrix(
            [{"path": "a.py", "cluster": 0}, {"path": "b.py", "cluster": 1}],
            bad),
        "cluster-matrix stage error is loud and job-named", ["cluster matrix"])

# d/e) supergroups: real failure loud; ImportError = the sanctioned scipy
# degrade — empties AND an explicit stderr marker, never silent. Two fine
# clusters: under 2 the coarse level is vacuous and legitimately returns
# empties without ever reaching coarse_groups.
import clusters as _cl

two = [{"id": 0, "paths": [("a.py", 0)]}, {"id": 1, "paths": [("b.py", 0)]}]
real_cg = _cl.coarse_groups
_cl.coarse_groups = lambda *a, **k: (_ for _ in ()).throw(
    ValueError("synthetic linkage failure"))
try:
    refusal(lambda: _supergroups(two, bad),
            "supergroups stage error is loud and job-named", ["supergroups"])
    _cl.coarse_groups = lambda *a, **k: (_ for _ in ()).throw(
        ImportError("No module named 'scipy'"))
    buf, real_err = io.StringIO(), sys.stderr
    sys.stderr = buf
    try:
        degraded = _supergroups(two, bad)
    finally:
        sys.stderr = real_err
    check("scipy unavailability degrades to marked empties",
          degraded == ({}, []) and "supergroups degraded" in buf.getvalue(),
          f"r={degraded} stderr={buf.getvalue()[:80]!r}")
finally:
    _cl.coarse_groups = real_cg

# ---- cleanup -----------------------------------------------------------------
shutil.rmtree(SCRATCH, ignore_errors=True)
finish()
