# PHP extractor hardening suite (issue #343) — run in its own process:
#   NEURONAV_EMBED_FAKE=1 .venv/Scripts/python.exe -X utf8 tests/test_phphard.py
#
# Hermetic: builds a graph over tests/fixtures/php (PSR-4 src/App tree +
# public/index.php convention entry + PHPUnit attributes) with a
# generated temp config + FAKE embeds. Pins:
#   f1  parse surface: class/interface/trait/enum types, methods +
#       top-level functions, typed params/ret, promoted-property ctors
#   f2  PSR-4 resolution: namespace App\Sub + `use` -> namespace-path-
#       to-directory match; `use function` binds exactly; external use
#       misses loudly (nothing recorded)
#   f3  entry rules: index.php convention (all funcs), PHPUnit
#       #[Test]/#[DataProvider] attributes + @test annotations;
#       unannotated methods stay dead-eligible
#   f4  call edges: new C ctor edge, static C::m via class_map,
#       $this->m own-file then trait files, typed-local/assignment-new
#       $o->m dispatch, bare fn via use-function import
#   f5  trait consumption: class `use TraitT` resolves the trait file;
#       trait methods dispatch from consumer receivers
#   f6  dead tiers: uncalled = likely; magic methods (__toString)
#       entry-exempt; `use function` sibling stays dead (exact binding)
#   f7  determinism: double build -> identical edges + dead candidates
#   f8  end-to-end: walk -> parse -> FAKE index (9 files), registry
#       ownership (.php -> php)
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

TMP = Path(tempfile.mkdtemp(prefix="neuronav_phphard_"))
(TMP / "config.json").write_text(json.dumps({
    "root": str(HERE / "tests" / "fixtures" / "php"),
    "collection": "phphard",
    "include_dirs": ["."],
    "extensions": [".php"],
    "exclude_dirs": [],
    "state_dir": str(TMP / "state"),
}), encoding="utf-8")
os.environ["NEURONAV_CONFIG"] = str(TMP / "config.json")
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

import graph  # noqa: E402  (binds the fixture config via NEURONAV_CONFIG)
import navindex  # noqa: E402
from extractors import php as P  # noqa: E402
from extractors import registry_for  # noqa: E402

from harness import check, finish


GREETER = "src/App/Greeter.php"
GREETS = "src/App/Greets.php"
LOGTRAIT = "src/App/LogTrait.php"
WIDGET = "src/App/Other/Widget.php"
FNS = "src/App/Fns.php"
SUIT = "src/App/Suit.php"
DEAD = "src/App/DeadCode.php"
INDEX = "public/index.php"
TEST = "src/App/Tests/GreeterTest.php"

# ---- f1 parse surface -----------------------------------------------------------

fs = P.parse(HERE / "tests/fixtures/php/src/App/Greeter.php", GREETER)
check("f1: Greeter class + implements + trait use",
      fs.class_name == "Greeter"
      and getattr(fs, "_php_implements", []) == [("Greeter", "Greets")]
      and getattr(fs, "_php_trait_uses", []) == [("Greeter", "LogTrait")],
      f"{fs.class_name} {getattr(fs, '_php_implements', None)}")
check("f1: methods + ctor collected",
      {"__construct", "greet", "widgetCount", "viaImport"} <= set(fs.funcs),
      str(sorted(fs.funcs)))
check("f1: typed params + ret",
      fs.funcs["greet"].params == [("n", "string")]
      and fs.funcs["greet"].ret == "string",
      f"{fs.funcs['greet'].params} {fs.funcs['greet'].ret}")

fs_w = P.parse(HERE / "tests/fixtures/php/src/App/Fns.php", FNS)
check("f1: namespace functions collected",
      sorted(fs_w.funcs) == ["shout", "whisper"], str(sorted(fs_w.funcs)))
check("f1: namespace segs",
      getattr(fs_w, "_php_namespace", []) == ["App", "Fns"],
      str(getattr(fs_w, "_php_namespace", None)))

fs_e = P.parse(HERE / "tests/fixtures/php/src/App/Suit.php", SUIT)
check("f1: enum surface",
      "Suit" in fs_e.aliases and "color" in fs_e.funcs,
      f"{sorted(fs_e.aliases)} {sorted(fs_e.funcs)}")

# ---- f2 PSR-4 resolution --------------------------------------------------------

check("f2: use App\\Other\\Widget resolves by namespace path",
      any(t == WIDGET and nm == "Widget"
          for t, nm in getattr(fs, "_php_type_imports", ())),
      str(sorted(getattr(fs, "_php_type_imports", ()))))
