"""neuronav bake budget: greedy byte-cap keeper (issue #86 phase 2).

`_cap_rows` moved VERBATIM from viz.py (rung V2). Pure stdlib; the
keep/drop decision depends only on priority order and per-unit cost.
"""

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
