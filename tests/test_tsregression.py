# TS-target regression (config-agnostic): byte-stability + liveness
# canaries + parse-coverage floors over whatever TS target the profile
# points at, plus the hermetic bake dead-flag pin (judge C1: the dead-share
# denominator resolves through the registry, so a .ts fixture flags exactly
# like the .gd control).
#   .venv/Scripts/python.exe -X utf8 tests/test_tsregression.py              (hermetic)
#   NEURONAV_CONFIG=<profile> .venv/Scripts/python.exe -X utf8 tests/test_tsregression.py
# Profile legs skip LOUDLY without a profile (issue #97: a regression
# target is named, never assumed). Canary names live only in the
# machine-local, gitignored profile — never in this file.
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROFILE = os.environ.get("NEURONAV_CONFIG", "")  # captured before the fixture config takes it

FIX = Path(__file__).resolve().parent / "fixtures" / "tsreg"
CFG = Path(tempfile.gettempdir()) / "neuronav_tsreg_config.json"
CFG.write_text(json.dumps({
    "root": FIX.as_posix(),
    "collection": "tsreg",
    "state_dir": str(Path(tempfile.gettempdir()) / "neuronav_tsreg_state"),
    "include_dirs": ["."],
    "extensions": [".ts", ".tsx", ".gd", ".py"],
    "exclude_dirs": [],
}), encoding="utf-8")
os.environ["NEURONAV_CONFIG"] = str(CFG)

from bake import files_model  # noqa: E402
from extractors import registry_for  # noqa: E402
import graph  # noqa: E402
import navconfig, navindex, navstore


from harness import check, finish

def digest(g) -> str:
    """Byte-stability digest over the walked-graph surface (cpphard law)."""
    h = hashlib.sha256()
    for rel in sorted(g.files):
        fs = g.files[rel]
        h.update(f"F{rel}".encode())
        h.update(f"X{fs.class_name}|{fs.extends}".encode())
        for name, fn in sorted(fs.funcs.items()):
            h.update(f"N{name}@{fn.line}:{len(fn.body)}:{fn.ret}:{fn.params}".encode())
        for s in sorted(fs.signals):
            h.update(f"S{s}".encode())
        for m in sorted(fs.members):
            h.update(f"M{m}={fs.members[m]}".encode())
        for c in sorted(fs.consts):
            h.update(f"C{c}={fs.consts[c]}".encode())
        for i in sorted(fs.imported_modules):
            h.update(f"I{i}".encode())
    for src in sorted(g.edges):
        for dst in sorted(g.edges[src]):
            h.update(f"E{src}>{dst}".encode())
    for r in sorted(g.roots):
        h.update(f"R{r}".encode())
    for k in sorted(g.class_map):
        h.update(f"K{k}={g.class_map[k]}".encode())
    dead = g.dead_code(10**9)
    for c in sorted(dead["candidates"], key=lambda d: (d["path"], d["func"], d["tier"])):
        h.update(f"D{c['path']}:{c['func']}:{c['tier']}".encode())
    return h.hexdigest()


# ---- hermetic section (no profile needed) ------------------------------
check("file_doc shaper revision is the pinned one (#295: language-owned intros —\n`//`-block/ts doc shapes changed, so doc shapes re-embed loudly per #220)",
      graph.FILE_DOC_REV == 3, f"rev={graph.FILE_DOC_REV}")

g = graph.get_graph(rebuild=True)
d1 = digest(g)
check("fixture graph byte-deterministic (double rebuild)",
      d1 == digest(graph.get_graph(rebuild=True)))

dead_flag, dead_likely, _ = files_model._dead_flags(
    g, graph.DEAD_TIER_WEIGHTS, graph.DEAD_SHARE_THRESHOLD)
check("judge C1: .gd control still flags dead-file",
      "deadshare.gd" in dead_flag, f"flagged={sorted(dead_flag)}")
check("judge C1: .ts with dead share >= threshold flags dead-file",
      "deadshare.ts" in g.files and "deadshare.ts" in dead_flag,
      f"in-graph={'deadshare.ts' in g.files} flagged={sorted(dead_flag)}")

amb = "ambient_dead.d.ts"
check("callable-bearing .d.ts is logic, not wiring",
      amb in g.files and g.files[amb].funcs
      and not registry_for(".ts").is_wiring_only(g.files[amb]),
      f"in-graph={amb in g.files}"
      f" funcs={sorted(g.files[amb].funcs) if amb in g.files else []}")
check("judge C1: ambient .d.ts dead share flags dead-file",
      amb in g.files and amb in dead_flag, f"flagged={sorted(dead_flag)}")
typ = "ambient_types.d.ts"
check("zero-callable .d.ts stays wiring-only and out of the dead tier",
      typ in g.files
      and registry_for(".ts").is_wiring_only(g.files[typ])
      and typ not in dead_flag,
      f"in-graph={typ in g.files}"
      f" funcs={len(g.files[typ].funcs) if typ in g.files else '-'}")

