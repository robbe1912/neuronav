"""TypeScript hard-case suite (issue #245, plan §6): the 13-fixture battery.

Mirrors test_cpphard's harness: temp config rooted at tests/fixtures/ts,
rebuild graph, dead_code tiers, edge assertions on the real graph, plus
the TS-specific pins — grammar-split wheel smoke, barrel rebind, tsconfig
aliases (good + malformed), overload collapse, default exports, and the
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
FIX = Path(__file__).resolve().parent / "fixtures" / "ts"

from harness import FAILURES as FAILS, styled

check = styled("comma")  # byte pin: print-sep PASS lines


CFG = Path(tempfile.gettempdir()) / "neuronav_tshard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "tshard",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".ts", ".tsx", ".mts", ".cts"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

from extractors import ts as ts_x  # noqa: E402


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


# suite pin: grammar-split wheel smoke — both grammars import and parse
import tree_sitter_typescript as _tst  # noqa: E402
from tree_sitter import Language, Parser  # noqa: E402

_smoke_ts = Parser(Language(_tst.language_typescript()))
_smoke_tsx = Parser(Language(_tst.language_tsx()))
check("wheel smoke: ts grammar parses clean TS",
      _smoke_ts.parse(b"const x: number = 1;").root_node.has_error is False)
check("wheel smoke: tsx grammar parses JSX",
      _smoke_tsx.parse(b"const e = <a b=\"c\" />;").root_node.has_error is False)

# fixture 1: arrow_methods.ts
ff = g.files["arrow_methods.ts"]
check("f1 funcs surface (arrow/fn-expr/method/ctor/merged accessor)",
      set(ff.funcs) == {"arrowfn", "fnexpr", "constructor", "shape", "size",
                        "collide", "user"}, str(sorted(ff.funcs)))
check("f1 accessor name in name_literals", ff.name_literals == {"size"},
      str(sorted(ff.name_literals)))
check("f1 typed class field member", ff.members.get("name") == "string",
      str(dict(ff.members)))
check("f1 first-wins collision body", ff.funcs["collide"].body == "{ return 3; }",
      repr(ff.funcs["collide"].body))
check("f1 arrow signature ret", ff.funcs["arrowfn"].ret == "number",
      ff.funcs["arrowfn"].ret)
check("f1 user -> arrowfn edge",
      "arrow_methods.ts::arrowfn" in g.edges.get("arrow_methods.ts::user", set()),
      str(sorted(g.edges.get("arrow_methods.ts::user", set()))))

# fixture 2: overloads.ts
of = g.files["overloads.ts"]
check("f2 one collapsed ship Func", len([n for n in of.funcs if n == "ship"]) == 1,
      str(sorted(of.funcs)))
_ship_lines = [i + 1 for i, ln in enumerate(
    (FIX / "overloads.ts").read_text(encoding="utf-8").splitlines())
    if ln.startswith("function ship(")]
check("f2 ship line = FIRST declaration", of.funcs["ship"].line == _ship_lines[0],
      f"{of.funcs['ship'].line} vs {_ship_lines[0]}")
check("f2 ship body from implementation", "return a;" in of.funcs["ship"].body,
      repr(of.funcs["ship"].body))
check("f2 ship(1) alive through caller", alive("overloads.ts", "ship"))

# fixture 3: barrels + rebind
bv = g.files["barrel_view.tsx"]
check("f3 from_imports rebound to origin definers",
      {("origin_a.ts", "alpha"), ("origin_b.ts", "beta"),
       ("dead_helpers.ts", "gone")} == set(bv.from_imports),
      str(sorted(bv.from_imports)))
check("f3 .ts barrel wiring-only", ts_x.is_wiring_only(g.files["barrel_index.ts"]))
check("f3 .tsx barrel wiring-only", ts_x.is_wiring_only(g.files["barrel_next.tsx"]))
check("f3 barrels absent from dead-file candidates",
      not ({p for p, _ in DEAD} & {"barrel_index.ts", "barrel_next.tsx"}))
check("f3 alpha/beta alive through rebind",
      alive("origin_a.ts", "alpha") and alive("origin_b.ts", "beta"))

# fixture 4: default exports
check("f4 anonymous default -> Func \"default\"",
      "default" in g.files["anon_default.ts"].funcs)
check("f4 named default stays named", "App" in g.files["named_default.tsx"].funcs)
check("f4 importer wires Local to anon default",
      "anon_default.ts::default" in g.edges.get("default_imports.ts::run", set()),
      str(sorted(g.edges.get("default_imports.ts::run", set()))))

# fixture 5: dynamic imports
dyn = g.files["dynamic_imports.ts"]
check("f5 dynamic+require literal targets land", dyn.imported_modules == {"lazy/mod.ts"},
      str(sorted(dyn.imported_modules)))
check("f5 non-literal import() adds nothing", "dyn" not in dyn.imported_modules)
check("f5 whole-module liveness", alive("lazy/mod.ts", "lazychunk"))

# fixture 6: tsconfig aliases
check("f6 @/widget alias resolves to src/widget.ts",
      ("aliasing/src/widget.ts", "wfunc") in g.files["aliasing/user.ts"].from_imports,
      str(sorted(g.files["aliasing/user.ts"].from_imports)))
check("f6 alias call edge", alive("aliasing/src/widget.ts", "wfunc"))
# re-arm the one-shot unreadable-tsconfig warning (module flags directly —
# cached unreadable results would short-circuit past the warn on retry)
ts_x._WARNED_TSCONFIG = False
ts_x._TSCONFIG_FIND.clear()
ts_x._TSCONFIG_LOAD.clear()
_err1 = StringIO()
with redirect_stderr(_err1):
    ts_x.parse(FIX / "aliasing/bad/user_bad.ts", "aliasing/bad/user_bad.ts")
_err2 = StringIO()
with redirect_stderr(_err2):
    ts_x.parse(FIX / "aliasing/bad/user_bad.ts", "aliasing/bad/user_bad.ts")
_lines1 = [ln for ln in _err1.getvalue().splitlines() if ln.strip()]
_lines2 = [ln for ln in _err2.getvalue().splitlines() if ln.strip()]
check("f6 malformed tsconfig: exactly ONE warn line", len(_lines1) == 1, str(_lines1))
check("f6 warn is one-shot", len(_lines2) == 0, str(_lines2))
check("f6 malformed sibling still parses (pass-through)",
      set(g.files["aliasing/bad/user_bad.ts"].funcs) == {"auBad"})

# fixture 7: generic call sites
check("f7 maxf<T>( edges land",
      "generics_calls.ts::maxf" in g.edges.get("generics_calls.ts::usegen", set()),
      str(sorted(g.edges.get("generics_calls.ts::usegen", set()))))
check("f7 obj.compute<T>() member edge lands",
      "generics_calls.ts::compute" in g.edges.get("generics_calls.ts::usegen", set()))

# fixture 8: decorators
de = g.files["decorators_entry.ts"]
check("f8 class decorator hoists every method", {"m", "n"} <= de.entry_hints,
      sorted(de.entry_hints))
check("f8 method decorator hoists that method", "handler" in de.entry_hints)
check("f8 decorated names alive",
      alive("decorators_entry.ts", "m") and alive("decorators_entry.ts", "n")
      and alive("decorators_entry.ts", "handler"))

# fixture 9: JSX composition
check("f9 Child JSX edge", "child.tsx::Child" in
      g.edges.get("jsx_composition.tsx::render", set()),
      str(sorted(g.edges.get("jsx_composition.tsx::render", set()))))
check("f9 handleDone donated via arg_refs",
      "handleDone" in g.files["jsx_composition.tsx"].arg_refs)
check("f9 handleDone alive", alive("jsx_composition.tsx", "handleDone"))
_jsx_src = (FIX / "jsx_composition.tsx").read_bytes()
check("f9 grammar split: ts grammar ERRORs on tsx bytes",
      ts_x._PARSERS[".ts"].parse(_jsx_src).root_node.has_error is True)
check("f9 grammar split: tsx grammar parses clean",
      ts_x._PARSERS[".tsx"].parse(_jsx_src).root_node.has_error is False)

# fixture 10: enums / types
et = g.files["enums_types.ts"]
check("f10 enum members -> consts under <Enum>",
      et.consts == {"Red": "<Color>", "Green": "<Color>", "Blue": "<Color>"},
      str(dict(et.consts)))
check("f10 type/interface names -> aliases",
      et.aliases.get("Pair") == "type" and et.aliases.get("Shape") == "interface",
      str(dict(et.aliases)))
check("f10 interfaces contribute no funcs", "area" not in et.funcs)

# fixture 11: dead tiers
check("f11 orphan dead likely", tier("dead_helpers.ts", "orphan") == "likely",
      str(tier("dead_helpers.ts", "orphan")))
check("f11 gone review (mention floor 2: def + import site)",
      tier("dead_helpers.ts", "gone") == "review",
      str(tier("dead_helpers.ts", "gone")))
check("f11 _priv review (unresolved base + underscore)",
      tier("dead_helpers.ts", "_priv") == "review",
      str(tier("dead_helpers.ts", "_priv")))
check("f11 accessor alive via name_literals", alive("dead_helpers.ts", "value"))
check("f11 typed-receiver method alive", alive("dead_helpers.ts", "bump"))

# fixture 12: super calls
check("f12 super.tick() lands on base file tick",
      "base_cls.ts::tick" in g.edges.get("super_calls.ts::tick", set()),
      str(sorted(g.edges.get("super_calls.ts::tick", set()))))
check("f12 base tick alive", alive("base_cls.ts", "tick"))
check("f12 drive -> subclass override edge",
      "super_calls.ts::tick" in g.edges.get("super_calls.ts::drive", set()))

# fixture 13: ambient declarations
check("f13 .d.ts contributes zero funcs", g.files["ambient.d.ts"].funcs == {})
check("f13 .d.ts wiring-only", ts_x.is_wiring_only(g.files["ambient.d.ts"]))
check("f13 impl stays alive", alive("ambient_impl.ts", "wrapper"))

# fixture 14: vite-style React entry trio (tsx twin of the js React
# fixture, #293 defect 1 — pre-fix the whole exported component tree
# landed in review-tier dead rows)
check("f14 exported PascalCase App -> entry hint",
      "App" in g.files["App.tsx"].entry_hints, sorted(g.files["App.tsx"].entry_hints))
check("f14 exported PascalCase Button -> entry hint",
      "Button" in g.files["Button.tsx"].entry_hints,
      sorted(g.files["Button.tsx"].entry_hints))
check("f14 render-root keeps App alive", alive("App.tsx", "App"))
check("f14 JSX-in-body edge keeps Button alive", alive("Button.tsx", "Button"))
check("f14 vite trio: zero dead rows",
      not ({("App.tsx", "App"), ("Button.tsx", "Button")} & set(DEAD)),
      str(sorted(set(DEAD))))

# fixture 14b: a tsx re-read failure is loud ONCE, never silent (#293
# defect 3 — a vanishing .tsx used to drop JSX mention counts quietly)
from extractors.model import FileSym as _FS14  # noqa: E402

setattr(ts_x, "_WARNED_TSX_READ", False)  # re-arm (tsconfig-warn precedent)
_e1, _e2 = StringIO(), StringIO()
_vanishing = FIX / "no_such.tsx"
with redirect_stderr(_e1):
    _sites = ts_x._jsx_sites(_vanishing, _FS14(path="no_such.tsx", ext=".tsx"))
with redirect_stderr(_e2):
    ts_x._jsx_sites(_vanishing, _FS14(path="no_such.tsx", ext=".tsx"))
_lines1 = [ln for ln in _e1.getvalue().splitlines() if ln.strip()]
_lines2 = [ln for ln in _e2.getvalue().splitlines() if ln.strip()]
check("f14b vanishing tsx: sites empty, exactly ONE warn line",
      _sites == [] and len(_lines1) == 1, str(_lines1))
check("f14b warn is one-shot", len(_lines2) == 0, str(_lines2))

# suite pin: the package walk is one-shot per ctx even with no package.json
# anywhere (#293 defect 2 — the no-package sentinel)
check("suite package-walk sentinel recorded for this ctx",
      "" in ts_x._PKG_SEEN.get(g, set()), str(ts_x._PKG_SEEN.get(g)))


def _pin_dead_share_hook() -> None:
    """Plan C1 contract (consumed by bake/files_model via the registry):
    .ts/.tsx files contribute to dead-share; ambient/script suffixes never."""
    from extractors.model import FileSym
    check("hook counts_dead_share: .ts/.tsx True, .mts/.cts False",
          ts_x.counts_dead_share(FileSym(path="a.ts", ext=".ts"))
          and ts_x.counts_dead_share(FileSym(path="b.tsx", ext=".tsx"))
          and not ts_x.counts_dead_share(FileSym(path="c.mts", ext=".mts"))
          and not ts_x.counts_dead_share(FileSym(path="d.cts", ext=".cts")))


_pin_dead_share_hook()

# fixture 15: file-based routing (#328) — expo-router `app/**` and Next
# `pages/**` mount route files by convention, so an anonymous-default
# route (and its exclusive deps) is an entry, never a dead candidate
check("f28 anon-default route under app/ is an entry",
      alive("app/index.tsx", "default"))
check("f28 the route's exclusive lowercase helper revives",
      alive("lib/route_row.ts", "renderRow"))
check("f28 pages/ route is an entry", alive("pages/about.tsx", "default"))
check("f28 app/(group)/ route is an entry",
      alive("app/(g)/profile.tsx", "default"))
check("f28 lowercase orphan OUTSIDE routing roots stays dead",
      tier("orphan_anon.ts", "renderAlone") == "likely",
      str(tier("orphan_anon.ts", "renderAlone")))
_routed = sorted(
    p.relative_to(FIX).as_posix()
    for root in ("app", "pages") for p in (FIX / root).rglob("*")
    if p.is_file())
check("f28 routing-root family is exactly the #328+#340 fixture set",
      _routed == ["app/(g)/profile.tsx", "app/(g)/settings/layout.tsx",
                  "app/a/b.tsx", "app/dashboard/page.tsx", "app/index.tsx",
                  "app/lib/colo.tsx", "pages/about.tsx", "pages/sub/x.tsx"],
      str(_routed))

# fixture 15b: deep route nesting (#340) — real file-routing trees mount
# routes at ANY depth under a top-level root; the direct-only rule left
# every deeper route (and its exclusive helper — the import originates
# from a dead file) trending dead
check("f28 #340 deep app/dashboard/page.tsx route is an entry",
      alive("app/dashboard/page.tsx", "default"))
check("f28 #340 deep route's exclusive helper revives",
      alive("lib/page_deep.ts", "renderPageDeep"))
check("f28 #340 expo deep-tree app/a/b.tsx is an entry",
      alive("app/a/b.tsx", "default"))
check("f28 #340 expo deep helper revives",
      alive("lib/deep_b.ts", "renderDeepB"))
check("f28 #340 nested pages/sub/x.tsx route is an entry",
      alive("pages/sub/x.tsx", "default"))
check("f28 #340 nested pages helper revives",
      alive("lib/sub_x.ts", "renderSubX"))
check("f28 #340 group+depth app/(g)/settings/layout.tsx is an entry",
      alive("app/(g)/settings/layout.tsx", "default"))
check("f28 #340 group+depth helper revives",
      alive("lib/layout_g.ts", "renderLayoutG"))
check("f28 #340 colocation file inside app/ roots too (over-approx " +
      "toward ALIVE, honest leg)", alive("app/lib/colo.tsx", "coloHelper"))

# suite-level pins
_ts_rows = [(p, n) for (p, n) in DEAD if p.endswith((".ts", ".tsx"))]
_alive_ts = [f"{rel}::{nm}" for rel, fs in g.files.items()
             if fs.ext in ts_x.TS_EXTS
             for nm in fs.funcs if (rel, nm) not in DEAD]
check("suite non-vacuity: >=10 alive TS funcs", len(_alive_ts) >= 10,
      str(len(_alive_ts)))
check("suite non-vacuity: >=6 dead TS rows", len(_ts_rows) >= 6, str(len(_ts_rows)))
check("suite no decorated name in dead candidates",
      not ({("decorators_entry.ts", n) for n in ("m", "n", "handler")} & set(DEAD)))
check("suite no wiring-only file in dead-file tier",
      not ({p for p, _ in DEAD}
           & {fs.path for fs in g.files.values() if ts_x.is_wiring_only(fs)}))

g2 = graph.get_graph(rebuild=True)
check("suite determinism double-run", digest(g) == digest(g2))

# byte pin: summary without leading blank line — kept local
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
