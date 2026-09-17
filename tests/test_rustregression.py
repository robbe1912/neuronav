"""Rust real-repo regression suite (issue #284).

Mirrors test_tsregression. Hermetic by default: tests/fixtures/rustreg
is a crate-shaped fixture exercising the anubis daemon-rs analogues —
lib.rs pub-mod closure roots, a re-export barrel that is wiring-only,
a bin target that is its OWN crate root, and an integration test
wired through the crate name (name-level liveness by design; no
cross-crate static edges is a #284 non-goal, pinned as such).

NEURONAV_CONFIG=<profile> adds profile legs against a real rust repo
(the #284 audit target). Profile legs SKIP LOUDLY without the env
(issue #97 — a regression target is named, never assumed); canary
names and the .rs parse floor live only in the machine-local
gitignored profile, never in this file.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "rustreg"
PROFILE = os.environ.get("NEURONAV_CONFIG", "")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


CFG = Path(tempfile.gettempdir()) / "neuronav_rustreg_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "rustreg",
            "state_dir": str(Path(tempfile.gettempdir()) / "neuronav_rustreg_state"),
            "include_dirs": ["."],
            "extensions": [".rs"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

from bake import files_model  # noqa: E402  (needs NEURONAV_CONFIG set first)
from extractors import registry_for  # noqa: E402
from extractors.rust import RUST_EXTS  # noqa: E402
import graph  # noqa: E402
import nav  # noqa: E402


def digest(g) -> str:
    """Byte-stable fingerprint (mirrors test_tsregression)."""
    h = hashlib.sha256()
    for rel in sorted(g.files):
        fs = g.files[rel]
        h.update(f"F{rel}".encode())
        h.update(f"X{fs.class_name}|{fs.extends}".encode())
        for name in sorted(fs.funcs):
            fn = fs.funcs[name]
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
    for c in sorted(g.dead_code(10**9)["candidates"],
                    key=lambda d: (d["path"], d["func"], d["tier"])):
        h.update(f"D{c['path']}:{c['func']}:{c['tier']}".encode())
    return h.hexdigest()


def dead_flags(g):
    return files_model._dead_flags(
        g, graph.DEAD_TIER_WEIGHTS, graph.DEAD_SHARE_THRESHOLD)


# ---------------- hermetic leg (always on) ----------------

check("file_doc contract revision is the pinned one",
      graph.FILE_DOC_REV == 2, str(graph.FILE_DOC_REV))

g = graph.get_graph(rebuild=True)

walked = [p for p in nav.iter_files() if p.suffix in RUST_EXTS]
indexed = {rel for rel, fs in g.files.items() if fs.ext == ".rs"}
check("hermetic: every walked .rs indexed (parse coverage 1.0)",
      len(walked) == len(indexed) == 6, f"walked={len(walked)} indexed={len(indexed)}")

DEAD = {(c["path"], c["func"]): c["tier"] for c in g.dead_code(10**9)["candidates"]}


def tier(path: str, func: str):
    return DEAD.get((path, func))


# lib.rs pub-mod closure: pub fns of all-pub-mod-chain modules are roots
lib = g.files["src/lib.rs"]
check("lib barrel re-exports api::serve as launch",
      ("src/api.rs", "serve") in lib.from_imports, str(sorted(lib.from_imports)))
check("lib barrel is wiring-only (no funcs, no dead rows)",
      not lib.funcs and registry_for(".rs").is_wiring_only(lib)
      and not [c for c in DEAD if c[0] == "src/lib.rs"],
      f"funcs={sorted(lib.funcs)}")
for _r in ("src/api.rs::serve", "src/api.rs::auth_gate", "src/net.rs::send"):
    check(f"pub-mod closure roots {_r}", _r in g.roots, "")

# api.rs alias: use crate::net::send as deliver rebinds to the definer
af = g.files["src/api.rs"]
check("use crate::net::send as deliver rebinds to src/net.rs",
      ("src/net.rs", "send") in af.from_imports
      and getattr(af, "_rust_aliases", {}).get("deliver") == ("src/net.rs", "send"),
      f"{sorted(af.from_imports)}")

# dead tiers + dead-file share: courier/orphan_dead dead of 4 fns (0.5)
check("courier dead (likely)", tier("src/api.rs", "courier") == "likely", "")
check("orphan_dead dead (likely)", tier("src/api.rs", "orphan_dead") == "likely", "")
check("dead fn still contributes its parse-true edge (courier -> send)",
      "src/net.rs::send" in g.edges.get("src/api.rs::courier", set()),
      str(sorted(g.edges.get("src/api.rs::courier", set()))))

# bin target is its OWN crate root (#284)
check("bin main rooted", "src/bin/rt.rs::main" in g.roots, "")
_rt_edges = g.edges.get("src/bin/rt.rs::main", set())
check("bin main -> own module tree (crate:: anchor, not the lib's src/)",
      "src/bin/rt/helper.rs::shake" in _rt_edges
      and "src/bin/rt/helper.rs::steady" in _rt_edges,
      str(sorted(_rt_edges)))
check("bin use-crate alias binds inside the bin",
      ("src/bin/rt/helper.rs", "shake") in g.files["src/bin/rt.rs"].from_imports,
      str(sorted(g.files["src/bin/rt.rs"].from_imports)))
for _f in ("shake", "steady"):
    check(f"bin helper fn {_f} alive", ("src/bin/rt/helper.rs", _f) not in DEAD, "")

# integration test: entry-hinted roots, name-level wiring by design
for _r in ("tests/it.rs::sends_packet", "tests/it.rs::serves"):
    check(f"integration test rooted: {_r}", _r in g.roots, "")
check("no cross-crate static edges from tests/ (name-level by design, #284)",
      not any(g.edges.get(src) for src in g.edges if src.startswith("tests/it.rs::")),
      str([s for s in g.edges if s.startswith("tests/it.rs::")]))

# dead-file flags: api.rs crosses the share threshold; wiring stays out
dead_flag, dead_likely, _ = dead_flags(g)
check("api.rs flagged dead-file (share 0.5 >= threshold)",
      "src/api.rs" in dead_flag, str(sorted(dead_flag)))
check("wiring-only files excluded from dead flags",
      not [p for p in dead_flag
           if registry_for(".rs").is_wiring_only(g.files[p])],
      str(sorted(dead_flag)))

# file_doc: structural head for fn-bearing importers; raw for wiring
_doc = graph.file_doc(nav.ROOT / "src/api.rs", "src/api.rs",
                      nav._read_text(nav.ROOT / "src/api.rs"), nav.FILE_DOC_CAST)
check("api.rs file_doc carries the imports head",
      "\n# imports: src/net.rs" in _doc and len(_doc) <= nav.MAX_EMBED_CHARS,
      _doc[:120])
_libdoc = graph.file_doc(nav.ROOT / "src/lib.rs", "src/lib.rs",
                         nav._read_text(nav.ROOT / "src/lib.rs"), nav.FILE_DOC_CAST)
check("wiring-only lib.rs embeds as raw text (no funcs)",
      _libdoc.startswith("//! Hermetic rust regression crate"), _libdoc[:80])

# determinism: byte-stable double rebuild
g2 = graph.get_graph(rebuild=True)
check("hermetic: double-rebuild digest stable", digest(g) == digest(g2),
      f"{digest(g)[:12]} vs {digest(g2)[:12]}")

# ---------------- profile legs (real repo, issue #284) ----------------

prof = None
if PROFILE:
    prof = Path(PROFILE)
elif (HERE / "config.json").is_file():
    prof = HERE / "config.json"

if registry_for(".rs") is None:
    print("SKIP profile legs: .rs extractor not registered (vacuous without it)")
elif prof is None or not prof.is_file():
    print("SKIP profile legs: no NEURONAV_CONFIG profile and no config.json "
          "(issue #97 — the rust regression target is a named machine-local "
          "repo, never assumed; see tests/AGENTS.md)")
else:
    nav._apply_config(prof)
    cfg = json.loads(prof.read_text(encoding="utf-8"))
    rr = cfg.get("rust_regression", {})
    parse_floor = float(rr.get("parse_floor", 0.9))
    rs_floor = rr.get("rs_floor")
    if not rs_floor:
        print("SKIP profile legs: profile carries no rust_regression.rs_floor "
              "(issue #97 — the absolute .rs count is a machine-local fact)")
    else:
        g = graph.get_graph(rebuild=True)

        walked = [p for p in nav.iter_files() if p.suffix in RUST_EXTS]
        indexed = {rel for rel, fs in g.files.items() if fs.ext == ".rs"}
        cov = len(indexed) / max(len(walked), 1)
        check(f"profile: parse coverage >= {parse_floor}", cov >= parse_floor,
              f"cov={cov:.3f} walked={len(walked)} indexed={len(indexed)}")
        check(f"profile: indexed .rs >= rs_floor ({rs_floor})",
              len(indexed) >= int(rs_floor), str(len(indexed)))

        g2 = graph.get_graph(rebuild=True)
        check("profile: double-rebuild digest stable", digest(g) == digest(g2),
              f"{digest(g)[:12]} vs {digest(g2)[:12]}")

        cands = g.dead_code(10**9)["candidates"]
        canaries = cfg.get("regression_canaries", {})
        if not canaries:
            print("SKIP canary legs: profile carries no regression_canaries "
                  "(issue #97 — canary names are machine-local facts)")
        else:
            for pair in canaries.get("dead", []):
                ptok, ftok = pair[0], pair[1]
                check(f"profile canary dead: {ptok}::{ftok}",
                      any(ptok in c["path"] and ftok in c["func"] for c in cands),
                      "")
            # rust refinement of tsregression's file-level alive list: the
            # alive contract is fn-level — the exact (path, func) pair must
            # be absent from dead candidates
            for pair in canaries.get("alive", []):
                ptok, ftok = pair[0], pair[1]
                check(f"profile canary alive: {ptok}::{ftok}",
                      not any(ptok in c["path"] and ftok in c["func"] for c in cands),
                      "")

        dead_flag, _, _ = dead_flags(g)
        wiring = [p for p in sorted(g.files)
                  if g.files[p].ext == ".rs"
                  and registry_for(".rs").is_wiring_only(g.files[p])]
        check("profile: wiring-only files excluded from dead flags",
              not [p for p in dead_flag if p in wiring],
              str([p for p in dead_flag if p in wiring]))

        checked_docs = 0
        for rel, fs in sorted(g.files.items()):
            if fs.ext != ".rs" or not fs.funcs:
                continue
            if not (fs.imported_modules or fs.from_imports):
                continue
            doc = graph.file_doc(nav.ROOT / rel, rel,
                                 nav._read_text(nav.ROOT / rel), nav.FILE_DOC_CAST)
            if not ("\n# imports: " in doc and len(doc) <= nav.MAX_EMBED_CHARS):
                check(f"profile: file_doc imports head for {rel}", False, doc[:100])
                break
            checked_docs += 1
        else:
            check("profile: .rs file_doc imports heads under budget "
                  f"({checked_docs} files)", checked_docs > 0, str(checked_docs))

print()
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
