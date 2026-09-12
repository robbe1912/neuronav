# bake/fnio — pure per-job transforms for viz._build_data (issue #86
# phase-2 V8). Moved verbatim from viz.py; every nav/graph/chroma edge
# stays in the viz.py orchestrator — data arrives as arguments.

import json

from bake.budget import _cap_rows

def _fn_io(g):
    """J15: per-function IO surface keyed "path::func"."""
    # per-function IO surface (params / ret / member writes / mutated params),
    # keyed "path::func". consumed by the fn click panel (signature line +
    # write chips), the focus-label writes-state badge and the mutators
    # filter. skipped entirely when a function has nothing to say.
    fio: dict[str, dict] = {}
    for rel, fs in g.files.items():
        for fn in fs.funcs.values():
            if not (fn.params or fn.ret or fn.writes or fn.mut_params):
                continue
            sig = ", ".join(f"{p}: {t}" if t else p for p, t in fn.params)
            fio[f"{rel}::{fn.name}"] = {
                "sig": f"{fn.name}({sig})",
                "ret": fn.ret,
                "w": sorted(fn.writes),
                "mp": sorted(fn.mut_params),
            }
    return fio


def _cap_fnio(fio, rank_of):
    """J16: fio byte budget — survives by file pagerank, ties by key."""
    # fio byte budget (spec §4 row 10): per-fn IO signatures carry C++
    # type strings (200-300 B/row at engine scale). Below the cap nothing
    # changes; above it entries survive by pagerank of their file (ties by
    # key), so hover IO stays richest on the files that matter.
    fio_dropped = 0
    _FIO_BYTE_CAP = 3_000_000
    if len(json.dumps(fio, separators=(",", ":"))) > _FIO_BYTE_CAP:
        kept_units, _ = _cap_rows(
            list(fio.items()),
            prio_key=lambda kv: (
                -rank_of(kv[0].split("::", 1)[0]), kv[0],
            ),
            cost_of=lambda kv: len(json.dumps([kv[0], kv[1]], separators=(",", ":"))) + 1,
            cap=_FIO_BYTE_CAP,
        )
        fio_dropped = len(fio) - len(kept_units)
        fio = dict(kept_units)
    return fio, fio_dropped
