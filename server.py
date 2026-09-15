"""neuronav MCP server (stdio): semantic + structural code intelligence.

Works from any clone/worktree: paths resolve relative to the checkout the
tool lives in. Clients: OpenCode, Claude Code, VS Code, Codex, omp
(`onboard.py wire --omp`, issue #130) — all stdio MCP.

Tools:
- explore(query, n=4, anchor="", orientation=True): START HERE for "how does
  X work" — one call returns windowed line-numbered source slices +
  callers/callees flow for the best hits; slices ending mid-file print a
  continuation anchor — pass it back to page forward without re-querying;
  orientation=False (repeat calls) skips the constant repo-map+cluster-map
  preamble and spends the budget on slices;
- repo_map(budget_tokens=2048): token-budget repo map — files ranked by
  structural PageRank with key signatures, tree-grouped by dir; the cheap
  orientation preamble to call before any search
- semantic_search(query, n=8): hybrid recall — vector + BM25F ranks fused,
  hits carry src provenance and 1-hop ctx neighbors
- find_functions(query, n=6): semantic search over individual functions
- symbol_graph(symbol, depth=1): callers/callees around a function or class,
  rows carry true counts + "+N more", node-capped with a truncation marker
- search_text(pattern, glob="", files_only=False): regex text search over
  the indexed files — grep-class queries (exact strings, TODOs, literals),
  Zoekt-style caps: 20 files / 3 lines each, truncation markers + totals
- dead_code(): functions unreachable from any entry point (candidates only)
- duplicates(): exact-clone function bodies (normalized hash groups)
- clusters(k, min_sim): subsystem clusters over the embedding space
- crosstalk(): cross-cluster coupling-hotspot report
- context(path, depth=1): subsystem map for one file (what it defines
  (funcs/signals/members, capped), cluster, structural + semantic neighbors,
  hub rank) — the fresh-agent orientation tool
- visualize(): generate the interactive 3D graph (graph.html) and return path
- rescan(): incremental re-index of everything above; appends a capped
  changed/deleted path list when anything moved
- memory(verb, name, body): Serena-style project memories (issue #67) —
  durable cross-session notes as plain .neuronav/memories/*.md files;
  verbs list/get/set/delete; mutating (like rescan), not read-only
every tool also takes dir="<checkout>" (issue #131): routes that one call
to another repo's index — one server entry per harness instead of one per
project. A fresh dir onboards on first contact (the build answers in
rescan() format); auto-rescan + watcher stay on the boot project, so alt
dirs refresh via the explicit rescan(dir=...).
- read tools auto-rescan first when the worktree drifted (cheap stat
  fingerprint, TTL-cached); config watch_interval_s > 0 additionally
  polls and rescans without waiting for tool calls
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import explore as _explore
import graph
from extractors import PRESETS, registry_for, res_to_rel  # noqa: E402
import memories
import nav

def _version() -> str:
    """The version serverInfo reports (issue #207): the neuronav package
    version — importlib.metadata answers for any installed copy (uvx/
    wheel; pyproject.toml is the version's source of truth), a plain
    checkout falls back to the pyproject beside this file (tomllib,
    stdlib since 3.11). Same derivation as onboard._uvx_ref, so
    serverInfo and the uvx tag pin (v<version>) can never disagree."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("neuronav")
    except PackageNotFoundError:
        import tomllib
        with open(Path(__file__).resolve().parent / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]


# Server instructions (MCP InitializeResult.instructions, issue #237):
# the agent-facing user manual — cross-tool workflow only, never an echo
# of tool descriptions (official guidance: concise, operational,
# model-agnostic; measured +25%% workflow adherence on GitHub's server).
_INSTRUCTIONS = (
    "Code-structure intelligence over the session's working directory. "
    "Workflow: call repo_map once per project for the layout; explore(topic) "
    "to orient on a subsystem; semantic_search / find_functions for lookups; "
    "symbol_graph / dead_code / duplicates for structure questions; "
    "visualize opens the graph.html bake. Tools are read-only except "
    "rescan (forces reindex) and memory (set/get/list/delete persistent "
    "project notes - save durable findings there, not transient state). "
    "Every tool takes an optional dir to target a different repo root. "
    "The index auto-refreshes on file drift; a tool marked 'degraded' "
    "still answers completely from the current index, though vector "
    "recall may be unavailable. An empty index answers with first-call "
    "guidance; onboard.py init --preset ts|js|python|cpp|gdscript "
    "scaffolds a config for unmatched file types."
)

mcp = FastMCP("neuronav", instructions=_INSTRUCTIONS)
# FastMCP forwards no version to its lowlevel Server (no such kwarg on
# mcp 1.29.x), and create_initialization_options then falls back to
# pkg_version("mcp") — serverInfo answered the mcp library's version,
# not neuronav's. Server.version is a plain attribute read at answer
# time, so pin it here (issue #207); an mcp that renames it fails
# loudly at import rather than silently misreporting.
mcp._mcp_server.version = _version()

# below except rescan and memory is pure read over the local index
READONLY = ToolAnnotations(readOnlyHint=True)

# ---- universal mount (issue #131): one server entry, per-call dir ---------
#
# Every tool below takes dir="" — empty serves the boot config
# (NEURONAV_CONFIG resolved at startup, identical to the per-project
# entries), a checkout path routes that single call to that repo's index.
# Stateless on purpose (#131 decision record): a stateful activate/switch
# adds one round trip per project change and a forgotten-switch class of
# errors.
#
# First contact with a fresh dir onboards it: the scaffold onboard.py
# init writes (.neuronav/config.json with "state_dir": "default" — the
# #91 opt-in — plus .neuroignore and the .gitignore line), then a full
# build (tracked base shards first when present, rescan heals the rest).
# An explicit dir IS consent — NOT the #91 silent-store class; the
# config-file-driven boot keeps its loud abort. The build answers in
# rescan()'s summary format (long builds report like rescan), and the
# next call serves.
#
# Freshness: _auto_rescan and the watcher stay BOOT-config only — the
# stat fingerprint, cooldown and watcher all live in process globals,
# and repointing them per dir would double-embed on alternation. Alt
# dirs refresh through the explicit rescan(dir=...).

_BOOT_STORE = (str(nav.STATE_DIR), nav.COLLECTION)
_SCOPE_LOCK = threading.RLock()  # nav globals are process-wide: one routed call at a time


def _at_boot() -> bool:
    return (str(nav.STATE_DIR), nav.COLLECTION) == _BOOT_STORE


# ---- degraded boot (issue #240) ----------------------------------------------
# A boot rescan that matched 0 files (pure defaults on a TS-only repo,
# or any config whose walk matches nothing) used to raise pre-handshake:
# the client saw a dead subprocess and the actionable text sat in
# stderr nobody reads. Instead the boot degrades: an empty store plus
# this flag, and every tool answers first-call guidance — the
# extensions actually scanned, a suffix census of the root, and a
# paste-ready config block for the file types found on disk. nav's
# 0-file RuntimeError stays the explicit-rescan contract (#41 law);
# only the boot path degrades, and _boot_recovery re-binds the boot
# config in-session the moment one appears.
_BOOT_DEGRADED: str | None = None

# census suffixes suggested in the guidance's config block before the
# cut — deterministic: ranked by count, ties by name
_GUIDANCE_SUGGEST_CAP = 8
# binary/asset suffixes never suggested for indexing: raw-embedding
# assets pollutes the vector space for no recall value
_CENSUS_DENY = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".pdf",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so",
    ".woff", ".woff2", ".ttf", ".otf", ".map", ".lock", ".bin", ".wasm",
})


