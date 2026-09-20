"""neuronav bake budget: the byte-cost spelling + greedy byte-cap keeper
(issues #86 phase 2, #369).

`_json_len` is the ONE compact-JSON cost measure every cap job measures
with (was restated per job — a stray separators change would silently
bias one cap against the others). `_cap_rows` moved VERBATIM from viz.py
(rung V2). Pure stdlib; the keep/drop decision depends only on priority
order and per-unit cost.
"""

import json


def _json_len(x) -> int:
    """Compact-JSON serialized length of x — the cap jobs' cost unit."""
    return len(json.dumps(x, separators=(",", ":")))


def _cap_rows(units, prio_key, cost_of, cap: int):
    """Greedy byte-budget keep over serializable units (spec §4 row 10).

    Walks units in deterministic priority order, keeps each while it
    still fits under cap; returns (kept units in walk order, units
    dropped). The keep/drop decision depends only on priority order and
    per-unit cost — callers own the arrangement (pair grouping, index
    order, dict rebuild)."""
    budget = cap
    kept = []
    for u in sorted(units, key=prio_key):
        cost = cost_of(u)
        if cost > budget:
            continue
        budget -= cost
        kept.append(u)
    return kept, len(units) - len(kept)