py_rel = "py_importer.py"
doc = graph.file_doc(navconfig.ROOT / py_rel, py_rel,
                     navindex._read_text(navconfig.ROOT / py_rel), navconfig.FILE_DOC_CAST)
imp_lines = [ln for ln in doc.splitlines() if ln.startswith("# imports:")]
check(".py doc head carries the resolved imports line",
      bool(imp_lines) and any("py_helper" in ln for ln in imp_lines),
      repr(imp_lines[:1]))
check("shaped .py doc stays under the embed budget",
      len(doc) <= navstore.MAX_EMBED_CHARS, f"{len(doc)} <= {navstore.MAX_EMBED_CHARS}")

ts_rel = "caller.ts"
if ts_rel in g.files:
    doc = graph.file_doc(navconfig.ROOT / ts_rel, ts_rel,
                         navindex._read_text(navconfig.ROOT / ts_rel), navconfig.FILE_DOC_CAST)
    imp_lines = [ln for ln in doc.splitlines() if ln.startswith("# imports:")]
    check(".ts doc head carries the resolved imports line",
          any("deadshare" in ln for ln in imp_lines), repr(imp_lines[:1]))
    check("shaped .ts doc stays under the embed budget",
          len(doc) <= navstore.MAX_EMBED_CHARS, f"{len(doc)} <= {navstore.MAX_EMBED_CHARS}")
else:
    check(".ts fixture parsed by the registry (extractors/ts.py)", False,
          "caller.ts absent from the graph — TS extractor not registered")

# ---- profile legs (skip LOUDLY without a profile — issue #97) ---------
print()
ts_mod = registry_for(".ts")
prof = None
if PROFILE:
    prof = Path(PROFILE)
else:
    _default = Path(__file__).resolve().parents[1] / "config.json"
    prof = _default if _default.is_file() else None

if ts_mod is None:
    print("SKIP profile legs — no '.ts' extractor registered "
          "(extractors/ts.py absent; lands with the TS extractor branch)")
elif prof is None:
    print("SKIP profile legs — no NEURONAV_CONFIG profile and no default "
          "config.json (issue #97: a regression target is named, never assumed)")
else:
    navconfig._apply_config(prof)
    cfg = json.loads(prof.read_text(encoding="utf-8"))
    parse_floor = float(cfg.get("ts_regression", {}).get("parse_floor", 0.9))
    ts_exts = getattr(ts_mod, "TS_EXTS")
    g = graph.get_graph(rebuild=True)
    walked = [p for p in navindex.iter_files() if p.suffix in ts_exts]
    indexed = {rel for rel, fs in g.files.items() if fs.ext in ts_exts}
    check("profile walks a TS surface", len(walked) > 0,
          f"{len(walked)} walked, extensions={navconfig.EXTS}")
    cov = len(indexed) / len(walked) if walked else 0.0
    check("profile parse coverage >= floor", cov >= parse_floor,
          f"{len(indexed)}/{len(walked)} = {cov:.4f} >= {parse_floor}")
    d1 = digest(g)
    check("profile graph byte-deterministic (double rebuild)",
          d1 == digest(graph.get_graph(rebuild=True)))

    cands = g.dead_code(10**9)["candidates"]
    cans: dict = cfg.get("regression_canaries", {})
    if not (cans.get("dead") or cans.get("alive")):
        print("SKIP canaries — profile carries no regression_canaries "
              "(issue #97: floors are named in the profile, not assumed)")
    for ptok, ftok in cans.get("dead", []):
        check(f"canary {ftok} dead",
              any(ptok in c["path"] and ftok in c["func"] for c in cands))
    for tok in cans.get("alive", []):
        check(f"canary {tok} alive", not any(tok in c["path"] for c in cands))

    dead_flag, _, _ = files_model._dead_flags(
        g, graph.DEAD_TIER_WEIGHTS, graph.DEAD_SHARE_THRESHOLD)
    wiring = [p for p in sorted(dead_flag)
              if registry_for(g.files[p].ext).is_wiring_only(g.files[p])]
    check("no wiring-only file in the dead-file tier",
          wiring == [], str(wiring[:5]))

    bad_hdr: list[str] = []
    bad_cap: list[str] = []
    n_docs = 0
    for rel, fs in sorted(g.files.items()):
        if fs.ext not in ts_exts or not fs.funcs or not (fs.imported_modules or fs.from_imports):
            continue
        n_docs += 1
        doc = graph.file_doc(navconfig.ROOT / rel, rel,
                             navindex._read_text(navconfig.ROOT / rel), navconfig.FILE_DOC_CAST)
        if "\n# imports: " not in doc:
            bad_hdr.append(rel)
        if len(doc) > navstore.MAX_EMBED_CHARS:
            bad_cap.append(rel)
    check(".ts docs carry the imports header", n_docs > 0 and not bad_hdr,
          f"{n_docs} docs, missing on {len(bad_hdr)}: {bad_hdr[:3]}")
    check(".ts docs stay under the embed budget", not bad_cap, str(bad_cap[:5]))

finish()
