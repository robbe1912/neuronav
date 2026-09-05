"""Showcase placement probe: element showcase scenes must sit inside the
cluster their instancing wires feed, not in a name-similarity blob."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clusters as _clusters
import graph as _graph
import nav

TARGETS = [
    "VFX/showcase_earth_effects.tscn",
    "VFX/Blood_showcase_map_(premium).tscn",
    "VFX/Fire_showcase_(premium).tscn",
    "VFX/Fire_showcase_RTX(premium).tscn",
    "scenes/VFX_Preload.tscn",
]

def main() -> int:
    cs = nav.clusters()
    of = {}
    for c in cs:
        for p, _cls in c["paths"]:
            of[p] = c
    print(f"clusters: {len(cs)}, largest: {max(c['size'] for c in cs)}")
    ok = True
    for t in TARGETS:
        c = of.get(t)
        label = f"{c['label']}({c['id']})" if c else "MISSING"
        print(f"  {t}  ->  {label} (conf {c.get('confidence') if c else '-'})")
    # acceptance: element showcases live in their element cluster
    checks = [
        ("showcase_earth in Earth-family", "earth" in of[TARGETS[0]]["label"].lower()),
        ("Blood_showcase in Blood-family", "blood" in of[TARGETS[1]]["label"].lower()),
        ("Fire_showcase in Fire-family", "fire" in of[TARGETS[2]]["label"].lower()),
        ("Fire_showcase_RTX in Fire-family", "fire" in of[TARGETS[3]]["label"].lower()),
    ]
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