def _preset_hint(suggestions: list[str]) -> str | None:
    """The preset covering a guidance's suggested suffixes (issue #240):
    first hit in a fixed preference order — ts before js, so a mixed web
    repo suggests the fuller list. None when no preset applies."""
    for name in ("ts", "js", "python", "cpp", "gdscript"):
        if set(PRESETS[name]) & set(suggestions):
            return name
    return None


def _boot_guidance(census: dict[str, int], probe_fail: str | None) -> str:
    """First-call guidance for a 0-file boot (issue #240): what was
    scanned, what the root actually holds, the paste-ready fix. A pure
    function of the census + the boot config — deterministic."""
    lines = [
        "neuronav: EMPTY INDEX — the boot walk matched 0 files, so this "
        "server serves guidance instead of results (issue #240). Every "
        "tool answers with this text until a config covers the repo.",
        f"  root: {nav.ROOT.as_posix()}",
        f"  include_dirs: {list(nav.INCLUDE_DIRS)}",
        f"  extensions scanned: [{', '.join(sorted(nav.EXTS))}]",
    ]
    ranked = sorted(
        ((s, c) for s, c in census.items()
         if s.startswith(".") and s not in _CENSUS_DENY),
        key=lambda kv: (-kv[1], kv[0]),
    )
    if ranked:
        head = ", ".join(f"{s} x{c}" for s, c in ranked[:_GUIDANCE_SUGGEST_CAP])
        rest = len(ranked) - min(len(ranked), _GUIDANCE_SUGGEST_CAP)
        lines.append(
            f"  file types on disk (same walk, any suffix): {head}"
            + (f" (+{rest} more)" if rest > 0 else "")
        )
    else:
        lines.append(
            "  file types on disk: NONE under the walk — check root/"
            "include_dirs (onboard.py init --project <path> scaffolds a "
            "walk-all config)"
        )
    suggestions = [s for s, _ in ranked[:_GUIDANCE_SUGGEST_CAP]
                   if s not in nav.EXTS]
    if suggestions:
        block = {
            # "root": ".." — a project-local config resolves root against
            # .neuronav itself (the #240 trap); its parent IS the project
            "root": "..",
            "collection": "main",
            "state_dir": "default",
            "include_dirs": list(nav.WALK_DEFAULTS["include_dirs"]),
            "extensions": suggestions,
            "exclude_dirs": list(nav.WALK_DEFAULTS["exclude_dirs"]),
        }
        lines.append("  fix: write .neuronav/config.json under the root with")
        lines.extend("    " + ln for ln in json.dumps(block, indent=2).splitlines())
        hint = _preset_hint(suggestions)
        if hint is not None:
            lines.append(f"  (or run: onboard.py init --preset {hint})")
        unreg = [s for s in suggestions if registry_for(s) is None]
        if unreg:
            lines.append(
                f"  note: {', '.join(unreg)} have no structural extractor — "
                "they index as raw text (semantic_search/search_text work), "
                "but find_functions/symbol_graph/dead_code return nothing "
                "for these files until extractors land (TS is the tracked "
                "follow-up)"
            )
        lines.append(
            "  then call rescan (this session picks the new config up) "
            "or restart the session"
        )
    if probe_fail is not None:
        lines.append(f"  embed backend: {probe_fail}")
    return "\n".join(lines)


def _probe_embedder() -> str | None:
    """One 1-token embed against the configured provider at boot (issue
    #240): a wrong model or dead endpoint dies HERE with the fix in the
    message, not 200s into the first rescan (a jina GGUF tag routed to
    a generation runner answers /api/embed with a protocol error — that
    whole failure class dies at boot now). None = healthy or FAKE."""
    if os.environ.get("NEURONAV_EMBED_FAKE"):
        return None  # CI plumbing: no network by contract
    try:
        nav.embed(["."])
        return None
    except Exception as e:
        reason = nav.embed_failure_reason(e)
        if nav.EMBED_PROVIDER == "ollama":
            fix = f"ollama pull {nav.EMBED_MODEL}"
        else:
            fix = (
                f"check embed_model '{nav.EMBED_MODEL}' at embed_url "
                f"'{nav.EMBED_URL}' (auth via NEURONAV_EMBED_KEY)"
            )
        return (
            f"embed probe FAILED — model '{nav.EMBED_MODEL}' via "
            f"{nav.EMBED_PROVIDER} at {nav.EMBED_URL}: {reason}. Fix: {fix}."
        )


def _raw_text_banner(census: dict[str, int]) -> None:
    """Loud unsupported-language announcement (issue #240): files that
    MATCH the configured extensions but have no extractor index as raw
    text — fns 0 is otherwise indistinguishable from an empty repo.
    stderr at boot and after in-session recovery; the degraded guidance
    names it too."""
    hits = sorted(
        (s, c) for s, c in census.items()
        if s in nav.EXTS and registry_for(s) is None
    )
    if not hits:
        return
    named = ", ".join(f"{s} x{c}" for s, c in hits)
    print(
        f"neuronav: no structural extractor for {named} — indexed as raw "
        "text (semantic_search/search_text work), but find_functions/"
        "symbol_graph/dead_code return nothing for these files "
        "(onboard.py init --preset ts|js|python|cpp|gdscript curates "
        "extensions; a TS extractor is the tracked follow-up)",
        file=sys.stderr,
    )


def _enter_degraded(census: dict[str, int], probe_fail: str | None,
                    why: str) -> None:
    """Flip the boot into guidance mode (issue #240): set the flag every
    tool answers with, plus the stderr banner."""
    global _BOOT_DEGRADED
    _BOOT_DEGRADED = _boot_guidance(census, probe_fail)
    print(
        f"neuronav: DEGRADED — {why}; serving an empty index, every tool "
        "answers with first-call guidance",
        file=sys.stderr,
    )


def _boot_recovery() -> str | None:
    """In-session re-bind for a degraded pure-defaults boot (issue #240):
    the guidance says to write <root>/.neuronav/config.json — the next
    tool call picks it up here instead of demanding a restart. A
    config-FILE-driven boot stays degraded (its fix is editing that
    config, then restarting: discovery would hand back the same path).
    Returns the prelude the call answers with, or None when still
    degraded."""
    global _BOOT_DEGRADED, _BOOT_STORE
    if nav.CONFIG_PATH is not None:
        return None
    cfg_path = Path.cwd() / ".neuronav" / "config.json"
    if not cfg_path.is_file():
        return None
    _validate_foreign_config(cfg_path, Path.cwd())
    try:
        nav.use_config(cfg_path)
        _BOOT_STORE = (str(nav.STATE_DIR), nav.COLLECTION)
        stats = _bounded_rescan()
        g, fns, note = _sync_chain(stats)
        nav.stat_mark_synced()
        _raw_text_banner(nav.suffix_census())
        _BOOT_DEGRADED = None
        return (
            f"config appeared mid-session — rebound the boot to "
            f"{cfg_path.as_posix()} and indexed: files "
            f"{stats['added']}/{stats['updated']}/{stats['unchanged']}/"
            f"{stats['deleted']} (a/u/u/d), fns {fns['fns_upserted']} "
            f"upserted, graph {len(g.files)} files{note}. Call again to query."
        )
    except Exception as e:
        _BOOT_DEGRADED = (
            f"neuronav: recovery FAILED — the config at "
            f"{cfg_path.as_posix()} raised: {e}. Fix it (or the embedding "
            "backend it names), then restart the session."
        )
        return _BOOT_DEGRADED


