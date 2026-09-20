# C extractor hardening suite (issue #346) — run in its own process:
#   NEURONAV_EMBED_FAKE=1 .venv/Scripts/python.exe -X utf8 tests/test_chard.py
#
# Hermetic: builds a graph over tests/fixtures/c (entry program + paired
# header/impl + definition header + dead material + TU-scope callback
# table) with a generated temp config + FAKE embeds. Pins:
#   f1  parse surface: free fns (static/extern/fn-ptr params), struct
#       members, enumerators, typedefs, TU globals (array tables)
#   f2  includes: quoted -> imported_modules; angle-bracket system
#       includes skipped entirely (issue #346 edit 1)
#   f3  entry rules: main() roots; nothing else mints roots
#   f4  call edges: file-local, definition-header (static inline),
#       pure-declaration-header -> paired .c impl (stem pairing)
#   f5  callback liveness: TU-scope table entry (bare fn name) keeps the
#       handler alive via referenced_names; &fn argument refs ditto
#   f6  dead tiers: uncalled = likely (C has no dynamic surface);
#       no bogus attribution to the fn preceding a TU table
#   f7  determinism: double build -> identical edges + dead candidates
#   f8  end-to-end: walk -> parse -> FAKE index (6 files), registry
#       ownership (.c -> c, .h stays cpp)
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

TMP = Path(tempfile.mkdtemp(prefix="neuronav_chard_"))
(TMP / "config.json").write_text(json.dumps({
    "root": str(HERE / "tests" / "fixtures" / "c"),
    "collection": "chard",
    "include_dirs": ["."],
    "extensions": [".c", ".h"],
    "exclude_dirs": [],
    "state_dir": str(TMP / "state"),
}), encoding="utf-8")
os.environ["NEURONAV_CONFIG"] = str(TMP / "config.json")
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

import graph  # noqa: E402  (binds the fixture config via NEURONAV_CONFIG)
import navindex
from extractors import c as C  # noqa: E402
from extractors import cpp as CPP  # noqa: E402
from extractors import registry_for  # noqa: E402

from harness import check, finish


MAIN = "src/main.c"
WIDGET_H = "src/widget.h"
WIDGET_C = "src/widget.c"
UTIL_H = "src/util.h"
DEAD_C = "src/dead.c"
TABLE_C = "src/table.c"

# ---- f1 parse surface ----------------------------------------------------------

fs = C.parse(HERE / "tests/fixtures/c/src/main.c", MAIN)
check("f1: main.c funcs {main, helper, on_tick}",
      sorted(fs.funcs) == ["helper", "main", "on_tick"], str(sorted(fs.funcs)))
main_fn = fs.funcs["main"]
check("f1: main() params captured by name",
      [p[0] for p in main_fn.params] == ["argc", "argv"], str(main_fn.params))
check("f1: main() ret int", main_fn.ret == "int", main_fn.ret)

fs_w = C.parse(HERE / "tests/fixtures/c/src/widget.c", WIDGET_C)
check("f1: widget.c funcs incl. static",
      sorted(fs_w.funcs) == ["register_tick", "set_active", "widget_draw", "widget_init"],
      str(sorted(fs_w.funcs)))
check("f1: struct members x/y/state",
      {k: v for k, v in fs_w.members.items() if k in ("x", "y", "state")}
      == {"x": "int", "y": "int", "state": "int"},
      str(fs_w.members))

fs_h = CPP.parse(HERE / "tests/fixtures/c/src/widget.h", WIDGET_H)
check("f1: pure-declaration header funcs empty", not fs_h.funcs, str(sorted(fs_h.funcs)))
check("f1: enumerators W_IDLE/W_ACTIVE",
      {"W_IDLE", "W_ACTIVE"} <= set(fs_h.consts), str(sorted(fs_h.consts)))
check("f1: typedef alias widget_t", fs_h.aliases.get("widget_t", "") != "",
      str(fs_h.aliases))

fs_t = C.parse(HERE / "tests/fixtures/c/src/table.c", TABLE_C)
check("f1: TU array table global TABLE", "TABLE" in fs_t.globals, str(fs_t.globals))
check("f1: table.c funcs", sorted(fs_t.funcs) == ["handler_a", "second_handler", "table_run"],
      str(sorted(fs_t.funcs)))

