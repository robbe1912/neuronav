"""Query family: hybrid-recall + grep-class search tools (issue #345).

Tool-handler family split out of server.py — handlers are verbatim
moves. server.py owns the gate rails (boot handshake, _route scoping,
_serve shell, the _auto_rescan freshness gate, _stale_prelude) and
passes them in via register(): the pinning suites (test_server_stdio,
test_onboardprogress, test_autorescan) reach those names through
server's own namespace, so their definitions cannot leave it.
FastMCP registration happens in register(): every tool here is
read-only, one uniform annotation, applied in the historical def order
so tools/list order is unchanged. register returns the handlers so
server.py can keep binding them at module scope (tests call
server.repo_map & co directly).
"""

from __future__ import annotations

import fnmatch
import re

from mcp.server.fastmcp import Context
from servercore import READONLY, mcp

import explore as _explore
import graph
import navconfig, navindex, navstore
import recall


_EMPTY_INDEX = "no results (index empty — call rescan first)"


def _fmt(hits: list[dict]) -> str:
    """Format hybrid-recall hits: RRF-fused score, src provenance
    (vec/bm25/both), bidirectional 1-hop ctx labels, weak markers for
    under-floor rows (issue #297)."""
    if not hits:
        return _EMPTY_INDEX
    lines: list[str] = []
    if hits[0].get("degraded"):
        why = hits[0].get("degraded_reason") or "vector index unavailable"
        lines.append(f"degraded: BM25F-only ({why})")
    for h in hits:
        label = h.get("class_name") or h.get("extends") or h.get("ext") or ""
        tag = f"  [{label}]" if label else ""
        # issue #74 two-pass hits carry their marker to the wire (off by
        # default, so default rows stay byte-identical)
        tp = "  2pass" if h.get("two_pass") else ""
        # issue #297: under-floor rows are noise-shaped — tagged, and
        # counted once in the footer that names the floor
        wk = "  weak" if h.get("weak") else ""
        ctx = ", ".join(h.get("ctx") or [])
        lines.append(
            f"{h['score']:0.4f}  {h['file']}  src={h['src']}  ctx=[{ctx}]{tag}{tp}{wk}"
        )
    weak_n = sum(1 for h in hits if h.get("weak"))
    if weak_n:
        lines.append(
            f"[{weak_n} weak hit(s) below the relevance floor "
            f"(cos < {recall.RELEVANCE_FLOOR_SIM:.2f} / bm25 < "
            f"{recall.RELEVANCE_FLOOR_BM25:.1f}) — likely noise]"
        )
    return "\n".join(lines)
MAX_MAP_BUDGET = 8192
MIN_MAP_BUDGET = 256


def _here(g) -> str:
    """One-line you-are-here header: which checkout, how big, how many
    subsystems — stamped on orientation-tool responses so a client can
    always tell which project it is talking to."""
    n_clusters = len(navstore.clusters())
    return f"you are here: {navconfig.ROOT.as_posix()} — {len(g.files)} files, {n_clusters} clusters"