def _validate_foreign_config(cfg_path: Path, target: Path) -> dict:
    """A dir's pre-existing .neuronav/config.json is foreign input: a bad
    one must fail THIS call as an MCP error, not SystemExit the server
    (nav's load-time aborts are for config-file-driven runs)."""
    try:
        # utf-8-sig: a PowerShell-5-written config carries a BOM that a plain
        # utf-8 read would die on (issue #119)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(
            f"dir '{target.as_posix()}' has an unreadable config ({cfg_path}: {e}) "
            "— fix it or delete its .neuronav to re-onboard"
        ) from e
    if not isinstance(cfg, dict) or not cfg.get("state_dir"):
        raise ValueError(
            f"config '{cfg_path}' sets no \"state_dir\" — its default sits inside "
            f"the scanned root (issue #91). Set \"state_dir\": \"default\" to opt "
            "in (onboard.py init writes the opt-in), or delete .neuronav to "
            "re-onboard"
        )
    provider = str(cfg.get("embed_provider", "")).strip().lower()
    if provider and provider not in ("ollama", "openai"):
        raise ValueError(
            f"config '{cfg_path}': embed_provider '{provider}' is not "
            "'ollama' or 'openai'"
        )
    return cfg


def _heal_routed_drift() -> None:
    """Routed-scope freshness gate (issue #180): boot calls get
    _auto_rescan, but a dir onboarded once was served silently stale on
    every later routed call — its freshness path was only the explicit
    rescan(dir=...). config_scope already caches the stat-fingerprint
    slots per config, so the same TTL-cached gate extends here for free:
    dirty -> incremental rescan + graph/fns sync + re-baseline (self-
    heal, transparent to the call); embed-backend failure -> loud stderr
    note and an answer from the current index (the issue #19 contract —
    never a crashed call, never silent staleness)."""
    try:
        if not nav.stat_scan():
            return
        stats = nav.rescan()
        if stats["added"] or stats["updated"] or stats["deleted"]:
            _sync_chain(stats)
            print(
                f"neuronav: routed drift healed: files {stats['added']}/{stats['updated']}/"
                f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d) in "
                f"{nav.ROOT.as_posix()}",
                file=sys.stderr,
            )
        nav.stat_mark_synced()
    except Exception as e:
        print(
            f"neuronav: routed drift heal FAILED ({e}); answering from the "
            f"current index — call rescan(dir=\"{nav.ROOT.as_posix()}\") once "
            "the embedding backend is back",
            file=sys.stderr,
        )


def _first_contact() -> str | None:
    """Build the active scope's fresh store: tracked base shards first
    (import_base skips cleanly when absent), then the incremental rescan
    heals to the worktree. Returns None when the store already serves;
    else a rescan()-format summary so a long build reports progress the
    same way an explicit rescan does."""
    if nav._collection().count():
        _heal_routed_drift()
        return None
    nav.import_base()
    t0 = time.perf_counter()
    stats = nav.rescan()
    g, fns, note = _sync_chain(stats)
    nav.stat_mark_synced()
    return (
        f"onboarded {nav.ROOT.as_posix()} — index built: files "
        f"{stats['added']}/{stats['updated']}/{stats['unchanged']}/"
        f"{stats['deleted']} (a/u/u/d), fns {fns['fns_upserted']} upserted, "
        f"graph {len(g.files)} files, in {time.perf_counter() - t0:.1f}s{note}. "
        "Call again to query."
    )


@contextmanager
def _route(dir: str):
    """Serve this call under dir's index (issue #131). Yields None to run
    the tool body normally, or a prelude string when first contact built
    the index (the tool returns that instead). Serialized on _SCOPE_LOCK
    because nav's config globals are process-wide: routed calls must not
    interleave, and boot calls take the same lock so the watcher can
    never rescan a swapped config (RLock: _auto_rescan re-enters)."""
    with _SCOPE_LOCK:
        if not dir:
            if _BOOT_DEGRADED is not None:
                # issue #240: degraded boot — every tool answers the
                # first-call guidance; a config appearing mid-session
                # (pure-defaults boot) re-binds and builds first
                rec = _boot_recovery()
                if rec is not None:
                    yield rec
                    return
                yield _BOOT_DEGRADED
                return
            yield None
            return
        resolved = Path(dir).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(
                f"dir '{dir}' does not name a readable directory "
                f"(resolved: '{resolved.as_posix()}') — pass the target "
                "checkout's path"
            )
        cfg_path = resolved / ".neuronav" / "config.json"
        if not cfg_path.is_file():
            import onboard  # lazy: off the hot path by design

            onboard.scaffold(resolved)
        else:
            _validate_foreign_config(cfg_path, resolved)
        with nav.config_scope(cfg_path):
            yield _first_contact()


def _fmt(hits: list[dict]) -> str:
    """Format hybrid-recall hits: RRF-fused score, src provenance
    (vec/bm25/both), bidirectional 1-hop ctx labels."""
    if not hits:
        return "no results (index empty — call rescan first)"
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
        ctx = ", ".join(h.get("ctx") or [])
        lines.append(
            f"{h['score']:0.4f}  {h['file']}  src={h['src']}  ctx=[{ctx}]{tag}{tp}"
        )
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def explore(
    query: str,
    n: int = 4,
    anchor: str = "",
    orientation: bool = True,
    dir: str = "",
) -> str:
    """One-call orientation for "how does X work" questions.

    Seeds on the function-level vector index (lexical fallback when the
    embedding backend is down), returns Read-equivalent `cat -n` source
    slices with real line numbers, plus a callers/callees flow line per
    hit. Slices are capped at a 100-line window (issue #69); when a file
    continues past the window the slice ends with
    `... +N more lines - pass anchor="path:start-end" to continue` —
    call explore again with exactly that anchor string (query ignored)
    to page forward without re-querying. Weak hits become pointer lines
    instead of noise; total output is budget-capped so nothing
    externalizes to a file mid-answer.

    orientation=False (issue #125, repeat calls) skips the constant
    repo-map + cluster-map preamble and spends that budget on the file
    shortlist and slices instead.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        return _explore.run(query, n, anchor, orientation)


MAX_MAP_BUDGET = 8192
MIN_MAP_BUDGET = 256


def _here(g) -> str:
    """One-line you-are-here header: which checkout, how big, how many
    subsystems — stamped on orientation-tool responses so a client can
    always tell which project it is talking to."""
    n_clusters = len(nav.clusters())
    return f"you are here: {nav.ROOT.as_posix()} — {len(g.files)} files, {n_clusters} clusters"


@mcp.tool(annotations=READONLY)
def repo_map(budget_tokens: int = 2048, dir: str = "") -> str:
    """Token-budget repo map — the cheap orientation preamble.

    Aider-style: files ranked by structural PageRank (edge weight = wire
    count), each with its key signatures, tree-grouped by directory,
    truncated at the token budget. Call this first to learn the layout,
    then context(path) on any file that matters.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        budget = max(MIN_MAP_BUDGET, min(budget_tokens, MAX_MAP_BUDGET))
        g = graph.get_graph()
        return _here(g) + "\n" + graph.repo_map(budget_tokens=budget)


