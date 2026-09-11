"""explore(): one call = repo map + cluster map + file shortlist + source
slices with call flow, under a hard total budget (Agentless funnel).

Steals codegraph's measured agent-wayfinding rules (colbymchenry/codegraph,
CLAUDE.md): Read-equivalent `cat -n` slices so output is Edit-safe,
score-proportional allocation with a cliff to pointer lines for weak hits,
hard total budget under the host's inline tool-result cap, and
success-shaped output on every recoverable condition (embedding backend
down -> lexical fallback, never an MCP error).

Windowed slices (issue #69, SWE-agent ACI steal): every slice is capped at
WINDOW_LINES and ends with a deterministic continuation anchor; passing the
anchor back pages forward without re-querying or re-running the funnel.
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
WINDOW_LINES = 100          # slice height cap (issue #69): SWE-agent's ACI
                            # measured 100-line windows beating whole-file
                            # dumps (arxiv 2405.15793); anchors page forward
_TOKEN_RE = re.compile(r"[a-zA-Z_]{4,}")
_CODE_RE = re.compile(r"^\d+\t")
_ANCHOR_RE = re.compile(r"^(.+):(\d+)-(\d+)$")
PREAMBLE_TOKENS = 900           # repo-map orientation slice; constant per repo
CLUSTER_CAP = 1_800             # chars: cluster map section
SHORTLIST_CAP = 1_200           # chars: file shortlist section


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


def _anchor(path: str, next_start: int, total: int) -> str:
    """Deterministic continuation handle naming the NEXT window (issue #69):
    pass it back as explore(anchor=...) to page forward without re-querying."""
    end = min(total, next_start + WINDOW_LINES - 1)
    return (f"... +{total - next_start + 1} more lines - "
            f'pass anchor="{path}:{next_start}-{end}" to continue')


def _slice(path: str, start: int, end: int, cap: int) -> str | None:
    """Read-equivalent `cat -n` window over the 1-based inclusive
    [start, end] range: never taller than WINDOW_LINES (issue #69),
    char-capped for the budget discipline, ending in a deterministic
    continuation anchor whenever the file continues past the lines shown.
    None when the file is unreadable; "" when no line fits (start past
    EOF, or a cap smaller than the first line)."""
    try:
        text = (nav.ROOT / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    total = len(lines)
    lo = max(0, start - 1)
    hi = min(end, lo + WINDOW_LINES, total)
    out: list[str] = []
    used = 0
    for i in range(lo, hi):
        ln = f"{i + 1}\t{lines[i]}"
        if used + len(ln) > cap:
            break
        out.append(ln)
        used += len(ln) + 1
    if out and lo + len(out) < total:
        out.append(_anchor(path, lo + len(out) + 1, total))
    return "\n".join(out)


def _continue(anchor: str) -> str:
    """Anchor paging (issue #69): serve the next window of a file a previous
    slice pointed at — no funnel, no re-query, and zero clusters() passes
    (the one-pass law of issue #44 holds trivially). Bad or stale anchors
    get guidance, never an error."""
    m = _ANCHOR_RE.match(anchor.strip())
    if not m or int(m.group(2)) < 1 or int(m.group(3)) < int(m.group(2)):
        return (f'unreadable anchor "{anchor}" - expected path:start-end '
                "exactly as printed at the end of an explore slice")
    path, start, end = m.group(1), int(m.group(2)), int(m.group(3))
    # TOTAL_CAP minus header+anchor overhead so the backstop clamp below
    # can never bite mid-handle
    sl = _slice(path, start, end, TOTAL_CAP - 300)
    if sl is None:
        return f"anchor window unavailable: {path} is not readable"
    code = [ln for ln in sl.splitlines() if _CODE_RE.match(ln)]
    if not code:
        return (f"anchor window is empty: no lines at {start} in {path} "
                "(file shortened since the slice? re-run explore)")
    first = code[0].split("\t", 1)[0]
    last = code[-1].split("\t", 1)[0]
    return f"** {path} ** lines {first}-{last}\n{sl}"


def _cluster_map(cs: "list[dict[str, object]] | None") -> str:
    """Funnel stage 1 (constant per repo): subsystem layout, biggest
    first — the Agentless structure map the shortlists narrow into.
    `cs` is the clusters list run() computed once; None = the pipeline
    failed, degrade loudly."""
    if cs is None:
        return "== clusters == (unavailable - embedding index unreachable)"
    lines = [
        f"- {c['label']} ({c['size']} files): "
        + ", ".join(p for p, _cls in c["paths"][:3])
        for c in cs[:8]
    ]
    body = "\n".join(lines)[:CLUSTER_CAP]
    return f"== clusters ==\n{body}" if body else "== clusters == (none)"


def _file_shortlist(seeds: list[dict]) -> str:
    """Funnel stage 2: collapse fn hits to files, best score first."""
    agg: dict[str, list[dict]] = {}
    for h in seeds:
        agg.setdefault(h["path"], []).append(h)
    rows = sorted(
        agg.items(),
        key=lambda kv: (-max(h["score"] for h in kv[1]), kv[0]),
    )
    lines = [
        f"{i}. {p} - {len(hs)} hit(s): {', '.join(h['func'] for h in hs[:4])}"
        for i, (p, hs) in enumerate(rows, 1)
    ]
    return "== file shortlist ==\n" + "\n".join(lines)[:SHORTLIST_CAP]


def _symbol_slices(g, seeds: list[dict], degraded: bool, budget: int, cs) -> str:
    """Funnel leaf: Read-equivalent `cat -n` windows + call flow under the
    codegraph budget discipline (score-proportional caps, cliff to pointer
    lines for weak hits) and the issue #69 window law (height cap +
    continuation anchors)."""
    top = seeds[0]["score"]
    labels = _cluster_labels(cs)
    total = 0
    parts: list[str] = []
    if degraded:
        note = "(degraded: embedding backend unreachable - lexical fallback)"
        parts.append(note)
        total += len(note)

    for h in seeds:
        if total >= budget:
            break
        weak = h["score"] < CLIFF_FRACTION * top
        per_cap = max(MIN_HIT_CAP, min(MAX_HIT_CAP, (budget - total) // max(1, len(seeds))))
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
        # body excludes the (possibly multi-line) header; +4 keeps the
        # pre-window height (body lines + 5) as head/context slop
        end = h["line"] + len(body.splitlines()) + 4
        sl = _slice(h["path"], h["line"], end, per_cap - len(header) - len(flow) - 4)
        if sl:
            block += "\n" + sl
        block += f"\n{flow}"
        parts.append(block)
        total += len(block) + 2

    return "== symbols ==\n" + "\n\n".join(parts)


def run(query: str, n: int = 4, anchor: str = "") -> str:
    if anchor:
        return _continue(anchor)
    n = max(1, min(n, 8))
    g = graph.get_graph()
    seeds, degraded = _seed_hits(query, n)

    # Agentless funnel: constant orientation first (repo map, clusters),
    # then query-dependent narrowing (file shortlist -> symbol slices).
    # Every stage degrades loudly or returns guidance, never an error.
    # one clusters() pass feeds both funnel stages (map + slice labels);
    # nothing rescans between them, so sharing the result is semantically
    # identical and halves the pipeline cost (issue #44)
    try:
        cs = nav.clusters()
    except Exception:
        cs = None
    parts = [
        "== repo map ==\n" + g.repo_map(budget_tokens=PREAMBLE_TOKENS),
        _cluster_map(cs),
    ]
    if not seeds:
        parts.append(
            f"no hits for '{query}'. Next steps: find_functions with a symbol "
            "name you saw in the map; semantic_search for file-level recall; "
            "rescan() if files were just created."
        )
        return "\n\n".join(parts)[:TOTAL_CAP]

    parts.append(_file_shortlist(seeds))
    used = sum(len(p) + 2 for p in parts)
    parts.append(_symbol_slices(g, seeds, degraded, TOTAL_CAP - used, cs))
    return "\n\n".join(parts)[:TOTAL_CAP]


def _cluster_labels(cs: "list[dict[str, object]] | None") -> dict[str, str]:
    """path -> cluster label for slice headers; {} when the shared
    clusters pass failed (degraded mode labels nothing)."""
    if cs is None:
        return {}
    out: dict[str, str] = {}
    for c in cs:
        for p, _cls in c["paths"]:
            out[p] = c["label"]
    return out
