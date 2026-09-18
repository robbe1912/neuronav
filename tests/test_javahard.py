# Java extractor hardening suite (issue #335) — run in its own process:
#   NEURONAV_EMBED_FAKE=1 .venv/Scripts/python.exe -X utf8 tests/test_javahard.py
#
# Hermetic: builds a graph over tests/fixtures/java (standard Maven-style
# src/main/java + src/test/java layouts, demo + demo.shape packages) with
# a generated temp config + FAKE embeds. Pins:
#   f1  parse surface: types, methods (static/instance/default), ctors,
#       fields; package resolution by package-path-to-directory match
#   f2  imports: plain -> resolved type table, static member -> binds
#       exactly that method, external (java.util.*) -> loud degrade
#       (nothing recorded, no bogus edges)
#   f3  entry rules: main(String[]) + JUnit @Test/@ParameterizedTest;
#       unannotated methods stay dead-eligible
#   f4  call edges: typed-local instance call, static-import bare call,
#       qualified static call, `new` ctor
#   f5  interface default-method dispatch (rust trait-default shape) +
#       @Override dispatch shielding (review tier, never likely)
#   f6  dead tiers: uncalled public = likely; Object machinery
#       (toString) = entry-exempt
#   f7  determinism: double build -> identical edges + dead candidates
#   f8  end-to-end: walk -> parse -> FAKE index (7 files)
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

TMP = Path(tempfile.mkdtemp(prefix="neuronav_javahard_"))
(TMP / "config.json").write_text(json.dumps({
    "root": str(HERE / "tests" / "fixtures" / "java"),
    "collection": "javahard",
    "include_dirs": ["."],
    "extensions": [".java"],
    "exclude_dirs": [],
    "state_dir": str(TMP / "state"),
}), encoding="utf-8")
os.environ["NEURONAV_CONFIG"] = str(TMP / "config.json")
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

import graph  # noqa: E402  (binds the fixture config via NEURONAV_CONFIG)
import nav  # noqa: E402
from extractors import java as J  # noqa: E402
from extractors.java import JAVA_EXTS  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


APP = "src/main/java/demo/App.java"
GREETER = "src/main/java/demo/Greeter.java"
SHAPE = "src/main/java/demo/shape/Shape.java"
FLYER = "src/main/java/demo/shape/Flyer.java"
CIRCLE = "src/main/java/demo/shape/Circle.java"
DEAD = "src/main/java/demo/Dead.java"
TEST = "src/test/java/demo/AppTest.java"

# ---- f8: walk -> parse -> index -------------------------------------------
walked = sorted(nav.file_id(p) for p in nav.iter_files())
check("f8: walk finds the 7 fixture files under standard layouts",
      walked == [APP, DEAD, GREETER, CIRCLE, FLYER, SHAPE, TEST],
      str(walked))
stats = nav.rescan()
graph.sync_functions(stats["changed"], stats["deleted_paths"])
check("f8: FAKE index stores every walked file", nav.count() == 7,
      f"count={nav.count()}")

# ---- f1: parse surface + package resolution --------------------------------
import extractors  # noqa: E402

check("f1: registry binds .java to the module",
      extractors.registry_for(".java") is J, "registry_for(.java)")
app = J.parse(HERE / "tests/fixtures/java/src/main/java/demo/App.java", APP)
shape = J.parse(HERE / "tests/fixtures/java/src/main/java/demo/shape/Shape.java", SHAPE)
circle = J.parse(HERE / "tests/fixtures/java/src/main/java/demo/shape/Circle.java", CIRCLE)
check("f1: package decl resolved (keyword stripped)",
      app._java_package == "demo" and shape._java_package == "demo.shape",
      f"{app._java_package} / {shape._java_package}")
greeter = J.parse(HERE / "tests/fixtures/java/src/main/java/demo/Greeter.java", GREETER)
check("f1: methods, ctors and fields parsed",
      sorted(app.funcs) == ["helper", "main"]
      and "Circle" in circle.funcs  # constructor named like the type
      and sorted(shape.funcs) == ["describe"]
      and greeter.members.get("prefix") == "String",
      f"app={sorted(app.funcs)} circle={sorted(circle.funcs)} "
      f"shape={sorted(shape.funcs)} members={greeter.members}")
check("f1: interface default method has a body (dispatch source)",
      "area()" in shape.funcs["describe"].body or "area" in shape.funcs["describe"].body,
      shape.funcs["describe"].body[:60])
