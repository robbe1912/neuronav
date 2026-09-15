"""Derived-predicate cache (issue #71, steal #8 — Glean's derived facts).

The deep tool queries (dead-code tiers, duplicate groups, PageRank,
symbol degrees, corpus mentions) are pure functions of the built
graph, but graph.py recomputed them on every call. This module derives them ONCE at build/rescan time and
persists them under the project state dir (``.neuronav/predicates.json``,
stdlib json only), so query time becomes dict reads.

Laws:
- Derived artifact: same index data -> byte-identical cache bytes.
  Canonical json (sorted keys, fixed separators), every new derivation
  iterates sorted inputs; list orders come from the shared single code
  path in graph.py, which is itself walk-deterministic.
- Freshness: the cache carries a fingerprint (sha over the parsed
  surface + edge set + roots + a code stamp) and a seal (sha over the
  canonical payload). Stale, corrupt, or missing cache -> rederive at
  rescan, loudly; never serve unverified bytes.
- Fallback: any load/persist failure leaves the graph on the original
  on-the-fly paths (``_pred is None``) — tools stay correct, marked on
  stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import nav
from extractors import registry_for

SCHEMA = 1
NAME = "predicates.json"
# the predicate families persisted (payload shape contract). Only
# families a query path actually reads belong here: reverse
# reachability / SCC / transitive callers have no consumer (a
# depth-bounded symbol_graph BFS cannot read a closure without
# changing its output bytes), so caching them was pure byte+Tarjan
# cost with zero served benefit (review finding, PR #233).
FAMILIES = frozenset({"rank", "wires", "deg", "dead", "dups", "mentions"})

_code_stamp: str | None = None


def _canon(obj: object) -> bytes:
    """Canonical bytes for a JSON-able payload: sorted keys, no
    whitespace, ascii. Floats round-trip through repr, so parse(dumps)
    is the identity for this module's data (ints/strs/floats/lists)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def code_stamp() -> str:
    """Digest of the derivation code itself (graph.py, this module,
    extractors/). Tier rules and edge wiring live there, so an upgrade
    with an untouched corpus must still invalidate the cache. Hashed
    from source text — deterministic per checkout, no mtimes."""
    global _code_stamp
    if _code_stamp is None:
        base = Path(__file__).resolve().parent
        parts = [base / "graph.py", base / "predicates.py"]
        parts += sorted((base / "extractors").glob("*.py"))
        h = hashlib.sha256()
        for p in parts:
            h.update(p.read_bytes())
        _code_stamp = h.hexdigest()
    return _code_stamp


def fingerprint(g) -> str:
    """Digest of everything the cached predicates are a function of:
    per file (rel, ext, class/extends, per-func name/line/body hash),
    the full edge set, roots/referenced sets. Raw file text joins only
    when a mention-floor language is present (mentions count comments,
    which body hashes do not cover)."""
    h = hashlib.sha256()
    h.update(f"{SCHEMA}\n{code_stamp()}\n".encode("ascii"))
    gate = any(
        getattr(registry_for(f.ext), "MENTION_FLOOR", 0) for f in g.files.values()
    )
    for rel in sorted(g.files):
        fs = g.files[rel]
        h.update(f"F\t{rel}\t{fs.ext}\t{fs.class_name or ''}\t{fs.extends or ''}\n".encode())
        for name in sorted(fs.funcs):
            fn = fs.funcs[name]
            body = hashlib.sha1(fn.body.encode("utf-8", "surrogateescape")).hexdigest()
            h.update(f"\t{name}\t{fn.line}\t{body}\n".encode())
        if gate:
            try:
                raw = nav._read_text(nav.ROOT / rel)
            except OSError:
                raw = "<unreadable>"
            h.update(
                (
                    "\tM\t"
                    + hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()
                    + "\n"
                ).encode("ascii")
            )
    for src in sorted(g.edges):
        for dst in sorted(g.edges[src]):
            h.update(f"E\t{src}\t{dst}\n".encode())
    for label, keys in (("R", g.roots), ("f", g.referenced), ("n", g.referenced_names)):
        for k in sorted(keys):
            h.update(f"{label}\t{k}\n".encode())
    return h.hexdigest()


def derive(g) -> dict:
    """Full predicate payload via graph.py's own computation paths
    (single source of truth: what gets cached is by construction what
    the on-the-fly walk returns). ``g._pred`` is None while this runs —
    bind() only sets it after deriving."""
    mentions = None
    if any(getattr(registry_for(f.ext), "MENTION_FLOOR", 0) for f in g.files.values()):
        mentions = dict(sorted(g._mention_counts().items()))
    return {
        "rank": g.pagerank(),
        "wires": g.file_wires(),
        "deg": g._symbol_degrees(),
        "dead": g._dead_rows(),
        "dups": g._dup_groups(),
        "mentions": mentions,
    }


def _seal(payload: dict) -> str:
    return hashlib.sha256(_canon(payload)).hexdigest()


def _state_dir() -> Path:
    return Path(nav.STATE_DIR)


def _load(state_dir: Path, fp: str) -> dict | None:
    """Verified load: seal (integrity) then fingerprint (freshness).
    Any miss returns None — caller rederives. Absent file is the normal
    first run (silent); present-but-unusable is loud."""
    try:
        raw = (state_dir / NAME).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"neuronav: predicates cache unreadable ({e}); rederiving", file=sys.stderr)
        return None
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):  # b"42" / "[1,2]" parse fine
            raise ValueError(f"cache root is {type(doc).__name__}, not an object")
        seal = doc.pop("seal")
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"schema {doc.get('schema')!r} != {SCHEMA}")
        if _seal(doc) != seal:
            raise ValueError("seal mismatch (bytes changed on disk)")
        if doc.get("fingerprint") != fp:
            raise ValueError("fingerprint stale (inputs or derivation code changed)")
        pred = doc["pred"]
        if not isinstance(pred, dict):  # a LIST of family names would
            # pass set.issubset and then explode at query time
            raise ValueError(f"'pred' is {type(pred).__name__}, not an object")
        if not FAMILIES.issubset(pred):
            raise ValueError(f"missing families: {sorted(FAMILIES - set(pred))}")
        return pred
    except (ValueError, KeyError, TypeError) as e:
        print(f"neuronav: predicates cache invalid ({e}); rederiving", file=sys.stderr)
        return None


def _persist(state_dir: Path, fp: str, pred: dict) -> None:
    payload = {"schema": SCHEMA, "fingerprint": fp, "pred": pred}
    doc = dict(payload)
    doc["seal"] = _seal(payload)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / f"{NAME}.{os.getpid()}.tmp"
        tmp.write_bytes(_canon(doc))
        os.replace(tmp, state_dir / NAME)
    except OSError as e:
        print(
            f"neuronav: predicates cache not persisted ({e}); "
            "queries fall back to on-the-fly derivation",
            file=sys.stderr,
        )


def bind(g) -> None:
    """Build-time hook (graph.Graph.build tail): load-or-derive the
    predicate cache for this graph. Concurrent writers are safe — the
    atomic replace plus byte determinism make last-writer-wins
    harmless (both bytes are identical)."""
    state_dir = _state_dir()
    fp = fingerprint(g)
    pred = _load(state_dir, fp)
    if pred is None:
        pred = derive(g)
        _persist(state_dir, fp, pred)
    g._pred = pred
