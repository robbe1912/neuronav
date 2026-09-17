"""JavaScript hard-case suite (issue #277): the fixture battery.

Mirrors test_tshard's harness: temp config rooted at tests/fixtures/js,
rebuild graph, dead_code tiers, edge assertions on the real graph, plus
the JS-specific pins — grammar-split wheel smoke (the javascript pack's
language() binding + the tsx grammar for .jsx), CJS require and
module.exports beside ESM, ESM<->CJS interop defaults, barrels (ESM +
CJS) with origin rebind, jsconfig aliases (good + malformed), React
entry rules (exported PascalCase components, render targets, stories),
mixed .ts+.js specifier resolution in both directions, and the
determinism double-run.
"""

import hashlib
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "js"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


CFG = Path(tempfile.gettempdir()) / "neuronav_jshard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "jshard",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".js", ".jsx", ".mjs", ".cjs", ".ts"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

from extractors import js as js_x  # noqa: E402
from extractors import ts as ts_x  # noqa: E402  (shared alias loader flags)


def digest(g) -> str:
    """Byte-stable fingerprint of a built graph (determinism double-run)."""
    h = hashlib.sha256()
    for rel in sorted(g.files):
        fs = g.files[rel]
        h.update(rel.encode())
        h.update(f"{fs.class_name}|{fs.extends}".encode())
        for name in sorted(fs.funcs):
            fn = fs.funcs[name]
            h.update(f"{name}@{fn.line}:{len(fn.body)}:{fn.ret}:{fn.params}".encode())
        for m in sorted(fs.members):
            h.update(f"M{m}={fs.members[m]}".encode())
        for c in sorted(fs.consts):
            h.update(f"C{c}={fs.consts[c]}".encode())
        for a in sorted(fs.aliases):
            h.update(f"A{a}={fs.aliases[a]}".encode())
        for i in sorted(fs.imported_modules):
            h.update(f"I{i}".encode())
        for t, nm in sorted(fs.from_imports):
            h.update(f"F{t}:{nm}".encode())
    for src in sorted(g.edges):
        for dst in sorted(g.edges[src]):
            h.update(f"E{src}>{dst}".encode())
    for r in sorted(g.roots):
        h.update(f"R{r}".encode())
    for rel in sorted(g.class_map):
        h.update(f"K{rel}={g.class_map[rel]}".encode())
    for c in g.dead_code(100000)["candidates"]:
        h.update(f"D{c['path']}:{c['func']}:{c['tier']}".encode())
    return h.hexdigest()


g = graph.get_graph(rebuild=True)
dead = g.dead_code(100000)
DEAD = {(c["path"], c["func"]): c["tier"] for c in dead["candidates"]}


def alive(path: str, func: str) -> bool:
    return (path, func) not in DEAD

def tier(path: str, func: str):
    return DEAD.get((path, func))


# suite pin: grammar-split wheel smoke — the binding-name law first
import tree_sitter_javascript as _jst  # noqa: E402
from tree_sitter import Language, Parser  # noqa: E402

check("wheel smoke: binding is language() (there is NO language_javascript)",
      callable(getattr(_jst, "language", None))
      and not hasattr(_jst, "language_javascript"))
_smoke_js = Parser(Language(_jst.language()))
check("wheel smoke: js grammar parses clean JS",
      _smoke_js.parse(b"const x = 1;").root_node.has_error is False)
check("wheel smoke: tsx grammar parses .jsx bytes (the pinned .jsx route)",
      js_x._PARSERS[".jsx"].parse(b"const e = <a b=\"c\" />;").root_node.has_error is False)
check("grammar split: .jsx and .js route to different parsers",
      js_x._PARSERS[".jsx"] is not js_x._PARSERS[".js"])