@mcp.tool(annotations=READONLY)
async def explore(
    query: str = "",
    n: int = 4,
    anchor: str = "",
    orientation: bool = True,
    dir: str = "",
    ctx: Context = None,
) -> str:
    """One-call orientation for "how does X work" questions.

    Seeds on the function-level vector index (lexical fallback when the
    embedding backend is down), returns Read-equivalent `cat -n` source
    slices with real line numbers, plus a callers/callees flow line per
    hit. Slices are capped at a 100-line window (issue #69); when a file
    continues past the window the slice ends with
    `... +N more lines - pass anchor="path:start-end" to continue` —
    call explore again with exactly that anchor string ALONE (query not
    required when anchor is present, issue #276) to page forward without
    re-querying. Weak hits become pointer lines instead of noise; total
    output is budget-capped so nothing externalizes to a file mid-answer.

    orientation=False (issue #125, repeat calls) skips the constant
    repo-map + cluster-map preamble and spends that budget on the file
    shortlist and slices instead.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    def _body() -> str:
        if not query and not anchor:
            return ('explore: pass query="how does X work", or anchor='
                    '"path:start-end" exactly as printed at the end of a '
                    "previous slice to page forward (one of the two is required)")
        with _route(dir) as prelude:
            if prelude:
                return prelude
            _auto_rescan()
            return _explore.run(query, n, anchor, orientation)

    return await _serve(_body, ctx)

@mcp.tool(annotations=READONLY)
async def repo_map(
    budget_tokens: int = 2048,
    dir: str = "",
    ctx: Context = None,
) -> str:
    """Token-budget repo map — the cheap orientation preamble.

    Aider-style: files ranked by structural PageRank (edge weight = wire
    count), each with its key signatures, tree-grouped by directory,
    truncated at the token budget. Call this first to learn the layout,
    then context(path) on any file that matters.

    During a first index build this serves the previous graph tagged
    `stale: true` (or a progress pointer when none exists yet — issue
    #315); poll the neuronav://onboarding/status resource.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    def _body() -> str:
        stale = _stale_prelude()
        if stale is not None and not dir:
            g = graph._graph
            if g is None:
                return (
                    f"{stale}\nthe graph lands when the first pass completes — "
                    "semantic_search already serves the partially-built "
                    "index; poll neuronav://onboarding/status"
                )
            budget = max(MIN_MAP_BUDGET, min(budget_tokens, MAX_MAP_BUDGET))
            out = (
                stale + "\n" + _here(g) + "\n"
                + graph.repo_map(budget_tokens=budget)
            )
            if budget != budget_tokens:
                out += (
                    f"\n(budget clamped to {budget} — legal range "
                    f"{MIN_MAP_BUDGET}..{MAX_MAP_BUDGET})"
                )
            return out
        with _route(dir) as prelude:
            if prelude:
                return prelude
            _auto_rescan()
            budget = max(MIN_MAP_BUDGET, min(budget_tokens, MAX_MAP_BUDGET))
            g = graph.get_graph()
            out = _here(g) + "\n" + graph.repo_map(budget_tokens=budget)
            if budget != budget_tokens:
                out += (
                    f"\n(budget clamped to {budget} — legal range "
                    f"{MIN_MAP_BUDGET}..{MAX_MAP_BUDGET})"
                )
            return out

    return await _serve(_body, ctx)

@mcp.tool(annotations=READONLY)
async def semantic_search(
    query: str,
    n: int = 8,
    dir: str = "",
    two_pass: bool = False,
    graph_boost: float | None = None,
    ctx: Context = None,
) -> str:
    """Find files in this repo by meaning, not keywords.

    Hybrid recall: vector similarity fused with lexical BM25F ranks —
    src=vec|bm25|both says which side found each hit, ctx= lists up to 3
    structural neighbors worth a look while you are there. Use before
    grep when hunting a concept: input handling, timed effects, save
    system, netcode, AI behavior, item storage.

    Scores are RRF rank-fusion values (1/(30+rank) summed per side that
    found the file, plus the graph-neighbor boost), NOT cosine: ~0.03 is
    a strong top hit and 1.0 is unreachable — compare rows by order,
    never against find_functions' 0-1 cosine scale (issue #125). Rows
    whose every side sits under the absolute relevance floor are tagged
    `weak` with a footer naming the floor (issue #297).

    two_pass=True runs the RepoCoder second retrieve (issue #74: pass-1
    hits donate identifiers to one re-embedded augmented query; engaged
    rows are tagged 2pass). graph_boost rides the shipped recall
    default when omitted (λ 0.25, the #228 grid winner — 1-hop wire
    neighbors of top hits get a rank-decayed bump); pass 0.0 to disable
    and larger λ to strengthen; negative values are rejected loudly.

    During a first index build this serves vector-only ranks from the
    partially-built index, tagged `stale: true` (issue #315); poll the
    neuronav://onboarding/status resource.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    def _body() -> str:
        stale = _stale_prelude()
        if stale is not None and not dir:
            req_n = n
            n_c = max(1, min(n, 25))
            # bm25=False, expand=False: the pure-vector bench baseline —
            # lexical fusion needs the structural graph, which does not
            # exist until the first pass completes
            try:
                hits = recall.search(query, k=n_c, bm25=False, expand=False)
            except Exception as e:
                # a read can lose the race with the build's upserts —
                # answer the honest state, not a hard error (loud-failures
                # law: the failure is named, the degraded mode is marked)
                return (
                    f"{stale}\npartial read unavailable this instant "
                    f"({type(e).__name__}: {e}) — retry in a moment or "
                    "poll neuronav://onboarding/status"
                )
            out = stale + "\n" + _fmt(hits)
            if n_c != req_n:
                out += f"\n(n clamped to {n_c} — legal range 1..25)"
            return out
        with _route(dir) as prelude:
            if prelude:
                return prelude
            _auto_rescan()
            k = max(1, min(n, 25))  # local: rebinding n would shadow the param
            out = _here(graph.get_graph()) + "\n" + _fmt(
                navstore.search(query, k, two_pass=two_pass, graph_boost=graph_boost)
            )
            if k != n:
                out += f"\n(n clamped to {k} — legal range 1..25)"
            return out

    return await _serve(_body, ctx)

@mcp.tool(annotations=READONLY)
def find_functions(query: str, n: int = 6, dir: str = "") -> str:
    """Semantic search over individual FUNCTIONS (not whole files).

    Use when you need the exact function implementing a concept, e.g.
    "apply status damage", "spawn projectile", "refresh item UI".
    Returns path::func with line numbers — pair with symbol_graph to see
    how a hit connects.

    Scores are embedding cosine similarity on a 0-1 scale (1.0 =
    identical — issue #125: never read semantic_search's ~0.03 RRF
    fusion values against this scale); degraded rows are tagged lexical
    substring strengths, not cosine.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        req_n = n
        n = max(1, min(n, 15))
        clamp_note = (
            f"\n(n clamped to {n} — legal range 1..15)" if n != req_n else ""
        )
        try:
            hits = graph.find_functions(query, n)
        except Exception as exc:
            # issue #116: backend-down is recoverable — serve explore's
            # same-shape lexical fallback tagged degraded (never a raw
            # MCP error, which is worse when a degraded explore answer
            # has just recommended exactly this tool)
            why = navstore.embed_failure_reason(exc)
            hits = _explore._lexical_fallback(query, n)
            if not hits:
                return f"degraded: {why} — no lexical match for {query!r} either"
            return "\n".join(
                [f"degraded: {why} — lexical fallback (substring, not semantic):"]
                + [
                    f"{h['score']:0.3f}  {h['path']}#{h['func']}:{h['line']}"
                    for h in hits
                ]
            ) + clamp_note
        if not hits:
            return "no function index — call rescan first"
        rows = [
            f"{h['score']:0.3f}  {h['path']}#{h['func']}:{h['line']}" for h in hits
        ]
        weak_n = sum(1 for h in hits if h.get("weak"))
        if weak_n:
            # issue #297: name the floor so the weak rows read as noise,
            # not as confident cosine neighbors
            rows.append(
                f"[{weak_n} of {len(hits)} under the relevance floor "
                f"(cos < {recall.RELEVANCE_FLOOR_SIM:.2f}) — likely noise]"
            )
        return "\n".join(rows) + clamp_note

# Zoekt-style cap discipline (issue #68): summarized, capped search output
# beats verbose paging (SWE-agent 12.0% -> 18.0% on SWE-bench Lite) —
# hard caps + truncation markers + total counts keep the answer
# token-bounded and tell the agent when to narrow.
SEARCH_MAX_FILES = 20
SEARCH_MAX_LINES = 3
SEARCH_LINE_CHARS = 200

@mcp.tool(annotations=READONLY)
def search_text(pattern: str, glob: str = "", files_only: bool = False, dir: str = "") -> str:
    """Regex text search over the indexed files — the grep-class tool.

    Exact strings and regex the semantic+symbol tools structurally miss:
    literals, TODOs, error messages, config keys. Rows are
    file:line:matched-line, ordered by path then line, hard-capped at
    20 files / 3 lines each (Zoekt-style) with per-file and global
    truncation markers plus the total match count — when the cap fires,
    narrow with glob= or a tighter pattern.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        if not pattern:
            return "no pattern given — pass a regex, e.g. search_text('TODO')"
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"invalid regex {pattern!r}: {exc}"
        g = graph.get_graph()
        if not g.files:
            return _EMPTY_INDEX
        matched: list[tuple[str, int, list[tuple[int, str]]]] = []
        scanned = 0
        total = 0
        for path in sorted(g.files):  # deterministic: path, then line order
            if glob and not fnmatch.fnmatch(path, glob):
                continue
            scanned += 1
            try:
                text = navindex._read_text(navconfig.ROOT / path)
            except OSError:
                continue  # vanished mid-walk; the next rescan reconciles
            hits = [
                (i, ln.rstrip())
                for i, ln in enumerate(text.splitlines(), 1)
                if rx.search(ln)
            ]
            if not hits:
                continue
            total += len(hits)
            matched.append((path, len(hits), hits[:SEARCH_MAX_LINES]))
        if not matched:
            return f"no matches for {pattern!r} in {scanned} indexed files"
        lines: list[str] = [_here(g)]
        for path, n, hits in matched[:SEARCH_MAX_FILES]:
            if files_only:
                lines.append(path)
                continue
            for i, ln in hits:
                ln = ln if len(ln) <= SEARCH_LINE_CHARS else ln[:SEARCH_LINE_CHARS] + "…"
                lines.append(f"{path}:{i}:{ln}")
            if n > SEARCH_MAX_LINES:
                lines.append(f"… and {n - SEARCH_MAX_LINES} more matches in {path}")
        n_files = len(matched)
        plural = "" if n_files == 1 else "s"
        if n_files > SEARCH_MAX_FILES:
            lines.append(
                f"… truncated at {SEARCH_MAX_FILES} files: {total} matches in "
                f"{n_files} file{plural} — narrow the pattern or pass glob="
            )
        else:
            lines.append(f"{total} matches in {n_files} file{plural}")
        return "\n".join(lines)


def register(_route, _serve, _stale_prelude, _auto_rescan):
    """Compose this family onto the FastMCP instance (issue #345): the
    gate rails arrive as parameters — importing them from server would
    cycle (server imports this module first) — and bind into this
    module's globals so the verbatim handler bodies resolve them by
    their historical names. Registration order is the historical def
    order, so tools/list is unchanged; the uniform read-only annotation
    is the one fact registration adds."""
    g = globals()
    for _rail in ("_route", "_serve", "_stale_prelude", "_auto_rescan"):
        g[_rail] = locals()[_rail]
    return explore, repo_map, semantic_search, find_functions, search_text
