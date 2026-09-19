"""Lua hard-case suite (issue #342): the fixture battery.

Mirrors test_gohard's harness: temp config rooted at tests/fixtures/lua,
rebuild graph, dead_code tiers, edge assertions on the real graph, plus
the Lua-specific pins — grammar wheel smoke, require() dotted-path
resolution (Neovim lua/ layout + directory init.lua modules) with the
loud unresolved-require degrade, module-var bindings and whole-module
import liveness (the js law), dynamic requires on the mention floor,
init.lua/main.lua convention entries gated on being-required,
_spec/test-dir entry rules, static T.m dispatch (require-bound /
own-file table / global-table corpus-wide), the t:m colon form,
metatable __index base-method liveness (the go iface floor), circular
requires, the sabotage teeth, and the determinism double-run.
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "lua"

from harness import finish, styled

check = styled("comma")  # byte pin: print-sep PASS lines

CFG = Path(tempfile.gettempdir()) / "neuronav_luahard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "luahard",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".lua"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

from extractors import lua as lx  # noqa: E402
from extractors import EXTENSIONS, PRESETS, registry_for  # noqa: E402


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
        for mv in sorted(fs.module_vars):
            h.update(f"V{mv}={fs.module_vars[mv]}".encode())
        for e in sorted(fs.entry_hints):
            h.update(f"H{e}".encode())
    for src in sorted(g.edges):
        for dst in sorted(g.edges[src]):
            h.update(f"E{src}>{dst}".encode())
    for r in sorted(g.roots):
        h.update(f"R{r}".encode())
    for c in g.dead_code(100000)["candidates"]:
        h.update(f"D{c['path']}:{c['func']}:{c['tier']}".encode())
    return h.hexdigest()


# suite pin: grammar wheel smoke — the pinned wheel loads and parses
import tree_sitter_lua as _tslua  # noqa: E402  (the pinned wheel, #342)

_root = lx._PARSER.parse(b"local M = {}\nfunction M.f() end\n").root_node
check("grammar wheel: tree-sitter-lua 0.5.0 parses a module table",
      _root.has_error is False and b"function_declaration" in bytes(
          str(_root).encode()), str(type(_tslua)))

# registry ownership (issue #342 registration law)
check("registry: .lua -> extractors.lua + preset",
      registry_for(".lua") is lx and ".lua" in EXTENSIONS
      and PRESETS["lua"] == (".lua", ".json", ".md"),
      str(getattr(registry_for(".lua"), "__name__", None)))

# ---- parse-level pins (hermetic: parse() only, no graph) ------------------------

_init_fs = lx.parse(FIX / "lua/myplug/init.lua", "lua/myplug/init.lua")
check("parse: funcs collected (named, local, nested)",
      {"setup", "teardown", "inner"} == set(_init_fs.funcs),
      str(sorted(_init_fs.funcs)))
check("parse: byte-offset line number (#472 — never start_point)",
      _init_fs.funcs["setup"].line == 5, str(_init_fs.funcs["setup"].line))
check("parse: require binding -> module var (js law)",
      _init_fs.module_vars.get("util") == "module:lua/myplug/util.lua"
      and "lua/myplug/util.lua" in _init_fs.imported_modules,
      str(_init_fs.module_vars))

_glob_fs = lx.parse(FIX / "lua/myplug/global_api.lua",
                    "lua/myplug/global_api.lua")
check("parse: global table methods keyed by table name",
      _glob_fs._lua_methods.get("GlobalSvc") == {"ping", "unused_g"}
      and "GlobalSvc" not in _glob_fs._lua_tables,
      f"{sorted(_glob_fs._lua_methods)} {_glob_fs._lua_tables}")

_base_fs = lx.parse(FIX / "lua/myplug/base.lua", "lua/myplug/base.lua")
check("parse: local tables recorded, global assignment is not one",
      _base_fs._lua_tables == {"Base", "Derived"},
      str(_base_fs._lua_tables))
check("parse: metatable __index bases harvested",
      _base_fs._lua_index == {"Base"}, str(_base_fs._lua_index))

# loud-degrade leg: unresolved literal require notes once, records nothing
lx._WARNED_UNRESOLVED = False
_err = io.StringIO()
with contextlib.redirect_stderr(_err):
    _dyn_fs = lx.parse(FIX / "lua/myplug/dyn_loader.lua",
                       "lua/myplug/dyn_loader.lua")
check("parse: unresolved require degrades loudly (never silent)",
      "unresolved require" in _err.getvalue()
      and "some.external.lib" not in {
          v for v in _dyn_fs.module_vars.values()}
      and not any("some.external" in i
                  for i in _dyn_fs.imported_modules),
      _err.getvalue().strip().splitlines()[-1] if _err.getvalue() else "no note")

# dynamic require: no literal, no binding — mention floor carries it
check("parse: dynamic require records no binding",
      "name" not in _dyn_fs.module_vars, str(_dyn_fs.module_vars))

# ---- graph-level pins ------------------------------------------------------------

g = graph.get_graph(rebuild=True)
dead = g.dead_code(100000)
DEAD = {(c["path"], c["func"]): c["tier"] for c in dead["candidates"]}


def alive(path: str, func: str) -> bool:
    return (path, func) not in DEAD


# entry rules: convention roots + spec rules, gated on being-required
check("entry: main.lua convention roots love.load/update",
      "main.lua::load" in g.roots and "main.lua::update" in g.roots,
      str(sorted(r for r in g.roots if r.startswith("main.lua"))))
check("entry: never-required init.lua roots (Neovim convention)",
      "lua/other_plug/init.lua::activate" in g.roots,
      str(sorted(r for r in g.roots if "other_plug" in r)))
check("entry: REQUIRED init.lua is not a convention root",
      "lua/myplug/init.lua::setup" not in g.roots)
check("entry: _spec.lua + test-dir funcs root (gohard _test.go law)",
      "spec/util_spec.lua::test_add" in g.roots
      and "spec/util_spec.lua::spec_helper" in g.roots,
      str(sorted(r for r in g.roots if "spec/" in r)))

# reachability through every resolution arm
check("alive: require-bound call (plug.setup from the LÖVE root)",
      alive("lua/myplug/init.lua", "setup"))
check("alive: whole-module import liveness keeps unused_helper (js law)",
      alive("lua/myplug/util.lua", "unused_helper"))
check("alive: global-table call corpus-wide (GlobalSvc.ping)",
      alive("lua/myplug/global_api.lua", "ping"))
check("alive: own-file table static + colon calls (T.inner)",
      alive("lua/myplug/init.lua", "inner"))
check("alive: __index base methods (go iface floor)",
      alive("lua/myplug/base.lua", "b_meth"))
check("alive: circular requires keep both modules' funcs",
      alive("lua/myplug/circ_a.lua", "a_fn")
      and alive("lua/myplug/circ_b.lua", "b_fn"))

# the honest dead rows
check("dead: single-mention global method is likely",
      DEAD.get(("lua/myplug/global_api.lua", "unused_g")) == "likely",
      str(DEAD))
check("dead: never-required orphan module stays dead (mention floor -> review)",
      DEAD.get(("lua/myplug/orphan.lua", "orphan_fn")) == "review"
      and DEAD.get(("lua/myplug/orphan.lua", "orphan_two")) == "review",
      str(DEAD))
check("dead: dynamic require rides the mention floor (never likely)",
      DEAD.get(("lua/myplug/dyn_loader.lua", "load_mod")) == "review")

# edges exist on the wire
_out_load = g.edges.get("main.lua::load", set())
check("edge: LÖVE root -> plugin setup",
      "lua/myplug/init.lua::setup" in _out_load, str(sorted(_out_load)))
_out_upd = g.edges.get("main.lua::update", set())
check("edge: global-table call -> defining file",
      "lua/myplug/global_api.lua::ping" in _out_upd, str(sorted(_out_upd)))
_out_setup = g.edges.get("lua/myplug/init.lua::setup", set())
check("edge: require-bound util.log",
      "lua/myplug/util.lua::log" in _out_setup, str(sorted(_out_setup)))

# sabotage leg: revert the .lua registration, then assert this suite's own
# liveness floor — the assertion MUST fail in the sabotaged process,
# proving the lua rows flow through registry resolution (issue #256 law)
_sab = subprocess.run(
    [sys.executable, "-X", "utf8", "-c",
     "import os, sys\n"
     f"os.environ['NEURONAV_CONFIG'] = r'{CFG}'\n"
     "sys.path.insert(0, r'" + str(HERE) + "')\n"
     "import extractors\n"
     "extractors.EXTENSIONS.pop('.lua')\n"
     "extractors.BUILD_SEQUENCE = tuple(\n"
     "    s for s in extractors.BUILD_SEQUENCE\n"
     "    if getattr(s, '__module__', '') != 'extractors.lua')\n"
     "import graph\n"
     "g = graph.get_graph(rebuild=True)\n"
     "alive = g.dead_code(100000)['candidates']\n"
     "n = len([c for c in alive if c['path'].endswith('.lua')])\n"
     "assert n >= 3, f'lua dead rows {n} < 3 under sabotage'\n"],
    capture_output=True, text=True, cwd=str(HERE.parent), timeout=120)
_sab_last = (_sab.stdout + _sab.stderr).strip().splitlines()
check("sabotage: registry reverted -> liveness floor fails (teeth closed)",
      _sab.returncode != 0,
      _sab_last[-1] if _sab_last else f"rc={_sab.returncode}")

# determinism double-run
g2 = graph.get_graph(rebuild=True)
check("determinism: rebuild digest stable", digest(g) == digest(g2),
      f"{digest(g)[:12]} vs {digest(g2)[:12]}")

finish()
