"""Showcase placement probe: element showcase scenes must sit inside the
cluster their instancing wires feed, not in a name-similarity blob.

Manual probe (not a suite) — config-agnostic: discovers showcase-shaped
.tscn targets from whatever index the default config selects, so it never
hardcodes paths from a private target repo. Exits 0 with SKIP when the
index has no showcase scenes (e.g. the self-index)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph as _graph
import nav

STOP = {
    "showcase", "vfx", "scenes", "scene", "premium", "map", "effects",
    "rtx", "demo", "test", "show", "final", "sample",
}


def _element_tokens(path: str) -> set[str]:
    stem = Path(path).stem.lower()
    return {
        tok
        for tok in re.split(r"[_\-(\s]+", stem)
        if tok and tok not in STOP and not tok.isdigit()
    }


def main() -> int:
    g = _graph.get_graph()
    targets = sorted(
        p for p in g.files
        if p.endswith(".tscn") and "showcase" in Path(p).stem.lower()
    )
    if len(targets) < 2:
        print(f"SKIP: no showcase-shaped .tscn targets in this index ({len(targets)} found)")
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
        if c is None:
            checks.append((f"{Path(t).name} clustered", False))
            ok = False
            continue
        # acceptance: the scene's element vocabulary appears in its cluster
        # label (element showcases live in their element cluster)
        lab = c["label"].lower()
        hit = any(tok in lab for tok in _element_tokens(t))
        checks.append((f"{Path(t).name} label shares element token", hit))
        ok = ok and hit

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
