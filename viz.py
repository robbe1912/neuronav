"""neuronav viz: self-contained force-directed 3D graph of the indexed repo.

Generates `graph.html` (single file; vendored three.js is embedded as
data: URIs, so it boots offline from file:// — zero network deps).
Nodes = indexed files colored by semantic cluster; red-mixed nodes contain
dead-code candidates. Edges = aggregated structural links (call edges,
scene instancing, scene→script attachment).

Usage:  python viz.py            # writes <state_dir>/graph.html (active config)
        python viz.py out.html   # custom output path
"""

from __future__ import annotations

import base64
import math
import os
import re
import json
import sys
from datetime import datetime
from pathlib import Path
import navconfig, navindex, navstore
import graph
from layout import _strata_analysis, _layout
from bake.files_model import _attach, _dead_flags, _build_nodes, _build_links
from bake.wires import _emit_wire_rows, _signal_wires, _wire_budget, _fn_roster
from bake.embeddings import _fetch_embeddings, _knn_sims
from bake.semantics import (SEM_AFF_CAP, _cluster_matrix, _sem_aff,
                            _supergroups)  # SEM_AFF_CAP re-export: test_viz pins it
from bake.overlays import _highways, _cap_highways, _crosstalk_top
from bake.fnio import _fn_io, _cap_fnio
from bake.gitinfo import head, churn
import vizjs  # the 17-section graph.html template package (#299 A)




def _layout_stage(nodes, links, sims, ckeys, cmat):
    """J12: strata depths + churn channel + frozen offline layout.
    Aborts the bake loudly when the layout pass fails."""
    # frozen layout: deterministic offline sim bakes positions into DATA so
    # the browser loads a settled picture (no live global sim, no 900-tick
    # settle, identical output across regenerations). No in-browser fallback:
    # a failed offline pass aborts the build loudly.
    pos_baked = None
    depths, cyc_ids = _strata_analysis(len(nodes), links)
    # git-churn channel — ONE read of git state per bake (D2/#86): it
    # feeds both the layout radii and DATA.hot below, so a mid-bake
    # commit can never bake layout ≠ legend. The overlap relax MUST
    # use the same radii the browser draws or hot files overlap
    # neighbors; None when git/history is unavailable.
    hot = churn([nd["path"] for nd in nodes], str(navconfig.ROOT))
    try:
        pos_baked = _layout(
            len(nodes), links, sims, [nd["cluster"] for nd in nodes],
            ckeys=ckeys, cmat=cmat, depths=depths, hot=hot,
        )
    except Exception as e:
        raise RuntimeError(f"offline layout failed: {e}") from e
    return pos_baked, depths, cyc_ids, hot


def _assemble(nodes, links, fedges, mwires, fns, hw, fio, pos, hot,
              groups2, n_clusters, dead_flag, dead, cluster_names,
              depths, cyc_ids, crosstalk, sig_resolved, sig_unresolved,
              wire_dropped, hw_dropped, fio_dropped, sem_aff, sem_dropped):
    """J18: final DATA assembly with conditional channels."""
    data = {
        "nodes": nodes,
        "links": links,
        "fedges": fedges,
        "mwires": mwires,
        "fns": fns,
        "hw": hw,
        "fio": fio,
        # #279: semantic-affinity rows — the J9 layout pairs promoted to
        # ink (already ranked + capped); [] when the store is unusable
        "semAff": sem_aff,
        "pos": pos,
        "meta": {
            "files": len(nodes),
            "edges": len(links),
            "clusters": n_clusters,
            "deadFiles": len(dead_flag),
            "deadLikely": dead["by_tier"].get("likely", 0),
            "deadReview": dead["by_tier"].get("review", 0),
            # cid -> human name from navstore.clusters() labeler cascade
            "clusterNames": cluster_names,
            # freshness stamp: when this DATA was generated and from which
            # neuronav commit (rendered in #stats so stale pages are obvious)
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "git": head(Path(__file__).resolve().parent),
            # strata channel: height = call depth from entry files
            "strata": True,
            "depth": depths,
            # files inside call cycles (SCC size > 1) - madge's
            # cyclicNodeColor set; the cycles toggle frames them
            "cycIds": cyc_ids,
            # top inter-cluster corridors, labeled at their arc midpoints
            "crosstalk": crosstalk,
            # signal-resolution counters (map-spec-v2 §0/F13): scene
            # connections traced to a handler fn vs left anonymous
            "sig_resolved": sig_resolved,
            "sig_unresolved": sig_unresolved,
        },
    }
    if hot is not None:
        data["hot"] = hot
    if groups2:
        data["groups"] = groups2
    if wire_dropped or hw_dropped or fio_dropped or sem_dropped:
        # export budget engagement record (spec §4 row 10) — present only
        # when a cap actually trimmed something, so small-repo DATA stays
        # byte-identical to the uncapped pipeline
        data["meta"]["budget"] = {
            "wireRowsDropped": wire_dropped,
            "hwArcsDropped": hw_dropped,
            "fioDropped": fio_dropped,
            "semAffDropped": sem_dropped,
        }
    return data