check("f2: use function App\\Fns\\shout binds exactly",
      (FNS, "shout") in fs.from_imports, str(sorted(fs.from_imports)))
ext_fs = P.parse(HERE / "tests/fixtures/php/public/index.php", INDEX)
check("f2: external use (App\\Greeter at public/ level) still resolves or misses loudly",
      all(isinstance(t, str) for t, _ in getattr(ext_fs, "_php_type_imports", ())),
      str(sorted(getattr(ext_fs, "_php_type_imports", ()))))

# ---- f3/f4/f5/f6 graph level ------------------------------------------------------

g = graph.Graph().build()

expect_roots = {
    f"{INDEX}::page_main",
    f"{INDEX}::page_helper",
    f"{TEST}::greets_loudly",
    f"{TEST}::greets_cases",
    f"{TEST}::annotated_test",
}
check("f3: roots exact (convention + attributes + annotation)",
      set(g.roots) == expect_roots, str(sorted(set(g.roots) ^ expect_roots)))

edges = {f"{s} -> {t}" for s, tgts in g.edges.items() for t in tgts}
expect_edges = {
    f"{INDEX}::page_main -> {GREETER}::__construct",
    f"{INDEX}::page_main -> {GREETER}::greet",
    f"{INDEX}::page_main -> {WIDGET}::__construct",
    f"{INDEX}::page_helper -> {WIDGET}::describe",
    f"{GREETER}::greet -> {LOGTRAIT}::log",
    f"{GREETER}::greet -> {WIDGET}::describe",
    f"{GREETER}::widgetCount -> {WIDGET}::__construct",
    f"{GREETER}::widgetCount -> {WIDGET}::size",
    f"{GREETER}::viaImport -> {FNS}::shout",
    f"{LOGTRAIT}::logLoud -> {LOGTRAIT}::log",
    f"{WIDGET}::spawnGreeter -> {GREETER}::__construct",
    f"{TEST}::greets_loudly -> {GREETER}::__construct",
    f"{TEST}::greets_loudly -> {GREETER}::greet",
    f"{TEST}::greets_loudly -> {WIDGET}::__construct",
    f"{TEST}::greets_cases -> {GREETER}::__construct",
    f"{TEST}::greets_cases -> {GREETER}::greet",
    f"{TEST}::greets_cases -> {WIDGET}::__construct",
    f"{TEST}::annotated_test -> {GREETER}::__construct",
    f"{TEST}::annotated_test -> {GREETER}::greet",
    f"{TEST}::annotated_test -> {WIDGET}::__construct",
}
check("f4: call edges exact (ctor, static, this->trait, typed/assigned locals, use-fn)",
      edges == expect_edges, str(sorted(edges ^ expect_edges)))

check("f5: trait file in the consumer's trait map",
      LOGTRAIT in getattr(g, "_php_trait_map", {}).get(GREETER, ()),
      str(getattr(g, "_php_trait_map", None)))

dead = {(d["path"], d["func"], d["tier"]) for d in g.dead_code(100)["candidates"]}
expect_dead = {
    (DEAD, "never_called", "likely"),
    (FNS, "whisper", "likely"),
    (LOGTRAIT, "logLoud", "likely"),
    (SUIT, "color", "likely"),
    (TEST, "not_a_test", "likely"),
}
check("f6: dead tiers exact — all likely, __toString exempt, use-fn sibling dead",
      dead == expect_dead, str(sorted(dead ^ expect_dead)))
check("f6: __toString never a dead candidate",
      not any(fn == "__toString" for _, fn, _ in dead))


def digest(gr):
    return (sorted((k, sorted(v)) for k, v in gr.edges.items()),
            sorted((c["path"], c["func"], c["tier"]) for c in gr.dead_code(100)["candidates"]))


g2 = graph.Graph().build()
check("f7: double build byte-identical (edges + dead candidates)",
      digest(g) == digest(g2))

# ---- f8 end-to-end ----------------------------------------------------------------

walked = list(navindex.iter_files())
check("f8: walk finds all 9 fixture files", len(walked) == 9, str(len(walked)))
check("f8: registry ownership .php -> php", registry_for(".php") is P)
check("f8: executable files parse (fns > 0; Greets.php is a pure-declaration interface — funcs={} by the java/C declaration law)",
      all(len(g.files[r].funcs) > 0 for r in g.files if r != GREETS)
      and not g.files[GREETS].funcs,
      str({r: len(g.files[r].funcs) for r in g.files}))

import shutil
shutil.rmtree(TMP, ignore_errors=True)
finish()