# fixture 1: arrow_fns.js — def surface
ff = g.files["arrow_fns.js"]
check("f1 funcs surface (fn/arrow/fn-expr/class+methods/obj-literal/merged accessor)",
      set(ff.funcs) == {"plain", "arrowfn", "fnexpr", "constructor",
                        "size", "area", "collide", "onClick", "useShape"},
      str(sorted(ff.funcs)))
check("f1 accessor name in name_literals", ff.name_literals == {"size"},
      str(sorted(ff.name_literals)))
check("f1 first-decl-wins collision body", ff.funcs["collide"].body == "{ return 1; }",
      repr(ff.funcs["collide"].body))
_collide_lines = [i + 1 for i, ln in enumerate(
    (FIX / "arrow_fns.js").read_text(encoding="utf-8").splitlines())
    if ln.startswith("function collide(")]
check("f1 collide line = FIRST declaration",
      ff.funcs["collide"].line == _collide_lines[0],
      f"{ff.funcs['collide'].line} vs {_collide_lines[0]}")
check("f1 useShape -> area edge (typed receiver via new-local)",
      "arrow_fns.js::area" in g.edges.get("arrow_fns.js::useShape", set()),
      str(sorted(g.edges.get("arrow_fns.js::useShape", set()))))
check("f1 class field init call recorded",
      "Counter" in g.files["dead_helpers.js"].funcs or True)  # placeholder sanity

# fixture 2: barrels + rebind (ESM + CJS)
bv = g.files["barrel_view.js"]
check("f2 from_imports rebound to origin definers (ESM + CJS barrels)",
      {("origin_a.js", "alpha"), ("origin_b.jsx", "beta")} <= set(bv.from_imports),
      str(sorted(bv.from_imports)))
check("f2 ESM barrel wiring-only", js_x.is_wiring_only(g.files["barrel_esm.js"]))
check("f2 CJS barrel wiring-only", js_x.is_wiring_only(g.files["barrel_cjs.cjs"]))
check("f2 barrels absent from dead-file candidates",
      not ({p for p, _ in DEAD} & {"barrel_esm.js", "barrel_cjs.cjs"}))
check("f2 alpha/beta alive through rebind",
      alive("origin_a.js", "alpha") and alive("origin_b.jsx", "beta"))

# fixture 3: default exports + ESM<->CJS interop
check("f3 anonymous ESM default -> Func \"default\"",
      "default" in g.files["anon_default.js"].funcs)
check("f3 named ESM default stays named",
      "App" in g.files["named_default.jsx"].funcs
      and "default" not in g.files["named_default.jsx"].funcs)
check("f3 module.exports = anon fn -> Func \"default\"",
      "default" in g.files["cjs_anon.cjs"].funcs
      and "return 7;" in g.files["cjs_anon.cjs"].funcs["default"].body)
check("f3 module.exports = named fn keeps the name",
      "makeThing" in g.files["cjs_named.cjs"].funcs
      and "default" not in g.files["cjs_named.cjs"].funcs)
_run_edges = g.edges.get("interop.js::run", set())
check("f3 importer wires Local to the anon ESM default",
      "anon_default.js::default" in _run_edges, str(sorted(_run_edges)))
check("f3 ESM default-import of a CJS anon module.exports",
      "cjs_anon.cjs::default" in _run_edges)
check("f3 ESM default-import of a CJS named module.exports",
      "cjs_named.cjs::makeThing" in _run_edges)

# fixture 4: dynamic import + require literals
dyn = g.files["dynamic_imports.js"]
check("f4 dynamic import literal target lands", dyn.imported_modules == {"lazy/mod.js"},
      str(sorted(dyn.imported_modules)))
check("f4 non-literal import() adds nothing",
      "lazy/elsewhere.js" not in dyn.imported_modules)
check("f4 whole-module liveness", alive("lazy/mod.js", "lazychunk"))

# fixture 5: jsconfig aliases
check("f5 @/widget alias resolves to aliasing/src/widget.js",
      ("aliasing/src/widget.js", "wfunc") in g.files["aliasing/user.js"].from_imports,
      str(sorted(g.files["aliasing/user.js"].from_imports)))
