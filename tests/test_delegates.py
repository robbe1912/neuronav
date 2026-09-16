# pure-delegate duplicate filter (issue #268) — hermetic fixture, no
# embeddings, build-only paths. Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_delegates.py
#
# Identical thin delegation wrappers (optional null guard or one simple
# assignment + a single forwarding call to a shared helper) are
# delegation, not duplicated logic — exact-duplicate grouping reports
# them as refactor targets by construction. Pins:
# - the classifier shapes (miss OK, invent never — the #116 law): thin
#   wrappers classify, guard-returns-a-call / call-in-assignment /
#   trailing-expression / brace-language bodies never do
# - same-name identical wrappers (the only shape that groups, because
#   _dup_groups hashes the body INCLUDING the signature line) drop
#   from exact_duplicates, with the drop counted
# - a genuine 6-line duplicated-logic group STAYS reported (control)
# - the predicate cache stores the FILTERED list + skip count at the
#   derive boundary (issue #71): cached serve, forced-fresh derive,
#   and the persisted predicates.json payload all agree
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile.mkdtemp(prefix="delg268-"))
PROJ = WORK / "proj"
(PROJ / "config").mkdir(parents=True)
(PROJ / "config" / "neuronav.json").write_text(
    json.dumps(
        {
            "root": "..",
            "collection": "delg268",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".py"],
            "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav"],
        }
    ),
    encoding="utf-8",
)

# fixture wiring:
#   w1._wrap == w2._wrap   (same-name 4-line thin delegate: null guard +
#                          single forwarding call -> must drop, counted)
#   g1.crunch == g2.crunch (genuine 6-line duplicated logic -> control,
#                          must STAY reported)
WRAP_BODY = '''def _wrap(v):
    if v is None:
        return None
    return _shared(v)
'''
CRUNCH_BODY = '''def crunch(nums):
    total = 0
    for n in nums:
        total += n * 2
    if total > 10:
        total -= 1
    return total
'''
(PROJ / "w1.py").write_text(WRAP_BODY, encoding="utf-8")
(PROJ / "w2.py").write_text(WRAP_BODY, encoding="utf-8")
(PROJ / "g1.py").write_text(CRUNCH_BODY, encoding="utf-8")
(PROJ / "g2.py").write_text(CRUNCH_BODY, encoding="utf-8")

os.environ["NEURONAV_CONFIG"] = str(PROJ / "config" / "neuronav.json")

import graph  # noqa: E402  (binds the fixture config)
import predicates  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def is_delegate(body: str) -> bool:
    return graph._pure_delegate(graph._normalize_body(body))


def main() -> int:
    # -- classifier shapes (#116 law: miss a wrapper, never flag logic) ----
    check("py wrapper with signature line classifies",
          is_delegate(WRAP_BODY))
    check("gd-style wrapper without signature line classifies",
          is_delegate("if v == null:\n\treturn null\nreturn _shared(v)\n"))
    check("assignment + forwarding call classifies",
          is_delegate("def fwd(x):\n    t = x or 0\n    return _shared(t)\n"))
    check("double guard chain (<=2 guards) classifies",
          is_delegate(
              "def pick(v, w):\n"
              "    if v is None:\n        return None\n"
              "    elif w is None:\n        return 0\n"
              "    return _shared(v)\n"
          ))
    check("guard returning a call stays (not a thin wrapper)",
          not is_delegate(
              "def wrap(v):\n"
              "    if v is None:\n        return _shared(None)\n"
              "    return _shared(v)\n"
          ))
    check("assignment carrying a call stays (real preprocessing)",
          not is_delegate(
              "def prep(x):\n    t = compute(x)\n    return render(t)\n"
          ))
    check("trailing expression after the forward stays",
          not is_delegate(
              "def wrap(v):\n    if v is None:\n        return None\n"
              "    return _shared(v) or 0\n"
          ))
    check("three guards stay (past the conservative cap)",
          not is_delegate(
              "def pick(a, b, c):\n"
              "    if a is None:\n        return None\n"
              "    elif b is None:\n        return 0\n"
              "    elif c is None:\n        return 1\n"
              "    return _shared(a)\n"
          ))
    check("brace-language body stays (semicolons never classify)",
          not is_delegate(
              "int wrap(int v) {\n"
              "  if (!v) return 0;\n"
              "  return shared(v);\n"
              "}\n"
          ))
    check("genuine 6-line duplicated logic stays",
          not is_delegate(CRUNCH_BODY))

    # -- fixture graph: filtered serve, honest count, control kept --------
    g = graph.get_graph(rebuild=True)
    groups = g.exact_duplicates()
    members = sorted(m for grp in groups for m in grp["members"])
    check("thin delegate pair drops from exact_duplicates (#268)",
          not any("_wrap" in m for m in members), str(members))
    check("genuine 6-line dup group stays reported (control)",
          any(set(grp["members"]) == {"g1.py::crunch", "g2.py::crunch"}
              for grp in groups),
          str(groups))
    report = g.duplicates_report()
    check("duplicates_report counts the skipped delegate group",
          report["delegate_skipped"] == 1
          and [m for grp in report["groups"] for m in grp["members"]]
          == ["g1.py::crunch", "g2.py::crunch"],
          str(report))

    # -- cache boundary (#71): cache stores the FILTERED list -------------
    cached = g._pred
    check("bind-time cache payload is already filtered + counted",
          cached["dup_skips"] == 1
          and not any("_wrap" in m for d in cached["dups"] for m in d["members"]),
          str(cached.get("dup_skips")))
    on_disk = json.loads(
        (predicates._state_dir() / predicates.NAME).read_text(encoding="utf-8")
    )["pred"]
    check("persisted predicates.json stores filtered dups + skip count",
          on_disk["dup_skips"] == 1
          and not any("_wrap" in m for d in on_disk["dups"] for m in d["members"]),
          str(on_disk.get("dup_skips")))

    # forced-fresh derive must agree byte-for-byte with the cached serve
    g._pred = None
    raw = predicates.derive(g)
    check("fresh derive == cached payload (filtered + counted)",
          raw["dups"] == cached["dups"] and raw["dup_skips"] == cached["dup_skips"],
          f"raw={raw.get('dup_skips')} cached={cached.get('dup_skips')}")
    check("fresh duplicates_report == cached duplicates_report",
          g.duplicates_report() == report)
    g._pred = cached

    # a second build serves the persisted cache — still filtered
    g2 = graph.get_graph(rebuild=True)
    check("rebuild serves the persisted (filtered) cache",
          g2.duplicates_report() == report, str(g2.duplicates_report()))

    print(f"{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


try:
    code = main()
finally:
    shutil.rmtree(WORK, ignore_errors=True)

raise SystemExit(code)
