"""C++ hard-case suite (issue #13, spec §5): the 9-fixture battery.

Mirrors test_pyhard's harness: temp config rooted at tests/fixtures/cpp,
rebuild graph, dead_code tiers, alive()/stays_dead() in both directions,
determinism double-run, and the ClassDB-bound-never-dead meta-invariant.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "cpp"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


CFG = Path(tempfile.gettempdir()) / "neuronav_cpphard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "cpphard",
            "include_dirs": ["."],
            "extensions": [".h", ".cpp"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

from extractors.cpp import CPP_EXTS, harvest_registration  # noqa: E402


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
        for s in sorted(fs.signals):
            h.update(f"S{s}".encode())
        for m in sorted(fs.members):
            h.update(f"M{m}={fs.members[m]}".encode())
        for c in sorted(fs.consts):
            h.update(f"C{c}".encode())
        for i in sorted(fs.imported_modules):
            h.update(f"I{i}".encode())
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


# fixture 1: gdclass_macro.h
check("f1 refresh alive (ClassDB-bound)", alive("gdclass_macro.h", "refresh"))
check("f1 _bind_methods alive (frozen virtual)", alive("gdclass_macro.h", "_bind_methods"))
check("f1 unused_private_helper dead", tier("gdclass_macro.h", "unused_private_helper") == "review",
      str(tier("gdclass_macro.h", "unused_private_helper")))

# fixture 2: pair_class.h + pair_class.cpp
check("f2 pairing: .cpp class_name from header", g.files["pair_class.cpp"].class_name == "PairClass",
      g.files["pair_class.cpp"].class_name)
check("f2 pairing: header owns class_map", g.class_map.get("PairClass") == "pair_class.h")
check("f2 qualified def harvested", "combine" in g.files["pair_class.cpp"].funcs)
check("f2 combine alive (bound)", alive("pair_class.cpp", "combine"))
check("f2 registration edge", "pair_class.cpp::combine" in g.edges.get("pair_class.cpp::_bind_methods", set()),
      str(sorted(g.edges.get("pair_class.cpp::_bind_methods", set()))))

# fixture 3: template_container.h
tf = g.files["template_container.h"].funcs
check("f3 template methods extracted", {"push_back", "get", "unused_tplate_helper"} <= set(tf), sorted(tf))
check("f3 unused_tplate_helper dead", tier("template_container.h", "unused_tplate_helper") == "likely",
      str(tier("template_container.h", "unused_tplate_helper")))

# fixture 4: ifdef_platforms.cpp
ff = g.files["ifdef_platforms.cpp"].funcs
check("f4 both #ifdef branches", {"tools_only_calc", "runtime_only_calc"} <= set(ff), sorted(ff))
check("f4 unused_platform_helper dead", tier("ifdef_platforms.cpp", "unused_platform_helper") == "likely")

# fixture 5: signals_props.cpp
sp = g.files["signals_props.cpp"]
check("f5 ADD_SIGNAL -> signals", "value_changed" in sp.signals, sorted(sp.signals))
check("f5 ADD_PROPERTY -> member", "text" in sp.members, sorted(sp.members))
check("f5 BIND_ENUM_CONSTANT -> consts", {"MODE_FAST", "MODE_SLOW"} <= set(sp.consts), sorted(sp.consts))
check("f5 setter alive", alive("signals_props.cpp", "set_text"))
check("f5 getter alive", alive("signals_props.cpp", "get_text"))
check("f5 accessor edges", {"signals_props.cpp::set_text", "signals_props.cpp::get_text"}
      <= g.edges.get("signals_props.cpp::_bind_methods", set()),
      str(sorted(g.edges.get("signals_props.cpp::_bind_methods", set()))))

# fixture 6: virtuals_override.cpp (+ provider.h GDVIRTUAL)
check("f6 _notification alive (CPP_VIRTUALS)", alive("virtuals_override.cpp", "_notification"))
check("f6 _ready_like in repo GDVIRTUAL set", "_ready_like" in g.cpp_gdvirtuals, sorted(g.cpp_gdvirtuals))
check("f6 _ready_like alive (repo-set override)", alive("virtuals_override.cpp", "_ready_like"))
check("f6 unused_virtual_helper dead", tier("virtuals_override.cpp", "unused_virtual_helper") == "likely")

# fixture 7: forward_decls.h
fw = g.files["forward_decls.h"]
check("f7 quoted include exact", fw.imported_modules == {"provider.h"}, sorted(fw.imported_modules))
check("f7 forward decl -> no class_map entry", "Used" not in g.class_map)
check("f7 real class mapped", g.class_map.get("Provider") == "provider.h")

# fixture 8: name_literals.cpp
check("f8 state_changed alive (name literal)", alive("name_literals.cpp", "state_changed"))
check("f8 unused_literal_helper dead (review: emit_signal file)",
      tier("name_literals.cpp", "unused_literal_helper") == "review",
      str(tier("name_literals.cpp", "unused_literal_helper")))

# fixture 9: dead_control.cpp
check("f9 orphan_calc dead likely", tier("dead_control.cpp", "orphan_calc") == "likely")
check("f9 unused_plain_helper dead likely", tier("dead_control.cpp", "unused_plain_helper") == "likely")

# provider.h bound method
check("provider provide_value alive", alive("provider.h", "provide_value"))

# non-vacuity: both directions populated
check("alive direction non-vacuous", sum(1 for rel, fs in g.files.items() if fs.ext in CPP_EXTS
      for n in fs.funcs if alive(rel, n)) >= 10)
check("dead direction non-vacuous", len(DEAD) >= 6, str(len(DEAD)))

# meta-invariant: a ClassDB-bound method is never a dead candidate
bound_names = set()
for rel, fs in g.files.items():
    if fs.ext not in CPP_EXTS:
        continue
    for b in harvest_registration((FIX / rel).read_text(encoding="utf-8", errors="replace"))["binds"]:
        bound_names.add((rel, b.method))
        bound_names.add((g.class_map.get(b.cls, ""), b.method))
bad_bound = [(p, f, t) for (p, f), t in DEAD.items() if (p, f) in bound_names]
check("meta: no bound method in dead candidates", not bad_bound, str(bad_bound))

# determinism: same data -> same bytes (two rebuilds, seeded-independent
# because the C++ path has no rng; proves ordered-capture stability)
g2 = graph.get_graph(rebuild=True)
check("determinism double-run digest", digest(g) == digest(g2))

print()
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
