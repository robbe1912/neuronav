"""Rust hard-case suite (issue #244): the fixture battery.

Mirrors test_tshard's harness: temp config rooted at tests/fixtures/rust,
rebuild graph, dead_code tiers, edge assertions on the real graph, plus
the Rust-specific pins — grammar wheel smoke, pub-mod API closure, pub use
re-export rebinding, trait default-method dispatch, test-attribute entry
rules (cfg(test) gates mark nothing), macro call-site recording, the
wiring-only barrel analogue, and the determinism double-run.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "rust"



from harness import finish, styled

check = styled("comma")  # byte pin: print-sep PASS lines

CFG = Path(tempfile.gettempdir()) / "neuronav_rusthard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "rusthard",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".rs"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

from extractors import rust as rx  # noqa: E402


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
        for mv in sorted(fs.module_vars):
            h.update(f"V{mv}={fs.module_vars[mv]}".encode())
        for eh in sorted(fs.entry_hints):
            h.update(f"H{eh}".encode())
        for nl in sorted(fs.name_literals):
            h.update(f"L{nl}".encode())
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


# suite pin: grammar wheel smoke — the pinned tree-sitter-rust imports and
# parses real-world Rust (impl blocks, ?, dyn, macros)
import tree_sitter_rust as _tsr  # noqa: E402
from tree_sitter import Language, Parser  # noqa: E402

_smoke = Parser(Language(_tsr.language()))
check("wheel smoke: rust grammar parses impl/try/dyn/macro",
      _smoke.parse(
          b"fn main() -> std::io::Result<()> { helper(1)?; Ok(()) }\n"
          b"fn helper(x: u32) -> Result<u32, Box<dyn std::error::Error>> "
          b"{ println!(\"{}\", x); Ok(x) }"
      ).root_node.has_error is False)

# fixture 1: lib.rs — crate root
lf = g.files["lib.rs"]
check("f1 funcs surface (pub api + private dead)",
      set(lf.funcs) == {"api_exported", "internal_dead"}, str(sorted(lf.funcs)))
check("f1 pub mod decls bind module_vars",
      set(lf.module_vars) == {"net", "methods", "traits", "helpers"}
      and lf.module_vars["net"] == "module:net.rs", str(dict(lf.module_vars)))
check("f1 pub use records the re-export binding",
      ("net.rs", "send") in lf.from_imports, str(sorted(lf.from_imports)))
check("f1 api_exported root via lib entry rule",
      "lib.rs::api_exported" in g.roots, str(sorted(g.roots)))
check("f1 internal_dead dead (review: mention floor)",
      tier("lib.rs", "internal_dead") == "review",
      str([c for c in dead["candidates"] if c["path"] == "lib.rs"]))

# fixture 2: net.rs — pub-mod closure roots both pub fns
nf = g.files["net.rs"]
check("f2 funcs surface", set(nf.funcs) == {"send", "recv", "internal_dead2"},
      str(sorted(nf.funcs)))
check("f2 pub fns rooted through lib.rs pub-mod closure",
      "net.rs::send" in g.roots and "net.rs::recv" in g.roots)
check("f2 private fn dead (likely)", tier("net.rs", "internal_dead2") == "likely")

# fixture 3: methods.rs — struct + inherent impl
mf = g.files["methods.rs"]
check("f3 class_name is the first impl-able type",
      mf.class_name == "Server" and mf.aliases.get("Server") == "struct",
      f"{mf.class_name!r} {dict(mf.aliases)}")
check("f3 struct fields as typed members",
      mf.members == {"name": "String", "seq": "u32"}, str(dict(mf.members)))
check("f3 new ret type", mf.funcs["new"].ret == "Server", mf.funcs["new"].ret)
check("f3 self param recorded",
      mf.funcs["compute"].params[0] == ("self", "&self"),
      str(mf.funcs["compute"].params))
check("f3 compute -> internal_step edge (self-method, same file)",
      "methods.rs::internal_step" in g.edges.get("methods.rs::compute", set()),
      str(sorted(g.edges.get("methods.rs::compute", set()))))

# fixture 4: traits.rs — trait sig + default method, impl collapse
tf = g.files["traits.rs"]
check("f4 funcs surface (trait default + impl merged)",
      set(tf.funcs) == {"handle", "label"}, str(sorted(tf.funcs)))
_handle_lines = [i + 1 for i, ln in enumerate(
    (FIX / "traits.rs").read_text(encoding="utf-8").splitlines())
    if ln.lstrip().startswith("fn handle")]
check("f4 handle line = FIRST declaration (trait sig, overload law)",
      tf.funcs["handle"].line == _handle_lines[0],
      f"{tf.funcs['handle'].line} vs {_handle_lines[0]}")
check("f4 handle body from the impl block",
      "self.label()" in tf.funcs["handle"].body, repr(tf.funcs["handle"].body))
check("f4 trait + struct in class_map",
      g.class_map.get("Handler") == "traits.rs"
      and g.class_map.get("Adapter") == "traits.rs", str(dict(g.class_map)))
check("f4 handle -> label edge",
      "traits.rs::label" in g.edges.get("traits.rs::handle", set()))

# fixture 5: main_entry.rs — paths, methods, dyn dispatch, alias rebind
ef = g.files["main_entry.rs"]
check("f5 main rooted", "main_entry.rs::main" in g.roots)
_main_edges = g.edges.get("main_entry.rs::main", set())
for _dst in ("net.rs::recv",                 # use crate::net + net::recv(
             "net.rs::send",                 # use crate::transmit (re-export alias)
             "helpers.rs::helper_alive",     # crate::helpers:: path call
             "methods.rs::new",              # Server::new assoc fn
             "methods.rs::compute",          # srv.compute(1) typed receiver
             "traits.rs::handle",            # Box<dyn Handler> receiver
             "traits.rs::label"):            # h.label() default method
    check(f"f5 main -> {_dst}", _dst in _main_edges, str(sorted(_main_edges)))
check("f5 transmit alias rebound to origin definer",
      ("net.rs", "send") in ef.from_imports
      and getattr(ef, "_rust_aliases", {}).get("transmit") == ("net.rs", "send"),
      f"{sorted(ef.from_imports)} {dict(getattr(ef, '_rust_aliases', {}))}")
check("f5 main ret type", ef.funcs["main"].ret == "std::io::Result<()>",
      ef.funcs["main"].ret)
check("f5 unused_local dead (review: dyn-hinted file)",
      tier("main_entry.rs", "unused_local") == "review")
check("f5 _underscore dead (review: shelved convention)",
      tier("main_entry.rs", "_shelved_helper") == "review")

# fixture 6: helpers.rs — pub fn without a pub mod chain stays dead
hf = g.files["helpers.rs"]
check("f6 pub-mod-gated helper alive via in-crate call",
      alive("helpers.rs", "helper_alive"))
check("f6 pub fn dead when the mod is not pub (teeth)",
      tier("helpers.rs", "helper_pub_dead") == "likely",
      str([c for c in dead["candidates"] if c["path"] == "helpers.rs"]))

# fixture 7: tests_entry.rs — attribute entry rules
tsf = g.files["tests_entry.rs"]
check("f7 test attrs mark exactly their fns (cfg gate marks nothing)",
      sorted(tsf.entry_hints) == ["checks_a", "checks_b", "deep_test",
                                  "ignored_stack_entry"],
      str(sorted(tsf.entry_hints)))
for _r in ("tests_entry.rs::checks_a", "tests_entry.rs::checks_b",
           "tests_entry.rs::deep_test", "tests_entry.rs::ignored_stack_entry"):
    check(f"f7 {_r} rooted", _r in g.roots)
check("f7 stacked attrs (#[tokio::test] + #[ignore]) still root their fn",
      "tests_entry.rs::ignored_stack_entry" in g.roots
      and alive("tests_entry.rs", "ignored_stack_entry"),
      str(sorted(tsf.entry_hints)))
check("f7 async tokio::test fn parsed",
      "checks_b" in tsf.funcs and tsf.funcs["checks_b"].ret == "")
check("f7 uncalled fn inside cfg(test) mod dead",
      tier("tests_entry.rs", "dead_in_test_mod") == "likely")
check("f7 cfg-mod helper alive via #[test] caller edge",
      alive("tests_entry.rs", "deep_helper")
      and "tests_entry.rs::deep_helper" in g.edges.get("tests_entry.rs::deep_test", set()))
check("f7 crate::macros:: path call resolves without a use",
      "macros.rs::macro_user" in g.edges.get("tests_entry.rs::checks_a", set()),
      str(sorted(g.edges.get("tests_entry.rs::checks_a", set()))))

# fixture 8: macros.rs — call-sites recorded, never expanded
mf8 = g.files["macros.rs"]
check("f8 non-std macro call-site recorded (macros.rs + main_entry.rs)",
      mf8.name_literals == {"helper_macro"}
      and g.files["main_entry.rs"].name_literals == {"helper_macro"},
      f"{sorted(mf8.name_literals)} / "
      f"{sorted(g.files['main_entry.rs'].name_literals)}")
check("f8 macro caller alive", alive("macros.rs", "macro_user"))
check("f8 template-referenced fn NOT expanded into an edge (dead, review)",
      tier("macros.rs", "template_only_fn") == "review"
      and not g.edges.get("macros.rs::macro_user", set()))
check("f8 plain dead fn (likely)", tier("macros.rs", "never_called_via_macro") == "likely")

# fixture 9: barrel_only.rs — re-export-only file is wiring
bf = g.files["barrel_only.rs"]
check("f9 barrel analogue is wiring-only",
      not bf.funcs and rx.is_wiring_only(bf)
      and ("net.rs", "send") in bf.from_imports,
      f"funcs={sorted(bf.funcs)} from_imports={sorted(bf.from_imports)}")
check("f9 wiring-only file contributes no dead rows",
      not [c for c in dead["candidates"] if c["path"] == "barrel_only.rs"])

# fixture 10: dead_helpers.rs — undeclared file, everything dead
check("f10 undeclared orphans all dead",
      all(tier("dead_helpers.rs", n) == "likely"
          for n in ("orphan_one", "orphan_two", "orphan_pub")),
      str([c for c in dead["candidates"] if c["path"] == "dead_helpers.rs"]))

# fixture 11: bin/cli.rs — cargo bin target is its OWN crate root (issue
# #284): crate:: heads anchor at bin/cli/, never the fixture-root lib.rs
cf = g.files["bin/cli.rs"]
check("f11 bin main rooted", "bin/cli.rs::main" in g.roots)
check("f11 use-crate alias binds inside the bin's own module tree",
      ("bin/cli/helper.rs", "shake") in cf.from_imports
      and getattr(cf, "_rust_aliases", {}).get("jolt") == ("bin/cli/helper.rs", "shake"),
      f"{sorted(cf.from_imports)} {dict(getattr(cf, '_rust_aliases', {}))}")
_main11 = g.edges.get("bin/cli.rs::main", set())
for _dst in ("bin/cli/helper.rs::shake",   # use crate::helper::shake as jolt
             "bin/cli/helper.rs::steady"):  # helper::steady via the mod decl
    check(f"f11 main -> {_dst}", _dst in _main11, str(sorted(_main11)))
check("f11 bin helper fns alive", alive("bin/cli/helper.rs", "shake")
      and alive("bin/cli/helper.rs", "steady"))
check("f11 pub fn in a bin stays dead-eligible (no lib closure over bins)",
      tier("bin/cli.rs", "bin_orphan") == "likely",
      str([c for c in dead["candidates"] if c["path"] == "bin/cli.rs"]))

# non-vacuity: the suite bites on real signal
check("non-vacuity: >=10 alive rust rows",
      sum(1 for rel, fs in g.files.items() if fs.ext == ".rs"
          for nm in fs.funcs if alive(rel, nm)) >= 10,
      str(sum(1 for rel, fs in g.files.items() if fs.ext == ".rs"
              for nm in fs.funcs if alive(rel, nm))))
check("non-vacuity: >=6 dead rust rows", len(DEAD) >= 6, str(len(DEAD)))
check("non-vacuity: both tiers present",
      {"likely", "review"} <= set(DEAD.values()), str(sorted(set(DEAD.values()))))
check("no entry-hinted fn in dead rows",
      not [c for c in dead["candidates"] if c["func"] in g.files[c["path"]].entry_hints])
check("no wiring-only file in dead rows",
      not [c for c in dead["candidates"] if rx.is_wiring_only(g.files[c["path"]])])

# dead-share hook pin (mirrors tshard): the flag drives bake/_dead_flags
# through registry resolution, not a hard-coded suffix list
check("counts_dead_share true for .rs", rx.counts_dead_share(lf))
check("counts_dead_share false for non-rust",
      not rx.counts_dead_share(type(lf)(path="x.py", ext=".py")))

# sabotage leg: revert the .rs registration, then assert this suite's own
# non-vacuity floor — the assertion MUST fail in the sabotaged process,
# proving the dead rows flow through registry resolution (issue #256 law)
_sab = subprocess.run(
    [sys.executable, "-X", "utf8", "-c",
     "import os, sys\n"
     f"os.environ['NEURONAV_CONFIG'] = r'{CFG}'\n"
     "sys.path.insert(0, r'" + str(HERE) + "')\n"
     "import extractors\n"
     "extractors.EXTENSIONS.pop('.rs')\n"
     "extractors.BUILD_SEQUENCE = tuple(\n"
     "    s for s in extractors.BUILD_SEQUENCE\n"
     "    if getattr(s, '__module__', '') != 'extractors.rust')\n"
     "import graph\n"
     "g = graph.get_graph(rebuild=True)\n"
     "dead = g.dead_code(100000)['candidates']\n"
     "n = len([c for c in dead if c['path'].endswith('.rs')])\n"
     "assert n >= 6, f'rust dead rows {n} < 6 under sabotage'\n"],
    capture_output=True, text=True, cwd=str(HERE.parent), timeout=120)
_sab_last = (_sab.stdout + _sab.stderr).strip().splitlines()
check("sabotage: registry reverted -> non-vacuity leg fails (teeth closed)",
      _sab.returncode != 0,
      _sab_last[-1] if _sab_last else f"rc={_sab.returncode}")

# determinism double-run
g2 = graph.get_graph(rebuild=True)
check("determinism: rebuild digest stable", digest(g) == digest(g2),
      f"{digest(g)[:12]} vs {digest(g2)[:12]}")

finish()
