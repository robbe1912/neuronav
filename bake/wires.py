# bake/wires — pure per-job transforms for viz._build_data (issue #86
# phase-2 V8). Moved verbatim from viz.py; every nav/graph/chroma edge
# stays in the viz.py orchestrator — data arrives as arguments.

import json

from bake.budget import _cap_rows

def _emit_wire_rows(g, idx):
    """J5: fedges + call/var mwires — ONE iteration emits BOTH exports
    (map-spec-v2 §0) so the two schemas can never drift."""
    # function-level call edges: [src_file_idx, src_fn, dst_file_idx, dst_fn, line]
    # mwires: named-wire map rows [ty, sf, sfn, df, dfn, line, extra]
    # (map-spec-v2 §0). call rows ride the exact fedges filters — emitted
    # in the same iteration so the two exports cannot drift — while the
    # ::VAR: pseudo-node dsts fedges skips are harvested as member wires.
    fedges: list[list] = []
    mwires: list[list] = []
    for src_key, dsts in g.edges.items():
        if src_key.endswith("::tscn"):  # pseudo source, fn would be "tscn"
            continue
        if "::" not in src_key:
            # file-level source (cpp v1.1 header-scope refs) — no fn to
            # attribute; its file adjacency already rides the links layer
            continue
        s_path, s_fn = src_key.split("::", 1)
        if s_path not in idx:
            continue
        src_fs = g.files.get(s_path)
        s_line = src_fs.funcs[s_fn].line if src_fs and s_fn in src_fs.funcs else 0
        for dst_key in dsts:
            if "::VAR:" in dst_key:
                # member wire — dst file owns the member; intra-file
                # skipped like calls (intra-file wires: spec §11 parking lot)
                d_path, member = dst_key.split("::VAR:", 1)
                if d_path in idx and d_path != s_path:
                    mwires.append(
                        ["var", idx[s_path], s_fn, idx[d_path], member, s_line, None]
                    )
                continue
            if (
                dst_key.endswith("::tscn")
                or "::SIGNAL:" in dst_key
            ):
                continue
            if "::" not in dst_key:
                # fn -> whole-file edge (cpp v1.1 template/instantiation
                # refs resolve to the target's file, not a fn): file-level
                # ink comes from the links layer; the fn layer skips it
                continue
            d_path, d_fn = dst_key.split("::", 1)
            if d_path not in idx or d_path == s_path:
                continue
            fedges.append([idx[s_path], s_fn, idx[d_path], d_fn, s_line])
            mwires.append(
                ["call", idx[s_path], s_fn, idx[d_path], d_fn, s_line, None]
            )

    # canonical row order: g.edges values are sets (PYTHONHASHSEED varies
    # their iteration order across processes) — export paths must never
    # leak set order; mirrors the deterministic named-wire sort below
    fedges.sort(key=lambda e: (e[0], e[1], e[2], e[3], e[4]))
    return fedges, mwires


def _signal_wires(g, idx):
    """J6: signal mwires rows + resolution counters (rows join the single
    post-concat sort in _build_data)."""
    # signal wires: scene connections resolved against the scene's script
    # ext_resources via graph.script_rels — the same cascade _wire_tscn
    # wires edges from, so the corridor channel can never drift from the
    # graph. A connection resolving in N scripts yields N rows; one
    # resolving in none counts into meta.sig_unresolved — the anonymous
    # amber corridor channel (map-spec-v2 §1/F13).
    rows: list[list] = []
    sig_resolved = 0
    sig_unresolved = 0
    for rel, fs in g.files.items():
        if fs.ext != ".tscn" or rel not in idx:
            continue
        script_rels = g.script_rels(fs)
        for sig_name, handler in fs.connections:
            hit = [
                s_rel
                for s_rel in script_rels
                if handler in g.files[s_rel].funcs and s_rel in idx
            ]
            if hit:
                sig_resolved += 1
                for s_rel in hit:
                    rows.append(
                        ["signal", idx[rel], sig_name, idx[s_rel], handler, 0, None]
                    )
            else:
                sig_unresolved += 1
    return rows, sig_resolved, sig_unresolved


def _wire_budget(fedges, mwires, paths, rank_of):
    """J7: engine-scale wire byte budget — whole FILE PAIRS kept by
    pagerank priority (rank_of), re-emitted in ascending original order."""
    # engine-scale export budget (spec §4 row 10): named-wire rows grow
    # ~12/file and would push the engine bake past the bootable-html size.
    # Below the byte cap nothing changes (self-index/game-target bake identical);
    # above it, whole FILE PAIRS are kept by pagerank priority — call rows
    # and their fedges mirrors share a pair, so the two exports stay
    # consistent — until the budget is spent. Deterministic: fixed sort
    # keys, whole-pair keeps, original emission order preserved.
    wire_dropped = 0
    _WIRE_BYTE_CAP = 2_600_000
    if (len(json.dumps(fedges, separators=(",", ":")))
            + len(json.dumps(mwires, separators=(",", ":"))) > _WIRE_BYTE_CAP):
        groups: dict[tuple, list] = {}
        for i, r in enumerate(fedges):
            groups.setdefault((r[0], r[2]), [[], []])[0].append(i)
        for i, r in enumerate(mwires):
            groups.setdefault((r[1], r[3]), [[], []])[1].append(i)

        def _pair_cost(pair) -> int:
            fe, mw = groups[pair]
            # +1 per row: the joining comma each kept row adds to the
            # serialized list (caps are enforced on the real bake bytes)
            return (sum(len(json.dumps(fedges[i], separators=(",", ":"))) + 1 for i in fe)
                    + sum(len(json.dumps(mwires[i], separators=(",", ":"))) + 1 for i in mw))

        kept_pairs, _pairs_dropped = _cap_rows(
            list(groups),
            prio_key=lambda p: (
                -rank_of(paths[p[0]]) - rank_of(paths[p[1]]),
                paths[p[0]], paths[p[1]],
            ),
            cost_of=_pair_cost,
            cap=_WIRE_BYTE_CAP,
        )
        keep_fe: list[int] = []
        keep_mw: list[int] = []
        for pair in kept_pairs:
            keep_fe.extend(groups[pair][0])
            keep_mw.extend(groups[pair][1])
        wire_dropped = (len(fedges) - len(keep_fe)) + (len(mwires) - len(keep_mw))
        fedges = [fedges[i] for i in sorted(keep_fe)]
        mwires = [mwires[i] for i in sorted(keep_mw)]
    return fedges, mwires, wire_dropped


def _fn_roster(g, paths):
    """J8: complete per-file fn roster [name, line], line order (spec §0)."""
    fns: dict[str, list[list]] = {}
    for p in paths:
        fs = g.files.get(p)
        if fs and fs.funcs:
            fns[p] = [
                [fn.name, fn.line]
                for fn in sorted(fs.funcs.values(), key=lambda fn: (fn.line, fn.name))
            ]
    return fns