@mcp.tool(annotations=READONLY)
def semantic_search(
    query: str,
    n: int = 8,
    dir: str = "",
    two_pass: bool = False,
    graph_boost: float | None = None,
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
    never against find_functions' 0-1 cosine scale (issue #125).

    two_pass=True runs the RepoCoder second retrieve (issue #74: pass-1
    hits donate identifiers to one re-embedded augmented query; engaged
    rows are tagged 2pass). graph_boost rides the shipped recall
    default when omitted (λ 0.25, the #228 grid winner — 1-hop wire
    neighbors of top hits get a rank-decayed bump); pass 0.0 to disable
    and larger λ to strengthen; negative values are rejected loudly.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        n = max(1, min(n, 25))
        return _here(graph.get_graph()) + "\n" + _fmt(
            nav.search(query, n, two_pass=two_pass, graph_boost=graph_boost)
        )


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
        n = max(1, min(n, 15))
        try:
            hits = graph.find_functions(query, n)
        except Exception as exc:
            # issue #116: backend-down is recoverable — serve explore's
            # same-shape lexical fallback tagged degraded (never a raw
            # MCP error, which is worse when a degraded explore answer
            # has just recommended exactly this tool)
            why = nav.embed_failure_reason(exc)
            hits = _explore._lexical_fallback(query, n)
            if not hits:
                return f"degraded: {why} — no lexical match for {query!r} either"
            return "\n".join(
                [f"degraded: {why} — lexical fallback (substring, not semantic):"]
                + [
                    f"{h['score']:0.3f}  {h['path']}#{h['func']}:{h['line']}"
                    for h in hits
                ]
            )
        if not hits:
            return "no function index — call rescan first"
        return "\n".join(
            f"{h['score']:0.3f}  {h['path']}#{h['func']}:{h['line']}" for h in hits
        )


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
            return "no results (index empty — call rescan first)"
        matched: list[tuple[str, int, list[tuple[int, str]]]] = []
        scanned = 0
        total = 0
        for path in sorted(g.files):  # deterministic: path, then line order
            if glob and not fnmatch.fnmatch(path, glob):
                continue
            scanned += 1
            try:
                text = nav._read_text(nav.ROOT / path)
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


# symbol_graph output shaping (issue #125): the walk stays in graph.py
# (resolution + BFS), but its rendering truncated silently twice — rows
# capped at 8 names with no count, response cut at 40 LINES, i.e.
# mid-node. Rendering here follows explore's _flow line law instead:
# full counts on every row, "+N more" past the name cap, node-boundary
# cut + marker past the node cap. An agent checking "is removing this
# function safe?" must never read 8 callers as the total when there are 20.
SYMBOL_MAX_NODES = 13   # nodes per response (3 lines each + marker keeps
                        # the old 40-line discipline)
SYMBOL_ROW_NAMES = 8    # caller/callee names per row before "+N more"


def _sg_short(key: str) -> str:
    path, _, name = key.partition("::")
    return f"{path}#{name}"


def _sg_row(label: str, keys: list[str]) -> str:
    shown = ", ".join(_sg_short(k) for k in keys[:SYMBOL_ROW_NAMES]) or "-"
    more = (
        f" +{len(keys) - SYMBOL_ROW_NAMES} more"
        if len(keys) > SYMBOL_ROW_NAMES
        else ""
    )
    return f"    {label}: {len(keys)} ({shown}{more})"


def _symbol_view(g, symbol: str, depth: int) -> str:
    """graph.symbol_graph's walk rendered with visible truncation (issue
    #125): same resolution and BFS, but every row carries its true count
    and the response says when it cut. A total miss suggests difflib
    closest matches instead of dead-ending — context()'s precedent."""
    keys = g._resolve(symbol)
    if not keys:
        import difflib

        names = sorted({n for fs in g.files.values() for n in fs.funcs})
        by_lower = {n.lower(): n for n in names}
        close = difflib.get_close_matches(
            symbol.lower(), sorted(by_lower), n=3, cutoff=0.4
        )
        sug = (
            f" Closest matches: {', '.join(by_lower[c] for c in close)}"
            if close
            else ""
        )
        return f"no function matching '{symbol}'{sug}"
    blocks: list[str] = []
    seen: set[str] = set()
    frontier = set(keys)
    for _ in range(depth):
        nxt: set[str] = set()
        for key in sorted(frontier):
            if key in seen:
                continue
            seen.add(key)
            callers = sorted(g.reverse.get(key, ()))
            callees = sorted(g.edges.get(key, ()))
            blocks.append(
                f"{_sg_short(key)}\n"
                + _sg_row("callers", callers)
                + "\n"
                + _sg_row("callees", callees)
            )
            nxt |= {
                c for c in callees + callers
                if not c.endswith(graph.TSCN_SUFFIX)
            }
        frontier = nxt - seen
        if not frontier:
            break
    out = blocks[:SYMBOL_MAX_NODES]
    if len(blocks) > SYMBOL_MAX_NODES:
        out.append(
            f"… truncated at {SYMBOL_MAX_NODES} of {len(blocks)} nodes — "
            "pass depth=1 or a narrower symbol"
        )
    return "\n".join(out)


@mcp.tool(annotations=READONLY)
def symbol_graph(symbol: str, depth: int = 1, dir: str = "") -> str:
    """Structural map around a function or class: who calls it, what it calls.

    Wire-view of the repo: use it to trace call chains before refactoring,
    to check if removing a function is safe, or to understand a subsystem's
    shape. depth=2 gives one hop beyond direct neighbors. Pair with
    find_functions when you only know the concept, not the name.

    Resolution (issue #125, now documented): exact function name, then
    class name (all its methods), then case-insensitive substring — up to
    10 roots. Every row shows its true count with the first 8 names
    ("+N more" past that); the response caps at 13 nodes with a
    truncation marker; a total miss suggests closest matches.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        depth = max(1, min(depth, 3))
        return _symbol_view(graph.get_graph(), symbol, depth)


@mcp.tool(annotations=READONLY)
def dead_code(n: int = 40, dir: str = "") -> str:
    """Functions unreachable from any entry point — deletion candidates.

    Entry points: autoloads, virtuals (_ready/_process/...), signal handlers
    (code + .tscn connections), GUT tests, string-dispatched names. Tiers:
    'likely' (no dynamic dispatch in file — strong candidate) and 'review'
    (file uses call()/Callable()/connect() — verify manually). NEVER delete
    without reading the file and running tests.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        n = max(1, min(n, 100))
        g = graph.get_graph()
        res = g.dead_code(limit=n)
        lines = [
            f"dead-code candidates: {res['total']} total  "
            f"(likely: {res['by_tier'].get('likely', 0)}, "
            f"review: {res['by_tier'].get('review', 0)})",
            res["note"],
            "",
        ]
        for d in res["candidates"]:
            lines.append(f"[{d['tier']:6}] {d['path']}:{d['line']}  {d['func']}")
        return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def duplicates(n: int = 20, dir: str = "") -> str:
    """Duplicated function bodies (exact, whitespace/comment-normalized),
    across ALL indexed languages (.gd, .py, C++ sources/headers) —
    issue #116: the scan is not GDScript-only, so a Python or C++ repo
    no longer gets a false clean bill. `#` comments strip in the
    normalization (gd/py); C++ `//` comments compare as body text.

    Simplification targets: same logic living twice. Groups with 3+ members
    first. Cross-file groups are refactoring gold (extract shared helper);
    same-file groups are quick wins.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        n = max(1, min(n, 50))
        g = graph.get_graph()
        groups = g.exact_duplicates(limit=n)
        if not groups:
            scanned = sum(1 for fs in g.files.values() if fs.funcs)
            return f"no exact duplicates found ({scanned} files with functions scanned)"
        lines = [f"{len(groups)} duplicate group(s):", ""]
        for g in groups:
            lines.append(f"group {g['hash']} ({len(g['members'])} copies):")
            lines.extend(f"  - {m.replace('::', '#')}" for m in g["members"])
            lines.append("")
        return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def clusters(k: int = 6, min_sim: float = 0.6, dir: str = "") -> str:
    """Subsystem clusters discovered from embedding geometry (mutual kNN).

    Shows which files belong to the same feature family — UI, core systems,
    asset handling, networking. Use to survey unfamiliar areas or find
    every file related to a system before refactoring it. Returns cluster
    sizes with member paths + class names.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        k = max(2, min(k, 12))
        min_sim = max(0.4, min(min_sim, 0.85))
        cs = nav.clusters(k=k, min_sim=min_sim)
        if not cs:
            return "index empty — call rescan first"
        lines = [f"{len(cs)} cluster(s):", ""]
        for c in cs[:30]:
            label = c.get("label") or "misc"
            meta = f" [{c.get('method')}, conf {c.get('confidence', 0):.2f}]"
            lines.append(f"c{c['id']} {label} — {c['size']} files{meta}:")
            for path, _cls in c["paths"][:12]:
                lines.append(f"  res://{path}")
            if c["size"] > 12:
                lines.append(f"  … +{c['size'] - 12} more")
            lines.append("")
        return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def crosstalk(dir: str = "") -> str:
    """Coupling-hotspot report: which subsystem clusters are wired together.

    Counts structural (call/signal/var/instance) edges that CROSS cluster
    boundaries. Use before splitting/merging modules: a cluster with high
    external share is not self-contained; heavy cluster pairs are coupling
    hotspots. Pairs with `clusters` (what the families are) — this reports
    how leaky the boundaries are.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        import clusters as _clusters

        g = graph.get_graph()
        rep = _clusters.crosstalk(nav.clusters(), g)
        return _clusters.fmt_crosstalk(rep, top_n=2)


def _ctx_adjacency(g) -> tuple[dict, dict]:
    """File-level adjacency (both directions, per edge-type counts) and
    cross-file in-degree, aggregated once from the func-level edge set."""
    adj: dict[str, dict[str, dict]] = {}  # file -> nb -> {"->": t:n, "<-": t:n}
    indeg: dict[str, int] = {}
    for (s, d), tys in g.edge_types.items():
        sf, df = graph.split_key(s), graph.split_key(d)
        if sf == df:
            continue
        cell = adj.setdefault(sf, {}).setdefault(df, {">": {}, "<": {}})
        for t in tys:
            cell[">"][t] = cell[">"].get(t, 0) + 1
        cell = adj.setdefault(df, {}).setdefault(sf, {">": {}, "<": {}})
        for t in tys:
            cell["<"][t] = cell["<"].get(t, 0) + 1
        indeg[df] = indeg.get(df, 0) + len(tys)
    return adj, indeg


def _ctx_types(counts: dict[str, int]) -> str:
    return ", ".join(f"{t} x{n}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _ctx_semantic(path: str, k: int = 6) -> tuple[list[tuple[float, str]], str | None]:
    """Nearest files by embedding cosine — query with the file's own
    stored vector (no embed call, no new deps). Returns (rows, reason):
    reason is None on success — rows may then legitimately be empty when
    the file was never embedded — and carries the truthful backend
    failure otherwise (issue #116: an embed model mismatch must not
    masquerade as 'file not embedded — rescan first')."""
    try:
        col = nav._collection()
        got = nav.chroma_read(
            "ctx vectors", lambda: col.get(ids=[path], include=["embeddings"])
        )
        if not got["ids"]:
            return [], None
        res = nav.chroma_read(
            "ctx neighbors",
            lambda: col.query(
                query_embeddings=[got["embeddings"][0]],
                n_results=k + 1,
                include=["distances"],
            ),
        )
        return [
            (round(1.0 - float(d), 3), fid)
            for fid, d in zip(res["ids"][0], res["distances"][0])
            if fid != path
        ][:k], None
    except Exception as exc:
        return [], nav.embed_failure_reason(exc)


def _ctx_overview(g) -> str:
    """All-clusters overview: label, size, top members, external edges."""
    import clusters as _clusters

    cs = nav.clusters()
    _adj, indeg = _ctx_adjacency(g)
    ct = _clusters.crosstalk(cs, g)
    ext = {b["id"]: b["external_out"] + b["external_in"] for b in ct["by_cluster"]}
    clustered = {pp for c in cs for pp, _ in c["paths"]}
    unclustered = max(0, len(g.files) - len(clustered))
    lines = [
        f"clusters overview — {len(cs)} clusters, {len(g.files)} files "
        f"({unclustered} unclustered), "
        f'{ct["internal_edges"]} internal / {ct["external_edges"]} external edges '
        f'({ct["external_ratio"]:.0%} external)'
    ]
    for c in sorted(cs, key=lambda c: c["size"], reverse=True):
        top = sorted(
            ((indeg.get(pp, 0), pp) for pp, _ in c["paths"]), reverse=True
        )[:3]
        top_s = ", ".join(f"res://{pp} (in {v})" for v, pp in top)
        lines.append(
            f'c{c["id"]} "{c["label"]}" — {c["size"]} files '
            f'[{c["method"]}, conf {c["confidence"]:.2f}] '
            f'ext={ext.get(c["id"], 0)}'
        )
        if top_s:
            lines.append(f"    top: {top_s}")
    return "\n".join(lines)


CTX_DEFINES_CAP = 12  # names per kind in context()'s defines section


def _capped(names: list[str], cap: int) -> str:
    """First `cap` names, sorted by the caller, then an explicit +N more —
    the counts-everywhere discipline of explore's flow line (issue #125)."""
    shown = ", ".join(names[:cap])
    return shown + (f" +{len(names) - cap} more" if len(names) > cap else "")


def _render_defines(fs) -> list[str]:
    """What the file declares (issue #125): capped funcs/signals/members
    rows. The fresh-agent entry point used to emit every orientation
    fact EXCEPT the file's own API surface, forcing a blind read — this
    completes find_functions -> context -> read in one call. Funcs in
    definition order, signals/members sorted: deterministic."""
    lines: list[str] = []
    funcs = sorted(fs.funcs, key=lambda n: (fs.funcs[n].line, n))
    if funcs:
        lines.append(f"defines: {len(funcs)} func(s) — {_capped(funcs, CTX_DEFINES_CAP)}")
    if fs.signals:
        lines.append(
            f"  {len(fs.signals)} signal(s): "
            f"{_capped(sorted(fs.signals), CTX_DEFINES_CAP)}"
        )
    if fs.members:
        lines.append(
            f"  {len(fs.members)} member(s): "
            f"{_capped(sorted(fs.members), CTX_DEFINES_CAP)}"
        )
    return lines


def _render_membership(p: str, cs: list, indeg: dict[str, int]) -> list[str]:
    """Cluster block: label, confidence, this file's in-degree rank,
    top members."""
    lines: list[str] = []
    mine = next((c for c in cs if any(pp == p for pp, _ in c["paths"])), None)
    if mine is None:
        lines.append("cluster: unclustered")
    else:
        members = sorted(
            ((indeg.get(pp, 0), pp, cc) for pp, cc in mine["paths"]), reverse=True
        )
        my_rank = next(i for i, (_v, pp, _c) in enumerate(members, 1) if pp == p)
        lines.append(
            f'cluster: c{mine["id"]} "{mine["label"]}" (conf {mine["confidence"]:.2f}, '
            f'{mine["method"]}) — {mine["size"]} files, '
            f"this file ranks #{my_rank} by in-degree"
        )
        lines.append(f"  members (top {min(12, len(members))} by in-degree):")
        for v, pp, cc in members[:12]:
            cls = f" ({cc})" if cc else ""
            lines.append(f"    {v:>3}  res://{pp}{cls}")
        if len(members) > 12:
            lines.append(f"    … +{len(members) - 12} more")
    return lines


def _render_neighbors(p: str, adj: dict, indeg: dict[str, int], depth: int) -> list[str]:
    """Structural block: direct neighbors by edge type + direction,
    2-hop ring when depth >= 2."""
    lines = [f"structural neighbors (depth {depth}):"]
    if p not in adj:
        lines.append("  none — isolated file")
    else:
        direct = adj[p]
        entries = sorted(
            (
                (
                    sum(cell[">"].values()) + sum(cell["<"].values()),
                    nb,
                    cell[">"],
                    cell["<"],
                )
                for nb, cell in direct.items()
            ),
            key=lambda e: -e[0],
        )
        for _w, nb, out_t, in_t in entries[:15]:
            parts = []
            if out_t:
                parts.append("-> " + _ctx_types(out_t))
            if in_t:
                parts.append("<- " + _ctx_types(in_t))
            lines.append(f"  res://{nb}  {'  '.join(parts)}")
        if len(entries) > 15:
            lines.append(f"  … +{len(entries) - 15} more")
        if depth >= 2:
            seen = {p} | set(direct)
            hop2: dict[str, str] = {}
            for nb in direct:
                for nb2 in adj.get(nb, {}):
                    if nb2 not in seen and nb2 not in hop2:
                        hop2[nb2] = nb
            if hop2:
                h2 = sorted(hop2.items(), key=lambda kv: -indeg.get(kv[0], 0))[:10]
                lines.append(f"  2-hop ({len(hop2)} files, top {len(h2)} by in-degree):")
                for f2, via in h2:
                    lines.append(f"    res://{f2}  via res://{via}")
    return lines


def _render_semantic(p: str) -> list[str]:
    """Embedding-cosine neighbor block (empty -> rescan hint; backend
    failure -> degraded line with the true reason)."""
    lines = ["semantic neighbors (cosine):"]
    sem, err = _ctx_semantic(p)
    if err:
        lines.append(f"  degraded: {err}")
    elif not sem:
        lines.append("  n/a (file not embedded — rescan first)")
    else:
        for s, fid in sem:
            lines.append(f"  {s:.3f}  res://{fid}")
    return lines


def _render_hub(p: str, g, indeg: dict[str, int]) -> list[str]:
    """Hub status: in-degree + percentile rank over all files."""
    lines: list[str] = []
    ind = indeg.get(p, 0)
    ranked = sorted(g.files, key=lambda f: -indeg.get(f, 0))
    rank = ranked.index(p) + 1
    total = len(ranked)
    if ind:
        lines.append(
            f"hub: in-degree {ind} — rank {rank} of {total} files "
            f"(top {100.0 * rank / total:.0f}%)"
        )
    else:
        lines.append(f"hub: in-degree 0 — rank {rank} of {total} files (leaf)")
    return lines


@mcp.tool(annotations=READONLY)
def context(path: str = "", depth: int = 1, dir: str = "") -> str:
    """Subsystem map for one repo file — the orientation tool for agents.

    Fresh-agent entry point: pass a res:// path (or repo-relative) and get
    a text map — what it defines (funcs/signals/members, capped with
    "+N more"), its cluster (label, confidence, member hubs by in-degree),
    structural neighbors grouped by edge type (call/signal/var/attach/inst
    with counts and direction, depth 1-3), top semantic neighbors (embedding
    cosine), and hub status (in-degree rank). Called with no path, returns
    the all-clusters overview instead (label, size, top members, external
    edges). Build from existing clusters + graph + vector index; no new deps.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        depth = max(1, min(depth, 3))
        p = path.strip()
        g = graph.get_graph()
        if not p:
            return _ctx_overview(g)
        # res:// is the gdscript project-root scheme — strip via the
        # registry helper, not a local literal
        p = res_to_rel(p)
        p = p.replace("\\", "/").lstrip("/")
        if p not in g.files:
            import difflib

            close = difflib.get_close_matches(p, list(g.files), n=3, cutoff=0.4)
            sug = f" Closest matches: {', '.join(close)}" if close else ""
            return f"unknown file: {p} — pass a repo-relative or res:// path, or rescan first.{sug}"
        fs = g.files[p]
        adj, indeg = _ctx_adjacency(g)
        if fs.class_name and fs.extends:
            tag = f"{fs.class_name} extends {fs.extends}"
        else:
            tag = fs.class_name or fs.extends or fs.ext
        lines = [f"res://{p}  [{tag}]"]
        lines += _render_defines(fs)
        lines += _render_membership(p, nav.clusters(), indeg)
        lines += _render_neighbors(p, adj, indeg, depth)
        lines += _render_semantic(p)
        lines += _render_hub(p, g, indeg)
        return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def visualize(dir: str = "") -> str:
    """Generate the interactive 3D code-graph (rotatable neuron map).

    Nodes = files (colored by subsystem cluster, red-tinted when they contain
    dead-code candidates), edges = calls/instancing/signals. Search box,
    cluster filter chips, dead-code toggle, click for connections.
    Returns the bake path + its openable file:// URI — the file is fully
    self-contained and boots directly in a browser (issue #133). Regenerate
    after rescan if the graph changed materially.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact; the graph bakes into that dir's .neuronav).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        try:
            import viz
        except ImportError:
            return ("viz add-on not installed — delete-able surface is viz.py + vendor/ + "
                    "tools/serve.py; core tools (search/repo_map/context/...) work without it. "
                    "Restore viz.py to re-enable the bake.")

        out = viz.ensure_bake()
        # production = open the self-contained bake directly (file://);
        # serve.py exists for headless dev rigs only, never auto-launched
        # from a stdio server (issue #133).
        return (
            f"3D graph written to {out}. Open the fully self-contained bake "
            f"directly: {out.as_uri()}"
        )


# ---- auto-rescan freshness gate (issue #19) --------------------------------
# Every read tool calls _auto_rescan() on entry: nav's stat fingerprint
# (mtime/size walk, TTL-cached) is compared against the last synced
# baseline, and a drifted worktree triggers the sha-gated nav.rescan() +
# graph/fns sync before the tool answers. Failures never crash the call:
# one stderr warning, a RESCAN_COOLDOWN_S retry suppression, and the
# tool answers from the current index (the recall degraded-mode
# precedent). Config watch_interval_s > 0 adds a daemon thread that
# polls the same fingerprint and rescans without tool traffic.

RESCAN_COOLDOWN_S = 60.0  # auto-rescan retry suppression after a failure
WATCH_DEBOUNCE_S = 2.0  # writer-quiet window before a watcher rescan
WATCH_DEBOUNCE_MAX_S = 30.0  # cap on waiting for the writer to quiet down

# issue #203/#206: ONE bounded store-lock wait, shared by boot and the
# auto-rescan gate below — a stale holder (a dead session's MCP server)
# aborts loudly once this bound expires instead of queueing forever,
# whether that queue would stall a boot or wedge the watcher thread
LOCK_WAIT_S = 60.0


def _bounded_rescan() -> dict[str, int]:
    """The one bounded rescan (issues #203/#206): boot and _auto_rescan
    ride the same wait bound and the same loud abort — nav names the
    lock file and the likely holder when the wait expires, so no path
    copies the boot's inline acquire into a private twin."""
    return nav.rescan(timeout=LOCK_WAIT_S)


_rescan_busy = threading.Lock()  # in-flight trigger (cross-process is nav._db_lock's job)
_rescan_failed_at: float | None = None  # monotonic; None = healthy


def _cooldown_active() -> bool:
    return (
        _rescan_failed_at is not None
        and time.monotonic() - _rescan_failed_at < RESCAN_COOLDOWN_S
    )


def _auto_rescan() -> None:
    """Read-tool freshness gate: stat-scan -> dirty ? incremental rescan +
    graph/fns sync + baseline update. Never raises.

    BOOT-config only (issue #131): routed (dir=) calls skip the gate —
    the fingerprint slots and cooldown are process-global, and the alt
    dir's freshness path is the explicit rescan(dir=...). Serialized on
    _SCOPE_LOCK so the watcher can never rescan against a config a
    routed call swapped in (reentrant: boot tool calls arrive holding
    the lock from _route)."""
    global _rescan_failed_at
    if not _at_boot():
        return
    with _SCOPE_LOCK:
        if _cooldown_active() or not _rescan_busy.acquire(blocking=False):
            return  # failed recently, or another trigger is already mid-rescan
        try:
            try:
                if not nav.stat_scan():
                    return
                stats = _bounded_rescan()
                if stats["added"] or stats["updated"] or stats["deleted"]:
                    _sync_chain(stats)
                    print(
                        f"neuronav: auto-rescan: files {stats['added']}/{stats['updated']}/"
                        f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d)",
                        file=sys.stderr,
                    )
                nav.stat_mark_synced()
                _rescan_failed_at = None
            except SystemExit as e:
                # issue #206: nav's bounded-lock wait (and its zero-file
                # walk abort) exit the process by design at boot — in the
                # auto-rescan path they must degrade to the #19 law
                # instead of killing the watcher thread: the abort text
                # names the lock file and likely holder, the cooldown
                # suppresses the retry, tools keep answering from the
                # current index
                _rescan_failed_at = time.monotonic()
                print(
                    f"{e} Auto-rescan skipped this round — serving the "
                    f"current index, retry suppressed for "
                    f"{RESCAN_COOLDOWN_S:.0f}s.",
                    file=sys.stderr,
                )
            except Exception as e:  # embedding backend down etc: degrade loudly
                _rescan_failed_at = time.monotonic()
                print(
                    f"neuronav: auto-rescan FAILED ({e}); answering from the current "
                    f"index, retry suppressed for {RESCAN_COOLDOWN_S:.0f}s",
                    file=sys.stderr,
                )
        finally:
            _rescan_busy.release()


def _wait_quiet() -> None:
    """Sleep until the fingerprint stops changing (bounded): rescan a
    coherent tree, not a writer's half-saved state."""
    last = nav.stat_fingerprint()
    deadline = time.monotonic() + WATCH_DEBOUNCE_MAX_S
    while time.monotonic() < deadline:
        time.sleep(WATCH_DEBOUNCE_S)
        cur = nav.stat_fingerprint()
        if cur == last:
            return
        last = cur


def _watch_loop(interval: float) -> None:
    """Poll the stat fingerprint every interval; on a dirty transition let
    the writer go quiet, then run the shared gate (which re-checks
    dirtiness and cooldown before rescanning)."""
    while True:
        time.sleep(interval)
        try:
            dirty = nav.stat_scan(force=True)
        except Exception as e:
            print(f"neuronav: watch scan failed ({e}); continuing", file=sys.stderr)
            continue
        if dirty:
            _wait_quiet()
            _auto_rescan()


def _start_watcher(interval: float) -> threading.Thread:
    """Daemon polling watcher — stdlib only (threading/time/os in nav).

    The lambda (not ``target=_watch_loop``) keeps the callee a visible
    call for the static graph: bare callback refs are invisible to it."""
    t = threading.Thread(
        target=lambda: _watch_loop(interval), daemon=True, name="neuronav-watch"
    )
    t.start()
    return t


def _sync_chain(stats: dict) -> tuple[object, object, str]:
    """nav.rescan -> graph rebuild -> fns sync. fns failures degrade
    gracefully (dirty marker self-heals next run) instead of killing the
    server or blocking file search."""
    g = graph.get_graph(rebuild=True)
    note = ""
    try:
        fns = graph.sync_functions(
            stats.get("changed", []), stats.get("deleted_paths", [])
        )
    except Exception as e:  # fns stale-but-healing; file search unaffected
        fns = {"fns_upserted": 0}
        note = f" [fns sync FAILED, will self-heal next rescan: {e}]"
    return g, fns, note


RESCAN_PATH_CAP = 10  # changed/deleted paths listed before "+N more"


def _rescan_paths(stats: dict) -> str:
    """Capped changed/deleted listing (issue #125): 'what changed since I
    last looked' is the top reorientation question, and nav already
    carries the lists in stats — the wire output dropped them. Sorted for
    display only; stats keeps walk order for sync_functions."""
    lines = []
    for label, paths in (
        ("changed", stats.get("changed", [])),
        ("deleted", stats.get("deleted_paths", [])),
    ):
        if paths:
            lines.append(
                f"{label} ({len(paths)}): {_capped(sorted(paths), RESCAN_PATH_CAP)}"
            )
    return "\n" + "\n".join(lines) if lines else ""


@mcp.tool()
def memory(verb: str, name: str = "", body: str = "", dir: str = "") -> str:
    """Project memories — durable notes kept across sessions (issue #67).

    Memories answer "what we LEARNED" where the index answers "what IS":
    the flaky test, the deploy entry point, the quirk that cost a day.
    Plain markdown files in .neuronav/memories/ (this repo's project
    store — a dir= routes to that project's memories).

    Verbs:
    - "list": names + one-line summaries, sorted by name
    - "get": one memory's full body (name required) — verbatim
    - "set": create/overwrite (name + body required). The file starts
      with "# <name>"; the body is stored byte-verbatim (UTF-8, LF,
      atomic write) — start the body with a one-line
      "<!-- summary -->" comment to give list a summary line
    - "delete": remove (name required); a missing name fails loud

    Names are one safe filename component: 1-128 chars of
    letters/digits/._- starting alphanumeric (path shapes like "../x"
    are refused). This tool mutates state, like rescan — no read-only
    hint.
    """
    with _route(dir) as prelude:
        out = memories.run(verb, name, body)
        # a routed fresh dir onboards mid-call; unlike read tools we do
        # NOT return the prelude alone — dropping a mutating op to
        # report the build would silently lose the write
        return f"{prelude}\n{out}" if prelude else out

@mcp.tool()
def rescan(dir: str = "") -> str:
    """Re-index changed/new/deleted files: vectors, function index, graph.

    Fast when nothing changed. Run after pulling, branching, or mass
    edits. Read tools also auto-rescan on worktree drift (stat-gated,
    mtime/size fingerprint) — the boot project only; this explicit
    variant is also the freshness path for a non-boot dir (issue #131).

    When files changed, a capped changed/deleted path list (10 shown,
    "+N more" past it) follows the summary line.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (a fresh dir onboards on first contact).
    """
    with _route(dir) as prelude:
        # issue #41 law: the explicit rescan TOOL stays loud on a 0-file
        # walk — so the degraded-boot guidance (yielded by identity) is
        # NOT returned; we fall through to nav.rescan(), whose
        # RuntimeError names root/extensions. A mid-session recovery
        # prelude (a different string) still returns.
        if prelude and prelude is not _BOOT_DEGRADED:
            return prelude
        t0 = time.perf_counter()
        stats = nav.rescan()
        g, fns, note = _sync_chain(stats)
        nav.stat_mark_synced()
        dt = time.perf_counter() - t0
        return (
            f"rescan: files {stats['added']}/{stats['updated']}/"
            f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d), "
            f"fns {fns['fns_upserted']} upserted, graph {len(g.files)} files, "
            f"in {dt:.1f}s{note}"
            + _rescan_paths(stats)
        )