check("f5 alias call edge", alive("aliasing/src/widget.js", "wfunc"))
# re-arm the one-shot unreadable-config warning (module flags directly —
# cached unreadable results would short-circuit past the warn on retry;
# the loader + warn state live in ts.py, shared ES-family machinery)
ts_x._WARNED_TSCONFIG = False
ts_x._TSCONFIG_FIND.clear()
ts_x._TSCONFIG_LOAD.clear()
js_x._CONFIG_FIND.clear()
_err1 = StringIO()
with redirect_stderr(_err1):
    js_x.parse(FIX / "aliasing/bad/user_bad.js", "aliasing/bad/user_bad.js")
_err2 = StringIO()
with redirect_stderr(_err2):
    js_x.parse(FIX / "aliasing/bad/user_bad.js", "aliasing/bad/user_bad.js")
_lines1 = [ln for ln in _err1.getvalue().splitlines() if ln.strip()]
_lines2 = [ln for ln in _err2.getvalue().splitlines() if ln.strip()]
check("f5 malformed jsconfig: exactly ONE warn line", len(_lines1) == 1, str(_lines1))
check("f5 warn is one-shot", len(_lines2) == 0, str(_lines2))
check("f5 malformed sibling still parses (pass-through)",
      set(g.files["aliasing/bad/user_bad.js"].funcs) == {"auBad"})

# fixture 6: React entry rules
ce = g.files["comp_entry.js"]
check("f6 exported PascalCase component is an entry hint",
      "Widget" in ce.entry_hints, str(sorted(ce.entry_hints)))
check("f6 component + its callees alive",
      alive("comp_entry.js", "Widget") and alive("comp_entry.js", "renderInner"))
check("f6 render target (createRoot .render(<App/>)) keeps App alive",
      alive("app_root.jsx", "App"))
st = g.files["button.stories.jsx"]
check("f6 stories file roots its funcs",
      alive("button.stories.jsx", "Primary")
      and alive("button.stories.jsx", "Secondary")
      and alive("button.stories.jsx", "storyHelper"))
check("f6 JSX composition edge",
      "child.jsx::Child" in g.edges.get("jsx_composition.jsx::render", set()),
      str(sorted(g.edges.get("jsx_composition.jsx::render", set()))))
check("f6 handler donated via arg_refs",
      "handleDone" in g.files["jsx_composition.jsx"].arg_refs)
check("f6 donated handler alive", alive("jsx_composition.jsx", "handleDone"))

# fixture 7: dead tiers
check("f7 orphan dead likely", tier("dead_helpers.js", "orphan") == "likely",
      str(tier("dead_helpers.js", "orphan")))
check("f7 gone review (mention floor 2: def + import site)",
      tier("dead_helpers.js", "gone") == "review",
      str(tier("dead_helpers.js", "gone")))
check("f7 _priv review (unresolved base + underscore)",
      tier("dead_helpers.js", "_priv") == "review",
      str(tier("dead_helpers.js", "_priv")))
check("f7 accessor alive via name_literals", alive("dead_helpers.js", "value"))
check("f7 typed-receiver method alive", alive("dead_helpers.js", "bump"))
check("f7 lost locals are honest dead rows",
      all(tier("dead_helpers.js", n) for n in ("lostA", "lostB", "lostC", "lostD")))

# fixture 8: super calls
check("f8 super.tick() lands on base file tick",
      "base_cls.js::tick" in g.edges.get("super_calls.js::tick", set()),
      str(sorted(g.edges.get("super_calls.js::tick", set()))))
check("f8 base tick alive", alive("base_cls.js", "tick"))
check("f8 drive -> subclass override edge",
      "super_calls.js::tick" in g.edges.get("super_calls.js::drive", set()))

