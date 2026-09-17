# impact tool walk + rendering (issue #280) — synthetic graphs only, no
# index, no embeddings, no filesystem beyond the config read at import.
# Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_impact.py
#
# The walk (graph.impact) pins:
# - cycle safety: a 3-cycle in the closure terminates, the visited set
#   never re-enqueues, and the closure size is the node count (not a
#   path count)
# - diamonds count once: two paths from top to hub collapse to one
#   membership (union semantics, not path enumeration)
# - depth caps stay honest: max_depth clamps to [1, 8], totals reflect
#   only walked hops, and `beyond` counts the one-hop-past cut so a
#   capped answer can announce "+N more" instead of implying closure
# - direction asymmetry: callers closure != callees closure on the same
#   fixture; direction="callers"/"callees" only, anything else raises
#   ValueError with the valid options named
# - entry-boundary annotation: closure members that are roots surface
#   as `entries`; a callers closure with no roots says so
# - scene pseudo-keys (*::tscn) are neither counted nor traversed
# - determinism: same graph + reversed build order -> byte-identical
#   rendering (sorted frontier, no dict-order leakage)
# The rendering (server._impact_view) pins the #125 line law: full
# totals, per-hop histogram rows, "+N more" past 8 names, the depth-cap
# marker, seed capping for class symbols, and miss -> closest matches.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph  # noqa: E402
import server  # noqa: E402
from extractors.model import FileSym, Func  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")
    if not cond:
        FAILURES.append(name)


def add_file(g, path, names, ext=".py", class_name=""):
    fs = FileSym(path=path, ext=ext, class_name=class_name)
    for i, nm in enumerate(names):
        fs.funcs[nm] = Func(
            path=path, name=nm, line=i + 1,
            body=f"def {nm}(): pass", params=[("x", "int")],
        )
    g.files[path] = fs
    return fs


def wire(g, src_file, src_fn, dst_file, dst_fn):
    g._edge(f"{src_file}::{src_fn}", f"{dst_file}::{dst_fn}")


def build_ring(rev: bool = False):
    """Blast-radius fixture: a hub called through a 2-chain from two
    entry roots (test + registration), a 3-cycle feeding it, a diamond
    (top -> da/db -> hub), a side caller, an unanchored dead-end caller,
    and a 4-method class reached by one user. rev=True wires everything
    in reverse insertion order — the byte-determinism pin."""
    g = graph.Graph()
    files = [
        ("hub.py", ["hub"], ""),
        ("mid.py", ["m1", "m2"], ""),
        ("entry_test.py", ["te"], ""),
        ("entry_reg.py", ["rg"], ""),
        ("cyc.py", ["c1", "c2", "c3"], ""),
        ("dia.py", ["top", "da", "db"], ""),
        ("side.py", ["s"], ""),
        ("deadend.py", ["d"], ""),
        ("widget.py", ["w1", "w2", "w3", "w4"], "Widget"),
        ("user.py", ["u"], ""),
    ]
    wires = [
        ("mid.py", "m1", "mid.py", "m2"),
        ("mid.py", "m2", "hub.py", "hub"),
        ("entry_test.py", "te", "mid.py", "m2"),
        ("entry_reg.py", "rg", "mid.py", "m1"),
        ("side.py", "s", "mid.py", "m1"),
        ("cyc.py", "c1", "cyc.py", "c2"),
        ("cyc.py", "c2", "cyc.py", "c3"),
        ("cyc.py", "c3", "cyc.py", "c1"),
        ("cyc.py", "c1", "hub.py", "hub"),
        ("dia.py", "top", "dia.py", "da"),
        ("dia.py", "top", "dia.py", "db"),
        ("dia.py", "da", "hub.py", "hub"),
        ("dia.py", "db", "hub.py", "hub"),
        ("deadend.py", "d", "hub.py", "hub"),
        ("user.py", "u", "widget.py", "w1"),
    ]
    if rev:
        files = files[::-1]
        wires = wires[::-1]
    for path, names, cls in files:
        add_file(g, path, names, class_name=cls)
    for w in wires:
        wire(g, *w)
    # a scene pseudo-target on m2 (signal-wired method): never counted,
    # never traversed by the fn-level closure
    g._edge("mid.py::m2", "a.tscn::tscn")
    g.roots.update(("entry_test.py::te", "entry_reg.py::rg"))
    return g


def build_hubfarm():
    """12 one-fn files calling one hub — the '+N more' name-cap pin."""
    g = graph.Graph()
    add_file(g, "hubc.py", ["hubc"])
    for i in range(12):
        add_file(g, f"c{i:02d}.py", ["caller"])
        wire(g, f"c{i:02d}.py", "caller", "hubc.py", "hubc")
    return g