def main() -> None:
    """Console-script boot (issue #204) — the historic ``__main__`` body
    behind the ``neuronav-mcp`` entry point, plus the #203 hardening:
    boot state lands on stderr BEFORE any rescan work, and the boot
    lock wait is bounded. #240: a 0-file walk degrades to first-call
    guidance instead of dying pre-handshake, and one 1-token embed
    probe fails fast with the fix in the message. Behavior otherwise
    identical to the direct ``python server.py`` boot."""
    t0 = time.perf_counter()
    # issue #203: first contact must never be silent — name the resolved
    # config (or the pure-defaults root) before the boot rescan starts,
    # so a long first-contact build is visible from its first second
    if nav.CONFIG_PATH is not None:
        print(f"neuronav: config {nav.CONFIG_PATH}", file=sys.stderr)
    else:
        print(f"neuronav: pure defaults, root={nav.ROOT}", file=sys.stderr)
    # issue #240: what the root actually holds, extension filter off —
    # the 0-file verdict, the degraded-boot guidance and the raw-text
    # banner all read this one census
    census = nav.suffix_census()
    probe_fail = _probe_embedder()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    fns_up = 0
    watch_note = ""
    if not any(s in nav.EXTS for s in census):
        _enter_degraded(census, probe_fail, "boot walk matched 0 files")
    elif probe_fail is not None:
        if nav.count() == 0:
            # evidence-based abort (issue #240): an empty store needs
            # embeds to build — every path from here fails mid-rescan.
            # Die pre-handshake with the fix in the message instead.
            raise SystemExit(
                f"neuronav: {probe_fail} The store is empty and every "
                "index build embeds — aborting before the handshake so "
                "the failure carries the fix. Pull the model / start the "
                "backend, then restart the session."
            )
        # warm store: serve it degraded (the #19 law already covers
        # embed failures mid-serve); skip the boot rescan — it would
        # die on the first new embed
        print(
            f"neuronav: {probe_fail} Serving the warm index degraded; "
            "rescans that need new embeddings retry with the tool-call "
            "cooldown until the backend is back.",
            file=sys.stderr,
        )
        graph.get_graph(rebuild=True)
    else:
        try:
            stats = _bounded_rescan()
        except RuntimeError:
            # walk emptied between census and rescan — same degraded path
            _enter_degraded(
                nav.suffix_census(), None, "boot rescan found the walk empty"
            )
        else:
            g, fns, _ = _sync_chain(stats)
            nav.stat_mark_synced()
            fns_up = fns["fns_upserted"]
            _raw_text_banner(census)
            # boot config only by design (issue #131): the watcher drives
            # _auto_rescan, which is boot-gated — routed dirs refresh
            # explicitly
            if nav.WATCH_INTERVAL_S > 0:
                _start_watcher(nav.WATCH_INTERVAL_S)
                watch_note = f", watcher {nav.WATCH_INTERVAL_S:g}s"
    # warm the clusters stack (networkx/numpy/sklearn/scipy) on the main
    # thread before the event loop serves: importing these C extensions
    # lazily inside a fastmcp tool call (on the anyio loop thread) blocks
    # the stdio server indefinitely on Windows — clusters/context/crosstalk
    # all ride these imports
    import networkx  # noqa: F401
    import numpy  # noqa: F401
    import scipy.cluster.hierarchy  # noqa: F401
    import sklearn.cluster  # noqa: F401
    if _BOOT_DEGRADED is not None:
        state = "DEGRADED, guidance mode, "
    elif probe_fail is not None:
        state = "embed probe failed, serving warm index, "
    else:
        state = f"fns {fns_up}, "
    print(
        f"neuronav: startup files {stats['added']}/{stats['updated']}/"
        f"{stats['unchanged']}/{stats['deleted']}, "
        f"{state}"
        f"in {time.perf_counter() - t0:.1f}s{watch_note}",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