def _build_data() -> dict:
    g = graph.get_graph()
    clusters = navstore.clusters()

    file_cluster, cluster_names = _attach(clusters)
    dead_flag, dead_likely, dead = _dead_flags(
        g, graph.DEAD_TIER_WEIGHTS, graph.DEAD_SHARE_THRESHOLD)
    paths, idx, nodes = _build_nodes(g, file_cluster, dead_flag, dead_likely)
    links = _build_links(g, idx)

    # lazy pagerank shared by the wire + fio budgets (J7/J16 over-cap
    # branches only — small-repo bakes never compute it)
    _rank = None

    def rank_of(path: str) -> float:
        nonlocal _rank
        if _rank is None:
            _rank = g.pagerank()
        return _rank.get(path, 0.0)

    fedges, mwires = _emit_wire_rows(g, idx)
    sig_rows, sig_resolved, sig_unresolved = _signal_wires(g, idx)
    mwires.extend(sig_rows)
    # deterministic named-wire order: ty, sf, df, dfn, sfn, line (spec §0)
    mwires.sort(key=lambda w: (w[0], w[1], w[3], w[4], w[2], w[5]))
    fedges, mwires, wire_dropped = _wire_budget(fedges, mwires, paths, rank_of)
    fns = _fn_roster(g, paths)

    n_clusters = len(clusters)

    emb = _fetch_embeddings(paths)
    sims, emb_knn = _knn_sims(emb)
    cid_gid, groups2 = _supergroups(clusters, emb_knn)
    # #279: promote the J9 pairs (layout springs) to also-rendered ink
    sem_aff, sem_dropped = _sem_aff(emb, sims, idx)

    # supergroup id per node (gid; -1 = unclustered / groups unavailable)
    for nd in nodes:
        nd["gid"] = cid_gid.get(nd["cluster"], -1)

    ckeys, cmat = _cluster_matrix(nodes, emb)
    pos_baked, depths, cyc_ids, hot = _layout_stage(
        nodes, links, sims, ckeys, cmat
    )

    hw = _highways(pos_baked, nodes, links)
    hw, hw_dropped = _cap_highways(hw, links)

    fio = _fn_io(g)
    fio, fio_dropped = _cap_fnio(fio, rank_of)

    crosstalk = _crosstalk_top(links, nodes)

    return _assemble(
        nodes, links, fedges, mwires, fns, hw, fio, pos_baked, hot,
        groups2, n_clusters, dead_flag, dead, cluster_names,
        depths, cyc_ids, crosstalk, sig_resolved, sig_unresolved,
        wire_dropped, hw_dropped, fio_dropped, sem_aff, sem_dropped,
    )




