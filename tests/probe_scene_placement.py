"""Scene placement probe: heavily-instanced scene files must sit inside the
cluster their instancing wires feed, not in a name-similarity blob.

Manual probe (not a suite) — config-agnostic: picks the top .tscn targets
by inbound edge count from whatever index the default config selects, so it
never hardcodes paths or naming conventions from a private target repo.
Exits 0 with SKIP when the index has no such scenes (e.g. the self-index)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph as _graph
import nav

GENERIC = {
    "vfx", "scenes", "scene", "map", "effects", "demo", "test", "show",
    "final", "sample", "main", "core", "ui", "utils",
}


def _vocab(path: str) -> set[str]:
    stem = Path(path).stem.lower()
    return {
        tok
        for tok in re.split(r"[_\-(\s]+", stem)
        if tok and tok not in GENERIC and not tok.isdigit()
    }


def main() -> int:
    g = _graph.get_graph()
    # scene-shaped files ranked by inbound wire count (instancing = fan-in)
    w = g.file_wires()
    inbound: dict[str, int] = {}
    for dsts in w.values():
        for q, n in dsts.items():
            inbound[q] = inbound.get(q, 0) + n
    scenes = sorted(
        ((n, p) for p, n in inbound.items() if p.endswith(".tscn") and n > 1),
        reverse=True,
    )
    targets = [p for _n, p in scenes[:4]]
    if len(targets) < 2:
        print(f"SKIP: no heavily-instanced scene targets in this index ({len(targets)} found)")
        return 0
    cs = nav.clusters()
    of = {}
    for c in cs:
        for p, _cls in c["paths"]:
            of[p] = c
    print(f"clusters: {len(cs)}, largest: {max(c['size'] for c in cs)}")

    ok = True
    checks = []
    for t in targets:
        c = of.get(t)
        label = f"{c['label']}({c['id']})" if c else "MISSING"
        print(f"  {t}  ->  {label} (conf {c.get('confidence') if c else '-'})")
        instancers = [
            of[s] for s, m in w.items() if t in m for _ in range(m[t])
            if s in of
        ]
        want = None
        if instancers:
            counts: dict[str, int] = {}
            for c2 in instancers:
                counts[c2["id"]] = counts.get(c2["id"], 0) + 1
            want = max(sorted(counts), key=lambda k: counts[k])
        want_lab = next(
            (c2["label"] for c2 in cs if c2["id"] == want), "?"
        ) if want else "-"
        if c is None:
            checks.append((f"{Path(t).name} clustered", False))
            ok = False
            continue
        # acceptance: the scene sits in the cluster holding the plurality
        # of its instancing wires (a file lives with what instantiates it).
        # Cross-cutting utility scenes may fail this — that is the signal.
        good = want is None or c["id"] == want
        checks.append(
            (f"{Path(t).name} with instancer plurality ({want_lab})", good))
        ok = ok and good

    # determinism: two runs must produce identical membership
    cs2 = nav.clusters()
    m1 = {p: c["id"] for c in cs for p, _ in c["paths"]}
    m2 = {p: c["id"] for c in cs2 for p, _ in c["paths"]}
    checks.append(("deterministic across runs", m1 == m2))
    for name, cond in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
