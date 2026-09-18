"""C# hard-case suite (issue #336): the fixture battery.

Mirrors test_rusthard's harness: temp config rooted at tests/fixtures/csharp,
rebuild graph, dead_code tiers, edge assertions on the real graph, plus the
C#-specific pins — grammar wheel smoke (records/#if/patterns), partial-class
declaration merge, Unity MonoBehaviour lifecycle entry roots, [Test]/[Fact]
attribute entry hints, static-Main entry rule, namespace-qualified call
resolution, this-writes, property/field members, expression-bodied methods,
the enum-only wiring-only file, and the determinism double-run.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures" / "csharp"

from harness import finish, styled

check = styled("comma")  # byte pin: print-sep PASS lines

CFG = Path(tempfile.gettempdir()) / "neuronav_csharphard_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": FIX.as_posix(),
            "collection": "csharphard",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".cs"],
            "exclude_dirs": [],
        }
    )
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
sys.path.insert(0, str(HERE))

import graph  # noqa: E402  (needs NEURONAV_CONFIG set first)

try:
    from extractors import csharp as cx  # noqa: E402
except ImportError:  # pre-fix evidence runs: extractor not yet registered
    cx = None

check("csharp extractor registered (#336)", cx is not None,
      "extractors.csharp absent — .cs files ride raw-text fallback")
if cx is None:
    finish()
    raise SystemExit(1)


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
        for im in sorted(fs.imported_modules):
            h.update(f"I{im}".encode())
        for eh in sorted(fs.entry_hints):
            h.update(f"H{eh}".encode())
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


def tier(path: str, func: str):
    return DEAD.get((path, func))


# suite pin: grammar wheel smoke — the pinned tree-sitter-c-sharp imports
# and parses modern C# (records, pattern matching, #if preprocessor)
import tree_sitter_c_sharp as _tscs  # noqa: E402
from tree_sitter import Language, Parser  # noqa: E402

_smoke = Parser(Language(_tscs.language()))
check("wheel smoke: c# grammar parses record/pattern/#if",
      _smoke.parse(
          b"record Point(int X, int Y);\n"
          b"static int Pick(object o) => o switch { int i => i, _ => 0 };\n"
          b"#if DEBUG\nstatic void Dbg() {}\n#endif\n"
      ).root_node.has_error is False)

# fixture 1: Player.cs — partial MonoBehaviour + Unity lifecycle + [Test]
pf = g.files["Player.cs"]
check("f1 header: first type + primary base",
      pf.class_name == "Player" and pf.extends == "MonoBehaviour",
      f"{pf.class_name}:{pf.extends}")
check("f1 namespaces recorded", getattr(pf, "_csharp_namespaces", set()) == {"Game.Combat"},
      str(sorted(getattr(pf, "_csharp_namespaces", ()))))
check("f1 usings recorded as namespaces",
      "System" in pf.imported_modules
      and "UnityEngine" in pf.imported_modules
      and "System.Collections.Generic" in pf.imported_modules,
      str(sorted(pf.imported_modules)))
check("f1 partial merge: both declarations' methods in one surface",
      {"More", "Go", "Move", "TakesDamage"} <= set(pf.funcs),
      str(sorted(pf.funcs)))
check("f1 funcs surface (lifecycle, ctor, static helper, interface)",
      set(pf.funcs) == {"Player", "Awake", "Start", "Update", "OnEnable",
                        "OnDisable", "OnDestroy", "EditorOnly", "Move",
                        "Orphan", "TakesDamage", "Twice", "More", "Go",
                        "Tick", "Unused"},
      str(sorted(pf.funcs)))
check("f1 members: fields + property with types",
      pf.members.get("hp") == "int"
      and pf.members.get("names") == "List<string>"
      and pf.members.get("Health") == "int",
      str(dict(pf.members)))
check("f1 [Test] entry hint recorded", pf.entry_hints == {"TakesDamage"},
      str(sorted(pf.entry_hints)))
check("f1 unity flag set", getattr(pf, "_csharp_unity", False) is True)
check("f1 params + ret captured",
      pf.funcs["TakesDamage"].params == [("amount", "int")]
      and pf.funcs["TakesDamage"].ret == "void",
      f"{pf.funcs['TakesDamage'].params}:{pf.funcs['TakesDamage'].ret}")
check("f1 this-writes captured", pf.funcs["TakesDamage"].writes == {"hp"},
      str(sorted(pf.funcs["TakesDamage"].writes)))
check("f1 expression-bodied method body captured",
      "x * 2" in (pf.funcs["Twice"].body or ""), repr(pf.funcs["Twice"].body))
check("f1 ctor takes the class name", "Player" in pf.funcs
      and "hp = 100" in (pf.funcs["Player"].body or ""),
      repr(pf.funcs.get("Player").body if "Player" in pf.funcs else None))
check("f1 stat_tags sniffs (class Player : MonoBehaviour)",
      cx.stat_tags(pf.path and (FIX / "Player.cs").read_text(encoding="utf-8"))
      == ("Player", "MonoBehaviour"))

# fixture 2: Main.cs — static Main entry + static class header
mf = g.files["Main.cs"]
check("f2 static class header", mf.class_name == "Program" and mf.is_tool,
      f"{mf.class_name}:tool={mf.is_tool}")
check("f2 static Main rooted", "Main.cs::Main" in g.roots, str(sorted(g.roots)))
check("f2 dead: uncalled static likely-dead",
      tier("Main.cs", "DeadMain") == "likely",
      str([c for c in dead["candidates"] if c["path"] == "Main.cs"]))

# fixture 3: Services.cs — namespace-qualified call across namespaces
sf = g.files["Services.cs"]
check("f3 namespace-qualified call resolves",
      "Services.cs::Run" in g.edges
      and "Player.cs::Tick" in g.edges["Services.cs::Run"],
      str(g.edges.get("Services.cs::Run", ())))
check("f3 dead: uncalled method likely-dead",
      tier("Services.cs", "Dead") == "likely")

# edges: lifecycle + test + ctor call sites
check("edges: Update->Tick, Start->Twice, TakesDamage->Move, More->Move",
      g.edges.get("Player.cs::Update", set()) == {"Player.cs::Tick"}
      and "Player.cs::Twice" in g.edges.get("Player.cs::Start", ())
      and "Player.cs::Move" in g.edges.get("Player.cs::TakesDamage", ())
      and "Player.cs::Move" in g.edges.get("Player.cs::More", ()),
      str({k: sorted(v) for k, v in g.edges.items() if k.startswith("Player.cs::")
           and ("Update" in k or "Start" in k or "TakesDamage" in k or "More" in k)}))
check("edges: Main->Tick cross-file via type index",
      g.edges.get("Main.cs::Main", set()) == {"Player.cs::Tick"},
      str(g.edges.get("Main.cs::Main", ())))

# entry rules: Unity lifecycle set on unity files, [Test] hint, static Main
check("roots: Unity lifecycle methods on MonoBehaviour files",
      {"Player.cs::Awake", "Player.cs::Start", "Player.cs::Update",
       "Player.cs::OnEnable", "Player.cs::OnDisable", "Player.cs::OnDestroy"}
      <= g.roots, str(sorted(g.roots)))
check("roots: [Test]-attributed method", "Player.cs::TakesDamage" in g.roots)
check("roots: non-lifecycle non-test non-main NOT rooted",
      "Player.cs::Orphan" not in g.roots and "Main.cs::DeadMain" not in g.roots)
check("entry-exempt: Unity lifecycle names, nothing else",
      cx.is_entry_exempt("Update") and not cx.is_entry_exempt("TakesDamage"))

# dead tiers: exact fixture surface. Honest C# reality: an uncalled
# method is dead-likely even when it CALLS live code (More->Move),
# and a never-instantiated ctor / never-invoked public entry (Run)
# are no exception — v1 roots only Main/[Test]/Unity-lifecycle.
check("dead set exact (Orphan/EditorOnly/Go/More/ctor/Unused/DeadMain/Run/Dead)",
      set(DEAD) == {("Player.cs", "Orphan"), ("Player.cs", "EditorOnly"),
                    ("Player.cs", "Go"), ("Player.cs", "More"),
                    ("Player.cs", "Player"), ("Player.cs", "Unused"),
                    ("Main.cs", "DeadMain"), ("Services.cs", "Run"),
                    ("Services.cs", "Dead")},
      str(sorted(DEAD)))

# fixture 4: Enums.cs — no funcs, no members: wiring-only by the file shape
ef = g.files["Enums.cs"]
check("f4 enum-only file: wiring-only, counts in dead-share denominator",
      cx.is_wiring_only(ef) and cx.counts_dead_share(ef), str(sorted(ef.funcs)))

# determinism: rebuild produces the byte-identical graph
check("determinism: double-run digest identical",
      digest(g) == digest(graph.get_graph(rebuild=True)))

finish()
