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

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---- scratch corpus + config (the vizcorpus_build shape, miniature) --------
SCRATCH = Path(tempfile.gettempdir()) / "neuronav_bakeint_scratch"
shutil.rmtree(SCRATCH, ignore_errors=True)
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
import nav  # noqa: E402
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
    col = nav._collection()
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
nav.rescan()
walk_n = sum(1 for _ in nav.iter_files())
check("scratch corpus indexed", nav.count() == walk_n and walk_n == 7,
      f"count={nav.count()} walk={walk_n}")

p1 = viz.generate()
html1 = p1.read_text(encoding="utf-8")
check("healthy bake writes graph.html", p1.is_file() and p1 == nav.STATE_DIR / "graph.html", str(p1))
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
check("atomic write leaves no .tmp", not (nav.STATE_DIR / "graph.html.tmp").exists(), "")

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
check("store zeroed for the refusal leg", nav.count() == 0, str(nav.count()))
refusal(lambda: viz.generate(), "zeroed store refused (even under FAKE)",
        ["0 vectors", f"{walk_n} files", "rescan", "bakeint_fix", str(nav.DB_DIR)])
check("refusal leaves the old bake untouched", p1.read_text(encoding="utf-8") == html1, "")
nav.rescan()

# ---- 7. #64 partial store: FAKE waiver bakes in-process ---------------------
wipe(keep=2)
check("store partially wiped", nav.count() == 2 and 2 * 2 < walk_n, str(nav.count()))
try:
    viz.generate()
    waived = True
except RuntimeError:
    waived = False
check("FAKE hermetic rig still bakes a partial store", waived, "the #64 waiver")
nav.rescan()

# ---- 8. real-provider stance (child, FAKE scrubbed): always a refusal -------
wipe(keep=0)
r = run_child()
check("real provider + zeroed store: child refuses",
      r.returncode != 0 and "refusing to bake" in r.stderr and "0 vectors" in r.stderr
      and "rescan" in r.stderr, (r.stdout + r.stderr).strip()[:120].replace("\n", " "))
nav.rescan()
wipe(keep=2)
r = run_child()
check("real provider + partial store: child refuses",
      r.returncode != 0 and "near-empty" in r.stderr and "<50%" in r.stderr,
      (r.stdout + r.stderr).strip()[:120].replace("\n", " "))
nav.rescan()

# ---- 9. the waiver is FAKE-only: child WITH FAKE prints the waiver note -----
wipe(keep=2)
r = run_child(fake=True)
check("FAKE child bakes partial store with waiver note",
      r.returncode == 0 and "waiver" in r.stderr, (r.stdout + r.stderr).strip()[:120].replace("\n", " "))
nav.rescan()

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

# ---- cleanup -----------------------------------------------------------------
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