def main() -> int:
    g = build_ring()

    # -- full transitive closure, exact hops ---------------------------------
    res = g.impact("hub")
    check("callers: full closure total (12 = node count, not path count)",
          res["total"] == 12, f"total={res['total']}")
    check("callers: per-hop falloff 5/4/3",
          [len(h) for h in res["by_depth"]] == [5, 4, 3],
          str([len(h) for h in res["by_depth"]]))
    check("callers: hop 1 membership sorted",
          res["by_depth"][0] == [
              "cyc.py::c1", "deadend.py::d", "dia.py::da",
              "dia.py::db", "mid.py::m2",
          ], str(res["by_depth"][0]))
    check("callers: hop 2 membership sorted",
          res["by_depth"][1] == [
              "cyc.py::c3", "dia.py::top", "entry_test.py::te",
              "mid.py::m1",
          ], str(res["by_depth"][1]))
    check("callers: hop 3 closes the cycle without re-enqueueing",
          res["by_depth"][2] == [
              "cyc.py::c2", "entry_reg.py::rg", "side.py::s",
          ], str(res["by_depth"][2]))
    check("callers: closure completed inside the budget (beyond=0)",
          res["beyond"] == 0 and len(res["by_depth"]) < res["max_depth"])
    check("callers: seeds resolve exact and stay out of the total",
          res["seeds"] == ["hub.py::hub"]
          and all("hub.py::hub" not in h for h in res["by_depth"]))

    # cycle walked from inside: terminates, counts members once
    rc = g.impact("c2", max_depth=8)
    check("cycle: 3-cycle terminates with member-count closure",
          rc["total"] == 2 and rc["beyond"] == 0,
          f"total={rc['total']} beyond={rc['beyond']}")

    # diamond: top reaches hub via da AND db — one membership
    check("diamond: top counted once at hop 2",
          res["by_depth"][1].count("dia.py::top") == 1
          and res["total"] == 12)

    # -- depth caps -----------------------------------------------------------
    r1 = g.impact("hub", max_depth=1)
    check("cap: max_depth=1 walks one hop, counts the cut honestly",
          r1["total"] == 5 and r1["beyond"] == 4
          and r1["max_depth"] == 1,
          f"total={r1['total']} beyond={r1['beyond']}")
    r2 = g.impact("hub", max_depth=2)
    check("cap: max_depth=2 total 9, beyond counts hop 3",
          r2["total"] == 9 and r2["beyond"] == 3,
          f"total={r2['total']} beyond={r2['beyond']}")
    check("cap: clamp floor (0 -> 1)",
          g.impact("hub", max_depth=0)["max_depth"] == 1)
    r99 = g.impact("hub", max_depth=99)
    check("cap: clamp ceiling (99 -> IMPACT_MAX_DEPTH), same closure",
          r99["max_depth"] == graph.IMPACT_MAX_DEPTH == 8
          and r99["total"] == 12)

    # -- direction asymmetry ---------------------------------------------------
    check("direction: callees of the hub = 0 (it calls nothing)",
          g.impact("hub", direction="callees")["total"] == 0)
    ct = g.impact("te", direction="callees")
    check("direction: callees of te = m2 -> hub (2 hops, tscn skipped)",
          ct["total"] == 2
          and ct["by_depth"] == [["mid.py::m2"], ["hub.py::hub"]],
          str(ct["by_depth"]))
    check("direction: callers of te = 0 (it is a root, nothing calls it)",
          g.impact("te")["total"] == 0)
    try:
        g.impact("hub", direction="both")
        check("direction: bad direction raises ValueError", False)
    except ValueError as e:
        check("direction: bad direction raises ValueError naming options",
              "callers" in str(e) and "callees" in str(e), str(e))

    # scene pseudo-keys: never counted, never traversed
    cm2 = g.impact("m2", direction="callees")
    check("tscn: scene pseudo-key not in the fn-level closure",
          cm2["total"] == 1 and cm2["by_depth"] == [["hub.py::hub"]]
          and not any("tscn" in k for h in cm2["by_depth"] for k in h),
          str(cm2["by_depth"]))

    # -- entry-boundary annotation ---------------------------------------------
    check("entries: closure members that are roots surface, sorted",
          res["entries"] == ["entry_reg.py::rg", "entry_test.py::te"],
          str(res["entries"]))
    check("entries: capped walk reports only entries inside the cap",
          r2["entries"] == ["entry_test.py::te"], str(r2["entries"]))
    # user -> w1..w4 class seeds: one caller, no root behind it
    rw = g.impact("Widget")
    check("class symbol: all methods seed the walk",
          rw["seeds"] == [f"widget.py::w{i}" for i in (1, 2, 3, 4)]
          and rw["total"] == 1 and rw["entries"] == [],
          str(rw))

    # -- rendering (#125 line law) ----------------------------------------------
    v = server._impact_view(g, "hub", "callers", 4)
    check("render: header names symbol, seeds and direction",
          v.splitlines()[0] == "impact of hub (hub.py#hub): callers — what breaks",
          v.splitlines()[0])
    check("render: total row carries the full closure size",
          "total: 12 within 4 hops" in v)
    check("render: per-hop histogram rows with true counts",
          "    depth 1: 5 (cyc.py#c1, deadend.py#d, dia.py#da, dia.py#db, mid.py#m2)" in v
          and "    depth 2: 4 (" in v and "    depth 3: 3 (" in v)
    check("render: entries row is a count row",
          "    entries reached: 2 (entry_reg.py#rg, entry_test.py#te)" in v)
    v2 = server._impact_view(g, "hub", "callers", 2)
    check("render: depth cut announces +N past the cap",
          "… +3 more past the depth cap — pass max_depth=3 to expand" in v2,
          ", ".join(ln.strip() for ln in v2.splitlines() if "cap" in ln))
    vw = server._impact_view(g, "Widget", "callers", 4)
    check("render: class seeds cap at 3 with '+N more'",
          vw.splitlines()[0] == "impact of Widget (widget.py#w1, widget.py#w2, "
          "widget.py#w3 +1 more): callers — what breaks",
          vw.splitlines()[0])
    check("render: unanchored callers closure says so",
          "entries reached: 0 — no known entry" in vw,
          ", ".join(ln.strip() for ln in vw.splitlines() if "entries" in ln))
    check("render: empty closure stays honest, no fabricated rows",
          server._impact_view(g, "hub", "callees", 4)
          == "impact of hub (hub.py#hub): callees — what it depends on\n"
             "total: 0 within 4 hops")
    check("render: no entries row when the closure is empty",
          "entries" not in server._impact_view(g, "d", "callers", 4))

    hc = build_hubfarm()
    vh = server._impact_view(hc, "hubc", "callers", 4)
    check("render: 12-caller row caps at 8 names with '+4 more'",
          "    depth 1: 12 (c00.py#caller, c01.py#caller, c02.py#caller, "
          "c03.py#caller, c04.py#caller, c05.py#caller, c06.py#caller, "
          "c07.py#caller +4 more)" in vh
          and "total: 12 within 4 hops" in vh,
          vh.splitlines()[1:3])

    # -- miss -> closest matches -------------------------------------------------
    mv = server._impact_view(g, "hubbbb", "callers", 4)
    check("miss: suggestions, not a dead end",
          mv.startswith("no function matching 'hubbbb'")
          and "Closest matches: hub" in mv, mv)
    check("miss: graph.impact answers None",
          g.impact("hubbbb") is None)

    # -- determinism ---------------------------------------------------------------
    check("determinism: repeat call byte-identical",
          server._impact_view(g, "hub", "callers", 4) == v)
    check("determinism: reversed build order byte-identical",
          server._impact_view(build_ring(rev=True), "hub", "callers", 4) == v)
    check("determinism: payload repeats byte-identical",
          g.impact("hub") == res)

    # -- bare Graph.__new__ fixtures: read-path class defaults (#294) -------
    # _pred is already a class default so bare graphs can query; the
    # liveness read-path attributes (referenced / referenced_names /
    # reachable / autoloads) must be too — dead_code() raised
    # AttributeError on self.referenced for exactly this shape.
    bare = graph.Graph.__new__(graph.Graph)
    bare.files = {}
    empty = bare.dead_code()
    check("bare graph: dead_code() answers on empty files",
          empty["total"] == 0 and empty["candidates"] == [],
          f"total={empty['total']}")
    solo = graph.Graph.__new__(graph.Graph)
    solo.files = {}
    add_file(solo, "solo.py", ["only"])
    dc = solo.dead_code()
    check("bare graph: unreachable fn on a bare graph ranks dead",
          dc["total"] == 1
          and (dc["candidates"][0]["path"], dc["candidates"][0]["func"])
          == ("solo.py", "only"),
          str(dc["candidates"][:1]))
    check("bare graph: liveness read-path defaults are empty",
          not solo.referenced and not solo.referenced_names
          and not solo.reachable and solo.autoloads == {})
    try:
        solo.referenced.add("x")  # frozenset default: an accidental WRITE
        # on a bare graph must fail loudly, never leak across instances
        check("bare graph: accidental write fails loudly", False)
    except AttributeError:
        check("bare graph: accidental write fails loudly", True)
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