_VENDOR = Path(__file__).resolve().parent / "vendor" / "three-0.160.0"
_ADDONS = {   # keys the template imports; keep in sync with its import lines
    "three/addons/controls/OrbitControls.js": "controls/OrbitControls.js",
    "three/addons/lines/LineSegments2.js": "lines/LineSegments2.js",
    "three/addons/lines/LineSegmentsGeometry.js": "lines/LineSegmentsGeometry.js",
    "three/addons/lines/LineMaterial.js": "lines/LineMaterial.js",
}


def _data_uri(js: str) -> str:
    js = js.replace("\r\n", "\n")   # byte-stable embed across LF/CRLF checkouts
    return "data:text/javascript;base64," + base64.b64encode(js.encode("utf-8")).decode("ascii")


def _importmap() -> str:
    """Zero-network artifact: three + the addons the template imports are
    vendored (pinned 0.160.0, sha-pinned in vendor/) and embedded as data:
    URIs at build time. data: modules cannot resolve RELATIVE specifiers, so
    the addons' relative imports are rewritten to their importmap keys."""
    core = (_VENDOR / "three.module.js").read_text(encoding="utf-8")
    imports = {"three": _data_uri(core)}
    for key, rel in _ADDONS.items():
        src = (_VENDOR / rel).read_text(encoding="utf-8")
        src = re.sub(r"from\s+'\.\./(controls|lines)/([A-Za-z0-9_.]+)'",
                     r"from 'three/addons/\1/\2'", src)
        imports[key] = _data_uri(src)
    return json.dumps({"imports": imports}, separators=(",", ":"))


def _bake_store_guard() -> None:
    """Issue #64: refuse to bake graph.html from an empty or zeroed store.

    A wiped store used to bake silently — every semantic channel
    (clusters, kNN pairs, supergroups) degraded to empty and the graph
    came out ~300KB short with no error: the store-wipe shape behind
    issues #91/#159. The bake now refuses unless the store actually
    covers the walk:
      - 0 walked files: nothing to bake (rescan's issue #41 law);
      - 0 vectors with files on disk: the store is empty or absent —
        the incident class. NEURONAV_EMBED_FAKE does NOT lift this leg:
        every hermetic rig rescan-populates the store it bakes from, so
        a zero-vector store is never deliberate;
      - under half the walked files embedded: a partially wiped or
        foreign store. Only NEURONAV_EMBED_FAKE=1 waives this leg — the
        documented override for deliberate tiny hermetic stores; a
        real-provider run never gets the waiver.
    """
    walk_n = sum(1 for _ in navindex.iter_files())
    if walk_n == 0:
        raise RuntimeError(
            f"refusing to bake graph.html: the walk over root={navconfig.ROOT} "
            f"found 0 files (include_dirs={list(navconfig.INCLUDE_DIRS)}, "
            f"extensions={sorted(navconfig.EXTS)}) — a bake over nothing is the "
            "silent-empty-graph failure (issues #64/#41); fix the config "
            "or point it at a real checkout"
        )
    store_n = navstore.count()
    cfg = os.environ.get("NEURONAV_CONFIG")
    rescan_cmd = (f"python nav.py --config {cfg} rescan" if cfg
                  else "python nav.py rescan (in the project root)")
    if store_n == 0:
        raise RuntimeError(
            f"refusing to bake graph.html from an empty store (issue #64): "
            f"collection '{navconfig.COLLECTION}' in {navconfig.DB_DIR} holds 0 "
            f"vectors while the walk found {walk_n} files — this is the "
            "wiped-store shape that silently shipped a ~300KB-short graph. "
            f"Fix: {rescan_cmd}"
        )
    if store_n * 2 < walk_n:
        if not os.environ.get("NEURONAV_EMBED_FAKE"):
            raise RuntimeError(
                f"refusing to bake graph.html from a near-empty store "
                f"(issue #64): collection '{navconfig.COLLECTION}' in "
                f"{navconfig.DB_DIR} holds {store_n} vectors for {walk_n} "
                "walked files (<50%) — a partial bake would silently "
                f"degrade every semantic channel. Fix: {rescan_cmd} "
                "(deliberate tiny hermetic store: NEURONAV_EMBED_FAKE=1 "
                "waives this leg)"
            )
        print(f"neuronav: baking from a partial FAKE store "
              f"({store_n}/{walk_n} files) — hermetic rig, issue #64 "
              "waiver engaged", file=sys.stderr)