check("f1: JAVA_EXTS is the single suffix truth",
      JAVA_EXTS == frozenset({".java"}), str(JAVA_EXTS))

# ---- f2: import resolution ---------------------------------------------------
check("f2: static member import binds exactly that method",
      sorted(app.from_imports) == [(GREETER, "banner")],
      str(sorted(app.from_imports)))
tis = sorted(getattr(app, "_java_type_imports", ()))
check("f2: plain imports resolve by package-path-to-directory match",
      tis == [(FLYER, "Flyer"), (SHAPE, "Shape")], str(tis))
check("f2: external imports degrade loud (nothing recorded)",
      not any("java/util" in t or t[1] == "List" for t in tis + sorted(app.from_imports))
      and not app.imported_modules,
      f"tis={tis} mods={sorted(app.imported_modules)}")

# ---- f3: entry rules ---------------------------------------------------------
g = graph.Graph().build()
reach = set(g.reachable)
check("f3: main(String[]) is an entry root", f"{APP}::main" in g.reachable,
      f"{APP}::main")
check("f3: JUnit @Test/@ParameterizedTest methods are entry roots",
      all(f"{TEST}::{m}" in g.reachable for m in ("greets", "bannerTest", "areas")),
      str(sorted(r for r in g.reachable if r.startswith(TEST))))
check("f3: unannotated test-class method stays dead-eligible",
      f"{TEST}::notATest" not in g.reachable, f"{TEST}::notATest")

# ---- f4: call edges ----------------------------------------------------------
out = set(g.edges.get(f"{APP}::main", ()))
check("f4: typed-local instance call reaches the defining file",
      any(e.startswith(GREETER + "::") and "greet" in e for e in out), str(sorted(out)))
check("f4: static-import bare call binds the imported method",
      f"{GREETER}::banner" in out, str(sorted(out)))
check("f4: qualified `new a.b.C(...)` edges the explicit ctor (tail class)",
      f"{CIRCLE}::Circle" in out, str(sorted(out)))
check("f4: default-ctor `new Greeter()` stays name-level alive (no ctor Func)",
      "Greeter" in g.referenced_names
      and f"{GREETER}::Greeter" not in g.edges.get(f"{APP}::main", ()),
      str(sorted(g.referenced_names & {"Greeter"})))
tout = set(g.edges.get(f"{TEST}::bannerTest", ()))
check("f4: qualified static call (Greeter.banner()) resolves via class_map",
      f"{GREETER}::banner" in tout, str(sorted(tout)))

# ---- f5: interface default dispatch + @Override shielding --------------------
check("f5: interface default method dispatched on interface-typed receiver",
      f"{SHAPE}::describe" in out, str(sorted(out)))
check("f5: second interface's default dispatched too",
      f"{FLYER}::fly" in out, str(sorted(out)))
dead = g.dead_code(100)
tiers = {(c["path"], c["func"]): c["tier"] for c in dead["candidates"]}
check("f5: @Override method shields to review, never likely",
      tiers.get((CIRCLE, "fly")) == "review"
      and tiers.get((CIRCLE, "area"), "none") in ("review", "none"),
      str({k: v for k, v in tiers.items() if k[0] == CIRCLE}))

# ---- f6: dead tiers ----------------------------------------------------------
check("f6: uncalled public method = likely dead",
      tiers.get((DEAD, "neverCalled")) == "likely", str(sorted(tiers)))
check("f6: unannotated test-class method = likely dead",
      tiers.get((TEST, "notATest")) == "likely", str(sorted(tiers)))
check("f6: Object machinery (toString) is entry-exempt, never dead",
      all(k[1] != "toString" for k in tiers), str(sorted(tiers)))

# ---- f7: determinism ---------------------------------------------------------
def digest(gr):
    return (sorted((k, sorted(v)) for k, v in gr.edges.items()),
            sorted((c["path"], c["func"], c["tier"]) for c in gr.dead_code(100)["candidates"]))

g2 = graph.Graph().build()
check("f7: double build byte-identical (edges + dead candidates)",
      digest(g) == digest(g2))

import shutil
shutil.rmtree(TMP, ignore_errors=True)
print(("JAVAHARD OK" if not FAILS else f"JAVAHARDFAILS: {FAILS}"))
sys.exit(1 if FAILS else 0)