# ---- f2 includes ----------------------------------------------------------------

check("f2: main.c quoted includes {util.h, widget.h}",
      sorted(fs.imported_modules) == ["util.h", "widget.h"],
      str(sorted(fs.imported_modules)))
check("f2: angle-bracket <stdio.h> skipped entirely",
      "stdio.h" not in fs.imported_modules and not any(
          m.startswith("<") for m in fs.imported_modules))

# ---- f3/f4/f5/f6 graph level ----------------------------------------------------

g = graph.Graph().build()


check("f3: roots = main() + the referenced-name liveness root (handler_a)",
      sorted(g.roots) == [f"{MAIN}::main", f"{TABLE_C}::handler_a"], str(sorted(g.roots)))
edges = {f"{s} -> {t}" for s, tgts in g.edges.items() for t in tgts}
expect_edges = {
    f"{MAIN}::main -> {MAIN}::helper",
    f"{MAIN}::main -> {MAIN}::on_tick",
    f"{MAIN}::main -> {UTIL_H}::util_sum",
    f"{MAIN}::main -> {WIDGET_C}::register_tick",
    f"{MAIN}::main -> {WIDGET_C}::widget_draw",
    f"{MAIN}::main -> {WIDGET_C}::widget_init",
}
check("f4: call edges exact (local, definition-header, decl-header->impl)",
      edges == expect_edges, str(sorted(edges ^ expect_edges)))

check("f5: TU table handler alive via referenced_names",
      "handler_a" in g.referenced_names, str(sorted(g.referenced_names)))
check("f5: handler_a reachable (no containing fn, no edge)",
      f"{TABLE_C}::handler_a" in g.reachable)

# include-liveness (GK #348 review pin): a quoted include resolving to
# a repo .h keeps the header's DEFINED surface referenced even when
# never called — util_unused is included-but-never-called and stays
# alive; the raw include string resolves via the same-dir idiom
check("f5: include-liveness keeps the never-called header fn referenced",
      f"{UTIL_H}::util_unused" in g.referenced,
      str(sorted(k for k in g.referenced if k.startswith("src/util"))) or "none")
check("f5: include-liveness keeps util_unused reachable",
      f"{UTIL_H}::util_unused" in g.reachable)

dead = {(d["path"], d["func"], d["tier"]) for d in g.dead_code(100)["candidates"]}
expect_dead = {
    (DEAD_C, "never_called", "likely"),
    (DEAD_C, "also_dead", "likely"),
    (TABLE_C, "second_handler", "likely"),
    (TABLE_C, "table_run", "likely"),
    (WIDGET_C, "set_active", "likely"),
}
check("f6: dead tiers exact — all likely, no bogus table attribution",
      dead == expect_dead, str(sorted(dead ^ expect_dead)))


def digest(gr):
    return (sorted((k, sorted(v)) for k, v in gr.edges.items()),
            sorted((c["path"], c["func"], c["tier"]) for c in gr.dead_code(100)["candidates"]))


g2 = graph.Graph().build()
check("f7: double build byte-identical (edges + dead candidates)",
      digest(g) == digest(g2))

# ---- f8 end-to-end --------------------------------------------------------------

walked = sorted(navindex.iter_files())
check("f8: walk finds all 6 fixture files",
      sorted(p.as_posix() for p in walked)
      == sorted((HERE / "tests" / "fixtures" / "c" / r).as_posix() for r in
                (DEAD_C, MAIN, TABLE_C, UTIL_H, WIDGET_C, WIDGET_H)),
      str(walked))
check("f8: registry ownership .c -> c / .h -> cpp",
      registry_for(".c") is C and registry_for(".h") is CPP)
check("f8: every .c file parsed structurally (fns > 0)",
      all(len(g.files[r].funcs) > 0 for r in (MAIN, WIDGET_C, DEAD_C, TABLE_C)))

import shutil
shutil.rmtree(TMP, ignore_errors=True)
finish()