def _reject_nonfinite(node, path: str) -> None:
    """Issue #108: NaN/Infinity never reach graph.html. A non-finite
    float in DATA means a broken computation upstream (layout, sims,
    churn) — refuse with the offending path instead of writing invalid
    JSON the page would have to choke on."""
    if isinstance(node, float):
        if not math.isfinite(node):
            raise RuntimeError(
                f"bake payload {path} is {node!r} (issue #108) — a NaN/"
                "Infinity position or weight means a broken computation "
                "upstream; refusing to write invalid JSON into graph.html"
            )
    elif isinstance(node, dict):
        for k, v in node.items():
            _reject_nonfinite(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _reject_nonfinite(v, f"{path}[{i}]")


_SPLICE_MARKS = ("__DATA__", "__IMPORTMAP__")


def _strict_json(data, what: str) -> str:
    """Issue #108: strict-JSON serialize one bake payload — the finite
    walk above refuses NaN/Infinity by path, and allow_nan=False is the
    backstop at the serialization boundary."""
    _reject_nonfinite(data, what)
    try:
        return json.dumps(data, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"{what} payload is not strict JSON: {e}") from e


def _splice_safe(payload: str, what: str) -> str:
    """Issue #108: a splice payload must never break out of the script
    block or inject a second payload. '</script' (case-insensitive —
    HTML ends script data on any case) ends the block early from inside
    a JSON string; a literal __DATA__/__IMPORTMAP__ mark inside one
    payload would get the OTHER payload spliced into it by the chained
    replace. Crafted docs/names get a loud refusal, not a corrupt
    graph.html."""
    if "</script" in payload.lower():
        raise RuntimeError(
            f"{what} payload contains '</script' (case-insensitive) — a "
            "crafted doc/name would break out of the graph.html script "
            "block (issue #108); rename the offending content"
        )
    for mark in _SPLICE_MARKS:
        if mark in payload:
            raise RuntimeError(
                f"{what} payload contains the {mark} splice mark — repo "
                "content must never inject payloads into the template "
                "(issue #108)"
            )
    return payload


def _atomic_write(out: Path, html: str) -> None:
    """Issue #108: graph.html lands whole or not at all — bytes build in
    a sibling temp, then os.replace (atomic same-dir rename, the #158
    export_base manifest-last law). A crash mid-write leaves the previous
    bake intact; the temp is reaped on any failure."""
    tmp = out.with_name(out.name + ".tmp")
    try:
        # issue #118: text mode rewrites \n to os.linesep (\r\n on Windows)
        # — pin LF so the bake is byte-identical across platforms over the
        # same data
        tmp.write_text(html, encoding="utf-8", newline="\n")
        os.replace(tmp, out)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def generate(out: str | Path | None = None) -> Path:
    out = Path(out) if out else navconfig.STATE_DIR / "graph.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    _bake_store_guard()
    data = _build_data()
    html = (vizjs.template().replace("__DATA__", _splice_safe(_strict_json(data, "DATA"), "DATA"))
                    .replace("__IMPORTMAP__", _splice_safe(_importmap(), "importmap")))
    _atomic_write(out, html)
    return out

def ensure_bake() -> Path:
    """The one rescan→bake entry point (D14, issue #86 R8).

    server.visualize and onboard._index delegate here so the bake
    choreography has a single owner; their own add-on-absence messages
    stay at the import guard (viz.py itself is delete-able). nav's CLI
    stays bake-free by design — it must not import viz.
    """
    return generate()

if __name__ == "__main__":
    path = generate(sys.argv[1] if len(sys.argv) > 1 else None)
    print(path)
