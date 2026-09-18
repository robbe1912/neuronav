"""Go hard-case suite (issue #334): the fixture battery.

Mirrors test_rusthard's harness: temp config rooted at tests/fixtures/go,
rebuild graph, dead_code tiers, edge assertions on the real graph, plus
the Go-specific pins — grammar wheel smoke, go.mod module-prefix import
resolution (with the loud no-go.mod degrade), method receivers,
interface-satisfaction mirroring, _test.go entry rules (the rusthard
#[test] law), main-package entry, the String/Error std-interface shield,
func init roots, the sabotage teeth, and the determinism double-run.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "go"

from harness import finish, styled

check = styled("comma")  # byte pin: print-sep PASS lines

CFG = Path(tempfile.gettempdir()) / "neuronav_gohard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "gohard",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".go"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

from extractors import go as gx  # noqa: E402


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


# suite pin: grammar wheel smoke — the pinned pair loads and parses
import tree_sitter_go as _tsg  # noqa: E402  (the pinned wheel, #334)

_root = gx._PARSER.parse(b"package p\nfunc F() {}\n").root_node
check("grammar wheel: tree-sitter-go 0.25.0 parses a package",
      _root.has_error is False and b"function_declaration" in bytes(
          str(_root).encode()), str(type(_tsg)))

# ---- parse-level pins (hermetic: parse() only, no graph) ------------------------

_main_fs = gx.parse(FIX / "main.go", "main.go")
check("parse: package clause + main flag",
      _main_fs._go_package == "main" and _main_fs._go_is_main is True)
check("parse: funcs collected",
      {"main", "helper"} == set(_main_fs.funcs), str(sorted(_main_fs.funcs)))
check("parse: interface specs harvested (no spec Funcs minted)",
      _main_fs._go_interfaces.get("Reader") == ["Close", "Read"]
      and "Close" not in _main_fs.funcs, str(_main_fs._go_interfaces))

_store_fs = gx.parse(FIX / "store.go", "store.go")
check("parse: receiver methods keyed by type",
      _store_fs._go_method_types.get("Store") == {"Read", "Close", "Save"},
      str(_store_fs._go_method_types))
check("parse: struct fields + receiver env feed members",
      _store_fs.members.get("s") == "Store", str(sorted(_store_fs.members)))
check("parse: params/ret slices and tuples",
      _store_fs.funcs["Read"].params == [("p", "[]byte")]
      and _store_fs.funcs["Read"].ret == "error",
      f"{_store_fs.funcs['Read'].params} {_store_fs.funcs['Read'].ret}")

_test_fs = gx.parse(FIX / "util_test.go", "util_test.go")
check("parse: _test.go entry hints (Test*/Benchmark*/Fuzz*/Example*)",
      _test_fs._go_is_test and _test_fs.entry_hints == {"TestUtil"},
      str(sorted(_test_fs.entry_hints)))

# loud-degrade leg: a tree with no go.mod resolves nothing intra-module
_nd = Path(tempfile.mkdtemp()) / "solo.go"
(_nd.parent / "solo.go").write_text(
    'package a\n\nimport "example.com/other/lib"\n\nfunc F() { lib.X() }\n')
_solo = gx.parse(_nd, "solo.go")
check("parse: no go.mod degrades to all-external (loud note on stderr)",
      _solo.imported_modules == set()
      and _solo.module_vars.get("lib") == "external", str(_solo.module_vars))

# ---- graph-level pins ------------------------------------------------------------

g = graph.get_graph(rebuild=True)
dead = g.dead_code(100000)
DEAD = {(c["path"], c["func"]): c["tier"] for c in dead["candidates"]}


def alive(path: str, func: str) -> bool:
    return (path, func) not in DEAD


# entry rules: main package + test hints + func init
check("entry: package main roots main()",
      "main.go::main" in g.roots)
check("entry: _test.go funcs root (rusthard #[test] law)",
      "util_test.go::TestUtil" in g.roots)
check("entry: func init() always roots (implicit dispatch)",
      "store.go::init" in g.roots)

# reachability through every resolution arm
check("alive: same-file bare call", alive("main.go", "helper"))
check("alive: same-package cross-file bare call", alive("store.go", "OpenStore"))
check("alive: interface mirror revives satisfying methods",
      alive("store.go", "Read") and alive("store.go", "Close"))
check("alive: test helper via call edge", alive("util_test.go", "checkGold"))
check("alive: imported package exported API", alive("lib/help.go", "Help"))
check("shield: fmt.Stringer String() never dead",
      alive("orphan.go", "String"))

# the honest dead rows (review tier via the mention floor — the comment
# text cites each name beside its def)
check("dead: unexported uncalled lib fn",
      DEAD.get(("lib/help.go", "hidden")) == "review", str(DEAD))
check("dead: no-caller orphan fn",
      DEAD.get(("orphan.go", "forgotten")) == "review")
check("dead: same-package exported-but-uncalled stays eligible",
      DEAD.get(("store.go", "Save")) == "review")

# interface satisfaction is name-based corpus truth
check("satisfaction: Store covers Reader",
      getattr(g, "_go_satisfies", {}).get("Reader") == {"Store"},
      str(getattr(g, "_go_satisfies", {})))

# mirror edges exist on the wire (main -> store Read/Close, main -> lib Help)
_out = g.edges.get("main.go::main", set())
check("edge: interface call mirrors to the satisfier's file",
      "store.go::Read" in _out and "store.go::Close" in _out, str(sorted(_out)))
check("edge: package import call resolves dir-wide",
      "lib/help.go::Help" in _out)
check("edge: cross-file package call",
      "store.go::OpenStore" in _out)

# sabotage leg: revert the .go registration, then assert this suite's own
# dead-row floor — the assertion MUST fail in the sabotaged process,
# proving the dead rows flow through registry resolution (issue #256 law)
_sab = subprocess.run(
    [sys.executable, "-X", "utf8", "-c",
     "import os, sys\n"
     f"os.environ['NEURONAV_CONFIG'] = r'{CFG}'\n"
     "sys.path.insert(0, r'" + str(HERE) + "')\n"
     "import extractors\n"
     "extractors.EXTENSIONS.pop('.go')\n"
     "extractors.BUILD_SEQUENCE = tuple(\n"
     "    s for s in extractors.BUILD_SEQUENCE\n"
     "    if getattr(s, '__module__', '') != 'extractors.go')\n"
     "import graph\n"
     "g = graph.get_graph(rebuild=True)\n"
     "dead = g.dead_code(100000)['candidates']\n"
     "n = len([c for c in dead if c['path'].endswith('.go')])\n"
     "assert n >= 3, f'go dead rows {n} < 3 under sabotage'\n"],
    capture_output=True, text=True, cwd=str(HERE.parent), timeout=120)
_sab_last = (_sab.stdout + _sab.stderr).strip().splitlines()
check("sabotage: registry reverted -> dead-row floor fails (teeth closed)",
      _sab.returncode != 0,
      _sab_last[-1] if _sab_last else f"rc={_sab.returncode}")

# determinism double-run
g2 = graph.get_graph(rebuild=True)
check("determinism: rebuild digest stable", digest(g) == digest(g2),
      f"{digest(g)[:12]} vs {digest(g2)[:12]}")

finish()
