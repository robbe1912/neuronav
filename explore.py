"""explore(): one call = line-numbered source + call flow + budget discipline.

Steals codegraph's measured agent-wayfinding rules (colbymchenry/codegraph,
CLAUDE.md): Read-equivalent `cat -n` slices so output is Edit-safe,
score-proportional allocation with a cliff to pointer lines for weak hits,
hard total budget under the host's inline tool-result cap, and
success-shaped output on every recoverable condition (embedding backend
down -> lexical fallback, never an MCP error).
"""
from __future__ import annotations

import re

from pathlib import Path

import graph
import nav

TOTAL_CAP = 20_000          # chars; hosts externalize bigger results to files,
                            # which re-introduces a Read (codegraph tools.ts)
MIN_HIT_CAP = 1_200
MAX_HIT_CAP = 6_000
CLIFF_FRACTION = 0.15       # hits below this share of top score: pointer only
_TOKEN_RE = re.compile(r"[a-zA-Z_]{4,}")


def _tokens(q: str) -> set[str]:
    return set(_TOKEN_RE.findall(q.lower()))


def _lexical_fallback(query: str, n: int) -> list[dict]:
    """Deterministic name/path/class substring match when embeddings are
    unreachable. Same hit shape as graph.find_functions so callers stay
    uniform."""
    toks = _tokens(query)
    g = graph.get_graph()
    hits: list[dict] = []
    for path, fs in g.files.items():
        hay = f"{path} {fs.class_name or ''}".lower()
        for name, fn in fs.funcs.items():
            strength = sum(1 for t in toks if t in name.lower())
            if not strength:
                # path/class match alone is weaker but still a hit
                if any(t in hay for t in toks):
                    strength = 1
                else:
                    continue
            hits.append({
                "path": path, "func": name, "line": fn.line,
                "score": round(0.5 + 0.1 * min(strength, 5), 3),
            })
    hits.sort(key=lambda h: -h["score"])
    return hits[:n]


def _seed_hits(query: str, n: int) -> tuple[list[dict], bool]:
    """(hits, degraded). chroma fn-level seeds with lexical fallback."""
    try:
        hits = graph.find_functions(query, n)
        return (hits, False) if hits else (_lexical_fallback(query, n), True)
    except Exception:
        return _lexical_fallback(query, n), True


def _flow(g, path: str, fn_name: str) -> str:
    """One-line callers/callees header from the structural graph
    (g.edges = out-adjacency, g.reverse = in-adjacency)."""
    key = f"{path}::{fn_name}"
    callers: set = g.reverse.get(key) or set()
    callees: set = g.edges.get(key) or set()
    fmt = lambda keys: ", ".join(
        k.split("::", 1)[0].rsplit("/", 1)[-1] + "::" + k.split("::", 1)[1].lstrip("_")[:24]
        for k in sorted(keys)[:3]
    ) + (f" +{len(keys) - 3} more" if len(keys) > 3 else "")
    return f"callers: {len(callers)} ({fmt(callers) if callers else 'none - entry or dead'}) | callees: {len(callees)}"


def _slice(path: str, fn_line: int, body: str, cap: int) -> str | None:
    """Read-equivalent slice starting at the real `func` line, `cat -n`
    prefixed. None when the file is unreadable."""
    p = nav.ROOT / path
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    start = max(0, fn_line - 1)
    # body excludes the (possibly multi-line) header; allow for it
    end = min(len(lines), fn_line - 1 + len(body.splitlines()) + 5)
    out = []
    used = 0
    for i in range(start, end):
        ln = f"{i + 1}\t{lines[i]}"
        if used + len(ln) > cap:
            out.append(f"... [{path}:{i + 1} continues]")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def run(query: str, n: int = 4) -> str:
    n = max(1, min(n, 8))
    g = graph.get_graph()
    seeds, degraded = _seed_hits(query, n)

    if not seeds:
        return (
            f"no hits for '{query}'. Next steps: find_functions with a symbol "
            "name you saw in the code; semantic_search for file-level recall; "
            "rescan() if files were just created."
        )

    top = seeds[0]["score"]
    labels = _cluster_labels()
    total = 0
    parts: list[str] = []
    if degraded:
        note = "(degraded: embedding backend unreachable - lexical fallback)"
        parts.append(note)
        total += len(note)

    for h in seeds:
        key = f"{h['path']}::{h['func']}"
        if total >= TOTAL_CAP:
            break
        weak = h["score"] < CLIFF_FRACTION * top
        per_cap = max(MIN_HIT_CAP, min(MAX_HIT_CAP, (TOTAL_CAP - total) // max(1, len(seeds))))
        if weak or per_cap < MIN_HIT_CAP:
            parts.append(f"- {h['path']}::{h['func']}:{h['line']} (score {h['score']:.3f} - not shown; find_functions('{h['func']}') for source)")
            total += 90
            continue
        fs = g.files.get(h["path"])
        body = fs.funcs[h["func"]].body if fs and h["func"] in fs.funcs else ""
        label = labels.get(h["path"], "")
        header = f"** {h['path']} **  [{label}] score {h['score']:.3f}"
        flow = _flow(g, h["path"], h["func"])
        block = header
        sl = _slice(h["path"], h["line"], body, per_cap - len(header) - len(flow) - 4)
        if sl:
            block += "\n" + sl
        block += f"\n{flow}"
        parts.append(block)
        total += len(block) + 2

    return "\n\n".join(parts)[:TOTAL_CAP]


def _cluster_labels() -> dict[str, str]:
    try:
        out: dict[str, str] = {}
        for c in nav.clusters():
            for p, _cls in c["paths"]:
                out[p] = c["label"]
        return out
    except Exception:
        return {}