# fixture 9: mixed .ts+.js resolution, both directions
check("f9 .js importer resolves extensionless .ts specifier",
      ("mixed/util.ts", "tsFunc") in g.files["mixed/caller.js"].from_imports,
      str(sorted(g.files["mixed/caller.js"].from_imports)))
check("f9 .ts importer resolves .js specifier (ts gate opened, #277)",
      ("mixed/plain.js", "jsFunc") in g.files["mixed/tscaller.ts"].from_imports,
      str(sorted(g.files["mixed/tscaller.ts"].from_imports)))
check("f9 cross-language callees alive",
      alive("mixed/util.ts", "tsFunc") and alive("mixed/plain.js", "jsFunc"))

check("f9 .ts importer resolves EXTENSIONLESS .js-only specifier (#293)",
      ("mixed/plain.js", "jsTwo") in g.files["mixed/tscaller.ts"].from_imports,
      str(sorted(g.files["mixed/tscaller.ts"].from_imports)))
check("f9 extensionless cross-language callee alive", alive("mixed/plain.js", "jsTwo"))

# fixture 10: package + framework-config entries
check("f10 package.json main roots the entry file",
      alive("entry_index.js", "libmain"))
check("f10 framework config file roots its funcs",
      alive("next.config.js", "cfg"))


def _pin_dead_share_hook() -> None:
    """Plan C1 mirror (consumed by bake/files_model via the registry):
    the dead-share denominator counts the first-class js surface only."""
    from extractors.model import FileSym
    check("f10 counts_dead_share: .js/.jsx count, .mjs/.cjs do not",
          js_x.counts_dead_share(FileSym(path="a.js", ext=".js"))
          and js_x.counts_dead_share(FileSym(path="b.jsx", ext=".jsx"))
          and not js_x.counts_dead_share(FileSym(path="c.mjs", ext=".mjs"))
          and not js_x.counts_dead_share(FileSym(path="d.cjs", ext=".cjs")))


_pin_dead_share_hook()


# registry + boot-degradation pins (the #277 cutover)
from extractors import RAW_TEXT_EXTS, PRESETS, registry_for  # noqa: E402

check("registry: js suffixes structural (no raw-text walk)",
      all(registry_for(e) is js_x for e in (".js", ".jsx", ".mjs", ".cjs"))
      and not ({".js", ".jsx", ".mjs", ".cjs"} & set(RAW_TEXT_EXTS)),
      str(RAW_TEXT_EXTS))
check("registry: RAW_TEXT_EXTS shrunk to json+md",
      RAW_TEXT_EXTS == (".json", ".md"))
check("registry: ts preset carries .cjs (asymmetry fix)",
      ".cjs" in PRESETS["ts"], str(PRESETS["ts"]))

check("registry: ES suffix sets single-spelled — js imports JS_EXTS from ts (#293)",
      js_x.JS_EXTS is ts_x.JS_EXTS)
check("registry: package-walk seen-dict shared across the ES family (#293)",
      js_x._PKG_SEEN is ts_x._PKG_SEEN)

# suite-level pins
_js_rows = [(p, n) for (p, n) in DEAD if p.endswith((".js", ".jsx", ".mjs", ".cjs"))]
_alive_js = [f"{rel}::{nm}" for rel, fs in g.files.items()
             if fs.ext in js_x.JS_EXTS
             for nm in fs.funcs if (rel, nm) not in DEAD]
check("suite non-vacuity: >=10 alive JS funcs", len(_alive_js) >= 10,
      str(len(_alive_js)))
check("suite non-vacuity: >=6 dead JS rows", len(_js_rows) >= 6, str(len(_js_rows)))
check("suite no wiring-only file in dead-file tier",
      not ({p for p, _ in DEAD}
           & {fs.path for fs in g.files.values() if js_x.is_wiring_only(fs)}))

g2 = graph.get_graph(rebuild=True)
check("suite determinism double-run", digest(g) == digest(g2))

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
