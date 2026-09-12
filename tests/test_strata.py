# strata layout QA — call-depth layering invariants, stdlib + numpy only.
# Run: python tests/test_strata.py   (exit 0 = all pass)
# Imports layout.py directly (phase-2 V1, issue #86): the five pure fns
# moved out of viz.py and layout has no nav/chroma edge, so the test
# exercises the real shipped module. The old AST-extraction + {"os","sys"}
# whitelist law is deleted with that move.
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def load_viz_funcs():
    sys.path.insert(0, str(ROOT))
    import layout
    names = ("_links_adj", "_tarjan_scc", "_strata_depths",
             "_strata_analysis", "_layout")
    missing = [n for n in names if not hasattr(layout, n)]
    assert not missing, f"layout.py lost: {missing}"
    return {n: getattr(layout, n) for n in names}


def build_fixture():
    """32 files, directed links: two entries, a diamond, a 3-cycle, a long
    chain, a deep back-edge (27 -> 4) and inst/attach composition edges."""
    links = []

    def L(s, t, w=2, ty="call"):
        links.append({"s": s, "t": t, "w": w, "ty": ty})

    L(0, 2); L(0, 3)          # diamond top (0 = entry)
    L(2, 5); L(3, 5)          # diamond joins at 5
    L(1, 4)                   # 1 = second entry
    L(4, 6); L(6, 7)          # short chain
    L(5, 8)
    L(8, 9); L(9, 10); L(10, 8)   # cycle: SCC {8,9,10}
    L(9, 11); L(11, 12)
    L(1, 13, ty="inst"); L(13, 14, ty="attach")   # composition island
    L(14, 10)                # feeds the cycle from the island
    prev = 12
    for k in range(15, 25):   # chain for depth spread
        L(prev, k)
        prev = k
    for k in range(25, 32):   # leaves under the diamond join
        L(5, k)
    L(25, 7)                 # back-edge: deep leaf into the shallow chain
    L(27, 4)
    return links


def main():
    V = load_viz_funcs()

    # 0. layout.py stays stdlib(+numpy)-only: no nav/graph/chroma import
    #    may creep back into the pure module (phase-2 V1 law, issue #86)
    import ast as _ast

    mods = set()
    for _n in _ast.parse((ROOT / "layout.py").read_text(encoding="utf-8")).body:
        if isinstance(_n, _ast.Import):
            mods.update(a.name.split(".")[0] for a in _n.names)
        elif isinstance(_n, _ast.ImportFrom) and _n.module:
            mods.add(_n.module.split(".")[0])
    check("layout imports stdlib+numpy only", mods <= {"os", "sys", "numpy"},
          str(sorted(mods)))

    depths_fn, layout_fn = V["_strata_depths"], V["_layout"]
    N = 32
    links = build_fixture()

    # 1. depth monotonicity: every link depth[t] >= depth[s] (same-SCC edges
    # are equal; cross-SCC edges strictly increase under longest-path layering)
    d = depths_fn(N, links)
    bad = [(l["s"], l["t"]) for l in links if d[l["t"]] < d[l["s"]]]
    check("link depth monotone", not bad, str(bad[:4]))

    # 2. entry nodes (in-degree 0) sit at depth 0
    has_in = {l["t"] for l in links}
    entries = [i for i in range(N) if i not in has_in]
    check("entries exist", len(entries) >= 2, str(entries))
    check("entry depth 0", all(d[i] == 0 for i in entries),
          str([(i, d[i]) for i in entries]))

    # 3. diamond: 0 -> {2,3} -> 5 layers 0/1/2
    check("diamond layers", d[2] == 1 and d[3] == 1 and d[5] == 2,
          f"2:{d[2]} 3:{d[3]} 5:{d[5]}")

    # 4. cycle collapsed: SCC members share one depth
    check("cycle shares depth", d[8] == d[9] == d[10],
          f"8:{d[8]} 9:{d[9]} 10:{d[10]}")

    # 5. list-form links (same shape as the raw extractor rows) agree
    list_links = [[l["s"], l["t"], l["w"], l["ty"]] for l in links]
    check("list-form links agree", depths_fn(N, list_links) == d)

    # 6. real layout build: deterministic, finite, no-overlap, stratified Y
    clusters = [i % 3 for i in range(N)]
    p1 = layout_fn(N, links, [], clusters)
    p2 = layout_fn(N, links, [], clusters)
    h1, h2 = json.dumps(p1), json.dumps(p2)
    check("two builds byte-identical", h1 == h2,
          f"sha256 {hashlib.sha256(h1.encode()).hexdigest()[:12]}"
          f" == {hashlib.sha256(h2.encode()).hexdigest()[:12]}")
    flat = [x for p in p1 for x in p]
    check("no NaN/inf", all(math.isfinite(x) for x in flat))

    # same size chain as viz.py: rad = min(10, 3.5+sqrt(deg)) * 1.1, floors
    # at (rad_i + rad_j) * 1.7 — final positions must clear it in 3D
    deg = [0.0] * N
    for l in links:
        deg[l["s"]] += l["w"]
        deg[l["t"]] += l["w"]
    rad = [min(10.0, 3.5 + math.sqrt(x)) * 1.1 for x in deg]
    worst = math.inf
    for i in range(N):
        for j in range(i + 1, N):
            dx = p1[i][0] - p1[j][0]
            dy = p1[i][1] - p1[j][1]
            dz = p1[i][2] - p1[j][2]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            worst = min(worst, dist - (rad[i] + rad[j]) * 1.7)
    check("min separation >= radii-sum*1.7", worst >= 0, f"worst margin {worst:.2f}")

    # 7. reading order visible: entries above the deepest layer (Y-up)
    top = [p1[i][1] for i in entries]
    dmax = max(d)
    deep = [p1[i][1] for i in range(N) if d[i] == dmax]
    check("entries above deepest layer", min(top) > max(deep),
          f"min entry Y {min(top):.0f} vs max deep Y {max(deep):.0f}")

    print(f"\n{N} nodes · {len(links)} links · {len(FAILURES)} failure(s)")
    if FAILURES:
        print("FAILED:", ", ".join(FAILURES))
        sys.exit(1)
    print("ALL TESTS PASS")


if __name__ == "__main__":
    main()
