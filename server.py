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

import anyio  # stdio loop primitives: async tool shells run bodies off-loop (#315)
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

import explore as _explore
import graph
from extractors import PRESETS, registry_for, res_to_rel  # noqa: E402
import memories
import nav
import recall

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
    "search_text regex-greps the indexed files; context(path) is the "
    "one-file orientation dossier (cluster, neighbors, defines); "
    "clusters / crosstalk / arch_check map the subsystems themselves — "
    "communities, their coupling hotspots, project-rule violations — "
    "before a multi-file refactor; symbol_graph / dead_code / duplicates "
    "for structure questions; impact(symbol) for the pre-refactor "
    "blast-radius check — run it before renaming or removing anything; "
    "visualize opens the graph.html bake. A large first index build or "
    "bake never blocks silently (issue #315): progress flows as "
    "notifications/progress + stderr lines, the "
    "neuronav://onboarding/status resource is the pollable state, "
    "visualize answers 'queued' with the live phase, and reads served "
    "from a partially-built index are tagged stale: true. Tools are "
    "read-only except rescan (forces reindex) and memory "
    "(set/get/list/delete persistent project notes - save durable "
    "findings there, not transient state). "
    "Every tool takes an optional dir to target a different repo root. "
    "The index auto-refreshes on file drift; a tool marked 'degraded' "
    "still answers completely from the current index, though vector "
    "recall may be unavailable. An empty index answers with first-call "
    "guidance; onboard.py init --preset " + "|".join(PRESETS) + " "
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
# The two mutators carry the rest of the annotation vocabulary
# (spec: the hints below are meaningful only when readOnlyHint is
# false, which is exactly the mutators). memory set overwrites and
# delete removes durable notes -> destructiveHint; rescan only
# rebuilds derived caches and converges -> additive-only +
# idempotent (issue #253).
MUTATING_MEMORY = ToolAnnotations(destructiveHint=True)
MUTATING_RESCAN = ToolAnnotations(destructiveHint=False,
                                  idempotentHint=True)

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
# ---- handshake-first boot (issue #273) ----------------------------------------
# The harness must connect near-instantly: connection time may not include
# opening/building the chroma store, the embed probe, the boot rescan, or
# the warm C-extension imports. main() therefore enters the stdio loop
# immediately and _boot_sequence does that work on a daemon thread; every
# tool call passes through _await_boot() (via _route) before touching nav
# state. The wait is bounded — a wedged boot fails the call loudly instead
# of hanging the session — and a fatal boot sets _BOOT_FATAL so waiters
# raise the reason.
_BOOT_READY = threading.Event()
_BOOT_FATAL: str | None = None
_BOOT_WAIT_S = 300.0
_BOOT_T0 = 0.0  # monotonic start of the boot thread (issue #315 grace window)
_BOOT_THREAD: threading.Thread | None = None
_BOOT_START_LOCK = threading.Lock()


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
    first hit in a fixed preference order — the insertion order of
    extractors.PRESETS (ts before js, so a mixed web repo suggests the
    fuller list). None when no preset applies."""
    for name in PRESETS:
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
        head = _capped(
            [f"{s} x{c}" for s, c in ranked], _GUIDANCE_SUGGEST_CAP
        )
        lines.append(f"  file types on disk (same walk, any suffix): {head}")
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
                "for these files until extractors land for them"
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
        "(onboard.py init --preset ts|js|python|cpp|gdscript|rust curates "
        "extensions; these files index as raw text until extractors "
        "land for them)",
        file=sys.stderr,
    )


def _enter_degraded(census: dict[str, int], probe_fail: str | None,
                    why: str) -> None:
    """Flip the boot into guidance mode (issue #240): set the flag every
    tool answers with, plus the stderr banner."""
    global _BOOT_DEGRADED
    _BOOT_DEGRADED = _boot_guidance(census, probe_fail)
    # no build is running in guidance mode; keep the status resource clean
    _progress_set("done", note="degraded boot — guidance mode")
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
    A failed attempt must not brick the session (issue #292): the
    re-bind rolls fully back, so the CONFIG_PATH guard below stays a
    retry latch instead of a one-way tripwire — once the transient
    cause (embed blip, held store lock) is gone, the next call
    recovers. Returns the prelude the call answers with, or None when
    still degraded."""
    global _BOOT_DEGRADED, _BOOT_STORE
    if nav.CONFIG_PATH is not None:
        return None
    cfg_path = Path.cwd() / ".neuronav" / "config.json"
    if not cfg_path.is_file():
        return None
    _validate_foreign_config(cfg_path, Path.cwd())
    # the pre-recovery state, restored verbatim on failure: nav's
    # pure-defaults globals, the graph singleton (a mid-sync failure
    # must not leave it serving the candidate store), and the env
    # use_config exports for subprocesses
    saved_cfg = {f: getattr(nav, f) for f in nav._CONFIG_FIELDS}
    saved_graph = graph._graph
    try:
        nav.use_config(cfg_path)
        stats = _bounded_rescan()
        if stats["added"] or stats["updated"] or stats["deleted"]:
            _progress_set(
                "graph",
                note=f"rebuilding graph — {len(stats.get('changed', []))} changed",
            )
        g, fns, note = _sync_chain(stats)
        _progress_set("done", note="recovery complete")
        nav.stat_mark_synced()
        _raw_text_banner(nav.suffix_census())
        # only success re-points the boot identity: a failure past
        # use_config must leave _at_boot() describing the store the
        # session actually serves
        _BOOT_STORE = (str(nav.STATE_DIR), nav.COLLECTION)
        _BOOT_DEGRADED = None
        return (
            f"config appeared mid-session — rebound the boot to "
            f"{cfg_path.as_posix()} and indexed: files "
            f"{stats['added']}/{stats['updated']}/{stats['unchanged']}/"
            f"{stats['deleted']} (a/u/u/d), fns {fns['fns_upserted']} "
            f"upserted, graph {len(g.files)} files{note}. Call again to query."
        )
    except (Exception, SystemExit) as e:
        # SystemExit first-class: nav's lock-timeout abort escapes
        # `except Exception` and would kill the caller's thread
        # mid-recovery (issue #292). The rollback below is what makes
        # the guard above retryable: without it the first failure left
        # nav bound to the candidate config and every later call
        # short-circuited to the guidance until process restart.
        for f, v in saved_cfg.items():
            setattr(nav, f, v)
        # pre-recovery the env var cannot have been set: a boot with
        # NEURONAV_CONFIG bound has CONFIG_PATH set and never gets here
        os.environ.pop("NEURONAV_CONFIG", None)
        graph._graph = saved_graph
        _BOOT_DEGRADED = (
            f"neuronav: recovery FAILED — the config at "
            f"{cfg_path.as_posix()} raised: {e}. Fix it (or the embedding "
            "backend it names); the next call retries the recovery."
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
        stats = _bounded_rescan()
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
    stats = _bounded_rescan()
    if stats["added"] or stats["updated"] or stats["deleted"]:
        _progress_set("graph", note=f"rebuilding graph — {len(stats.get('changed', []))} changed")
    g, fns, note = _sync_chain(stats)
    _progress_set("done", note="first contact complete")
    nav.stat_mark_synced()
    return (
        f"onboarded {nav.ROOT.as_posix()} — index built: files "
        f"{stats['added']}/{stats['updated']}/{stats['unchanged']}/"
        f"{stats['deleted']} (a/u/u/d), fns {fns['fns_upserted']} upserted, "
        f"graph {len(g.files)} files, in {time.perf_counter() - t0:.1f}s{note}. "
        "Call again to query."
    )


# ---- build observability (issue #315) --------------------------------------
# First contact with a large repo is minutes of silent embed/graph/bake
# work; before #315 every MCP call during it surfaced as one
# indistinguishable client timeout. Three channels now carry the story,
# all fed from this one snapshot:
#   * notifications/progress — the async tool shells forward snapshots
#     to tokened clients while their body blocks (boot, rescan, bake)
#   * the neuronav://onboarding/status resource — pollable, never gated
#   * stderr heartbeats — "neuronav: indexing 1500/3223 (eta ~83s)",
#     the rescan-style lines an operator can tail
_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict[str, object] = {
    "phase": "idle", "count": 0, "total": 0,
    "started": 0.0, "note": "",
}
_PROGRESS_LAST_PRINT = 0.0  # monotonic; stderr heartbeats are rate-limited
PROGRESS_HEARTBEAT_S = 5.0  # stderr heartbeat floor
PROGRESS_STALE_GRACE_S = 15.0  # stale reads engage only past this build age
PROGRESS_POLL_S = 2.0  # async-shell notification cadence


def _progress_set(phase: str, count: int = 0, total: int = 0, note: str = "") -> None:
    """One writer API for every build phase. Phase transitions and the
    rate-limited heartbeat go to stderr (loud, tail-able); the snapshot
    feeds the resource and the notification pollers."""
    global _PROGRESS_LAST_PRINT
    now = time.monotonic()
    with _PROGRESS_LOCK:
        prev = _PROGRESS["phase"]
        if phase not in ("idle", "done"):
            _PROGRESS.update(
                phase=phase, count=count, total=total, note=note, started=now
            )
        else:
            _PROGRESS.update(phase=phase, count=count, total=total, note=note)
    if phase != prev:
        print(f"neuronav: build phase -> {phase}", file=sys.stderr)
    line = _progress_line()
    if line and now - _PROGRESS_LAST_PRINT >= PROGRESS_HEARTBEAT_S:
        _PROGRESS_LAST_PRINT = now
        print(f"neuronav: {line}", file=sys.stderr)


def _progress_snapshot() -> dict[str, object]:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


def _progress_line() -> str:
    """Human one-liner for the current build phase ('' when idle)."""
    s = _progress_snapshot()
    phase = s["phase"]
    if phase in ("idle", "done"):
        return ""
    elapsed = max(0.0, time.monotonic() - s["started"])
    count, total = s["count"], s["total"]
    parts = [f"{phase} {count}/{total}" if total else f"{phase} ({elapsed:.0f}s in)"]
    if total and count:
        parts.append(f"{100 * count / total:.0f}%")
        if count < total and elapsed > 0.5:
            parts.append(f"eta ~{(total - count) * elapsed / count:.0f}s")
    if s["note"]:
        parts.append(s["note"])
    return ", ".join(parts)


def _nav_progress_hook(phase: str, count: int, total: int) -> None:
    """nav's rescan/first-contact embed batches -> the snapshot (the one
    registered observer; boot thread, watcher and tool bodies all report
    through nav's hooks, so every embed loop updates this in one place)."""
    _progress_set("embed", count=count, total=total)


nav.PROGRESS_HOOKS.append(_nav_progress_hook)


def _boot_building() -> bool:
    """True while the boot thread is still running its first pass and no
    fatality is on record (the states where tool calls park on the gate)."""
    return (
        _BOOT_THREAD is not None
        and not _BOOT_READY.is_set()
        and _BOOT_FATAL is None
    )


def _stale_prelude() -> str | None:
    """Issue #315: while the boot build runs and the store already holds
    vectors, the two pure-read store views answer from the partially
    built index, honestly tagged stale — instead of parking the client
    on the boot gate past its timeout. The other tools stay gated: they
    rebuild shared derived state (graph singleton, cluster memo) whose
    mid-build mutation is not thread-safe. Returns None when the build
    is not the blocker, or inside the grace window (short builds simply
    deserve the fresh answer)."""
    if not _boot_building():
        return None
    if _BOOT_T0 and time.monotonic() - _BOOT_T0 < PROGRESS_STALE_GRACE_S:
        # short builds stay invisible: the caller blocks a beat on the boot
        # gate and gets the fresh answer. stale reads engage only when the
        # build is genuinely long enough to threaten client timeouts.
        return None
    try:
        have = nav._collection().count() > 0
    except Exception:  # noqa: BLE001 — store mid-open: fall back to the gate
        return None
    if not have:
        return None
    line = _progress_line() or "first index build in progress"
    return (
        f"stale: true — {line}; this answer reads the partially-built "
        "index (vector ranks only — lexical fusion lands when the build "
        "completes). Poll neuronav://onboarding/status or re-call after."
    )


async def _serve(body, ctx: Context | None) -> str:
    """Async shell for the #315 tools: run the sync tool body OFF the
    anyio loop (the loop must stay free to flush progress notifications
    and peer requests — a sync body parked on the boot gate wedges every
    concurrent call), while a poller forwards build snapshots to clients
    that sent a progressToken. report_progress is a no-op without one,
    so untokened clients lose nothing — the resource and stderr carry
    the same line."""
    done = anyio.Event()

    async def _poll() -> None:
        last = ""
        while not done.is_set():
            line = _progress_line()
            if line and line != last and ctx is not None:
                s = _progress_snapshot()
                try:
                    await ctx.report_progress(
                        s["count"], s["total"] or None,
                        message=f"neuronav: {line}",
                    )
                except Exception:  # noqa: BLE001 — client hung up: answer anyway
                    pass
            last = line
            with anyio.move_on_after(PROGRESS_POLL_S):
                await done.wait()

    raised: BaseException | None = None
    result: str = ""
    async with anyio.create_task_group() as tg:
        tg.start_soon(_poll)
        try:
            result = await anyio.to_thread.run_sync(body)
        except BaseException as e:  # stash: the task group must close clean
            raised = e  # issue #315: SystemExit (nav's lock-timeout abort)
        finally:  # must surface UNWRAPPED — a BaseExceptionGroup would mask
            done.set()  # it from callers (test_bootrecovery pins this)
    if raised is not None:
        raise raised
    return result


def _await_boot() -> None:
    """Handshake-first gate (issue #273): block this tool call until the
    boot thread finished (store open/build, graph, watcher). Waiting on
    the anyio loop thread is safe — Event.wait releases the GIL. An
    in-process import that never ran main() has no boot work to wait
    for — the gate closes over whatever nav state the host set up, so
    tests and scripts keep the pre-#273 semantics (no surprise boot
    rescan racing their fixtures); only main() starts the boot thread.
    Bounded and loud either way: timeout or _BOOT_FATAL raise, never a
    silent hang."""
    if _BOOT_THREAD is None:
        _BOOT_READY.set()
        return
    if _BOOT_READY.is_set():
        if _BOOT_FATAL is not None:
            raise ValueError(f"neuronav: boot failed fatally — {_BOOT_FATAL}")
        return
    if not _BOOT_READY.wait(timeout=_BOOT_WAIT_S):
        raise ValueError(
            f"neuronav: boot still incomplete after {_BOOT_WAIT_S:g}s — "
            "see the neuronav: stderr lines (embed backend or store "
            "lock?); restart the session if it is wedged."
        )
    if _BOOT_FATAL is not None:
        raise ValueError(f"neuronav: boot failed fatally — {_BOOT_FATAL}")


@contextmanager
def _route(dir: str):
    """Serve this call under dir's index (issue #131). Yields None to run
    the tool body normally, or a prelude string when first contact built
    the index (the tool returns that instead). Serialized on _SCOPE_LOCK
    because nav's config globals are process-wide: routed calls must not
    interleave, and boot calls take the same lock so the watcher can
    never rescan a swapped config (RLock: _auto_rescan re-enters)."""
    _await_boot()  # issue #273: never touch nav state mid-boot
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


MAX_MAP_BUDGET = 8192
MIN_MAP_BUDGET = 256


def _here(g) -> str:
    """One-line you-are-here header: which checkout, how big, how many
    subsystems — stamped on orientation-tool responses so a client can
    always tell which project it is talking to."""
    n_clusters = len(nav.clusters())
    return f"you are here: {nav.ROOT.as_posix()} — {len(g.files)} files, {n_clusters} clusters"


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
                nav.search(query, k, two_pass=two_pass, graph_boost=graph_boost)
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
    shown = _capped(
        [_sg_short(k) for k in keys], SYMBOL_ROW_NAMES
    ) or "-"
    return f"    {label}: {len(keys)} ({shown})"


def _symbol_view(g, symbol: str, depth: int) -> str:
    """graph.symbol_graph's walk rendered with visible truncation (issue
    #125): same resolution and BFS, but every row carries its true count
    and the response says when it cut. A total miss suggests difflib
    closest matches instead of dead-ending — context()'s precedent."""
    keys = g._resolve(symbol)
    if not keys:
        return _miss_view(g, symbol)
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


def _miss_view(g, symbol: str) -> str:
    """A resolution miss rendered as closest-match suggestions, not a
    dead end — the context() precedent _symbol_view set (issue #125);
    impact shares the shape (issue #280)."""
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


def _impact_view(g, symbol: str, direction: str, max_depth: int) -> str:
    """graph.impact's payload rendered under the #125 line law: the
    total is always the full closure size, per-hop rows carry true
    counts with "+N more" past the name cap, and a depth cut announces
    "+N more past the depth cap" instead of stopping silently."""
    res = g.impact(symbol, direction=direction, max_depth=max_depth)
    if res is None:
        return _miss_view(g, symbol)
    what = (
        "callers — what breaks"
        if res["direction"] == "callers"
        else "callees — what it depends on"
    )
    seeds = _capped([_sg_short(k) for k in res["seeds"]], 3)
    out = [
        f"impact of {res['symbol']} ({seeds}): {what}",
        f"total: {res['total']} within {res['max_depth']} hops",
    ]
    for hop, keys in enumerate(res["by_depth"], start=1):
        out.append(_sg_row(f"depth {hop}", keys))
    if res["beyond"]:
        cap = res["max_depth"]
        hint = (
            f"pass max_depth={cap + 1} to expand"
            if cap < graph.IMPACT_MAX_DEPTH
            else f"max_depth is capped at {graph.IMPACT_MAX_DEPTH}"
        )
        out.append(
            f"    … +{res['beyond']} more past the depth cap — {hint}"
        )
    if res["direction"] == "callers" and res["total"]:
        if res["entries"]:
            out.append(_sg_row("entries reached", res["entries"]))
        else:
            out.append(
                "    entries reached: 0 — no known entry (test / "
                "registration / export) anchors these callers; verify "
                "dispatch manually"
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
def impact(symbol: str, direction: str = "callers", max_depth: int = 4,
           dir: str = "") -> str:
    """Transitive blast radius of a function or class — the pre-refactor check.

    Everything that transitively calls it (direction="callers": what
    BREAKS if it changes, moves, or disappears) or everything it
    transitively calls (direction="callees": what it depends on). Run it
    before renaming or removing a hub: the response carries the full
    closure total plus a per-hop histogram (depth 1: N, depth 2: M, ...)
    for the falloff shape, and caller chains anchored at known entries
    (tests, registrations, exports) are listed as "entries reached" —
    a chain that dead-ends reads as such instead. Counts never truncate
    silently (issue #125): "+N more" past 8 names per row, "+N more
    past the depth cap" when the walk stops early. max_depth caps at 8;
    a bad direction answers with guidance, not an error.

    Same resolution as symbol_graph (exact name, then class methods,
    then substring — up to 10 seeds): use symbol_graph for the 1-hop
    detail view, dead_code for the no-caller verdict.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        try:
            return _impact_view(
                graph.get_graph(), symbol, direction, max_depth
            )
        except ValueError as e:
            return str(e)


@mcp.tool(annotations=READONLY)
def dead_code(n: int = 100, dir: str = "") -> str:
    """Functions unreachable from any entry point — deletion candidates.

    Entry points: autoloads, virtuals (_ready/_process/...), signal handlers
    (code + .tscn connections), GUT tests, string-dispatched names. Tiers:
    'likely' (no dynamic dispatch in file — strong candidate) and 'review'
    (file uses call()/Callable()/connect() — verify manually). NEVER delete
    without reading the file and running tests.

    Lists at most n rows (default 100, clamp 1..100); 'likely' rows come
    first, so a cut is never silent — the footer names how many of each
    tier you got (issue #266).

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
        shown = res["candidates"]
        if len(shown) < res["total"]:
            n_lik = sum(1 for d in shown if d["tier"] == "likely")
            n_rev = sum(1 for d in shown if d["tier"] == "review")
            lines.append("")
            lines.append(
                f"… truncated at {len(shown)} rows: showing "
                f"{n_lik} of {res['by_tier'].get('likely', 0)} likely + "
                f"{n_rev} of {res['by_tier'].get('review', 0)} review"
                " — pass n= for the rest"
            )
        return "\n".join(lines)


@mcp.tool(annotations=READONLY)
def duplicates(n: int = 20, dir: str = "") -> str:
    """Duplicated function bodies (exact, whitespace/comment-normalized),
    across ALL indexed languages — the extractor registry is the roster,
    so every language the walk indexes gets scanned (issue #116: the
    scan was never GDScript-only; no repo gets a false clean bill).
    Comment stripping follows each language's own comment syntax where
    the normalizer implements it; elsewhere comments compare as body
    text.

    Simplification targets: same logic living twice. Groups with 3+ members
    first. Cross-file groups are refactoring gold (extract shared helper);
    same-file groups are quick wins.

    Pure-delegate groups (a null-guard + single forwarding call — thin
    wrappers around a shared helper) are skipped, not reported as
    duplication; the footer counts them (issue #268).

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
        rep = g.duplicates_report(limit=n)
        groups = rep["groups"]
        skipped = rep["delegate_skipped"]
        skip_note = (
            f"{skipped} pure-delegate group(s) skipped"
            " — thin delegates, not duplicated logic"
        )
        if not groups:
            scanned = sum(1 for fs in g.files.values() if fs.funcs)
            tail = f"; {skip_note}" if skipped else ""
            return f"no exact duplicates found ({scanned} files with functions scanned{tail})"
        lines = [f"{len(groups)} duplicate group(s):", ""]
        for grp in groups:
            lines.append(f"group {grp['hash']} ({len(grp['members'])} copies):")
            lines.extend(f"  - {m.replace('::', '#')}" for m in grp["members"])
            lines.append("")
        if len(groups) < rep["groups_total"]:
            lines.append(
                f"… truncated at {len(groups)} of {rep['groups_total']}"
                " groups — pass n= for the rest"
            )
        if skipped:
            lines.append(skip_note)
        return "\n".join(lines)


CLUSTER_LIST_CAP = 30  # clusters shown per response; +N more past it


def _cluster_member(path_class: tuple) -> str:
    path, _cls = path_class
    return f"  res://{path}"

@mcp.tool(annotations=READONLY)
async def clusters(
    k: int = 6,
    min_sim: float = 0.6,
    dir: str = "",
    ctx: Context = None,
) -> str:
    """Subsystem clusters discovered from embedding geometry (mutual kNN).

    Shows which files belong to the same feature family — UI, core systems,
    asset handling, networking. Use to survey unfamiliar areas or find
    every file related to a system before refactoring it. Returns cluster
    sizes with member paths + class names.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    def _body() -> str:
        with _route(dir) as prelude:
            if prelude:
                return prelude
            _auto_rescan()
            k_c = max(2, min(k, 12))
            ms = max(0.4, min(min_sim, 0.85))
            cs = nav.clusters(k=k_c, min_sim=ms)
            if not cs:
                return "index empty — call rescan first"
            lines = [f"{len(cs)} cluster(s):", ""]
            for c in cs[:CLUSTER_LIST_CAP]:
                label = c.get("label") or "misc"
                meta = f" [{c.get('method')}, conf {c.get('confidence', 0):.2f}]"
                lines.append(f"c{c['id']} {label} — {c['size']} files{meta}:")
                lines.extend(_capped_row(c["paths"], 12, _cluster_member))
                lines.append("")
            if len(cs) > CLUSTER_LIST_CAP:
                lines.append(
                    f"… +{len(cs) - CLUSTER_LIST_CAP} more cluster(s) — "
                    f"listing capped at {CLUSTER_LIST_CAP}"
                )
            return "\n".join(lines)

    return await _serve(_body, ctx)


@mcp.tool(annotations=READONLY)
def crosstalk(dir: str = "") -> str:
    """Coupling-hotspot report: which subsystem clusters are wired together.

    Counts structural edges (call/signal/var/instance/attach) that CROSS
    cluster boundaries; one edge = one distinct fn pair — call-site and
    call-kind multiplicity collapsed. Scene->scene resource references (pack
    composition) are tallied separately and feed no number; each row
    carries the cluster's derivation method + confidence (#267).
    Use before splitting/merging modules: a cluster with high
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


@mcp.tool(annotations=READONLY)
def arch_check(dir: str = "") -> str:
    """Architecture-contract check: project rules over cluster crosstalk.

    Reads <state_dir>/arch-rules.json and evaluates each rule against
    the same partition + wiring the crosstalk tool reports — `forbid`
    (cluster A must send zero wires to cluster B) and `budget` (at most
    `max` wires), optionally narrowed to edge types call/var/signal/
    inst/attach/alias. Clusters are named by label or the cN id the
    clusters tool prints. Use before splitting/merging modules to prove
    a boundary still holds, or in review to catch new forbidden
    coupling with the offending file pairs. No rules file configured
    answers with how to write one; a typo'd rule file is reported
    loudly (unknown kind/cluster/type), never silently skipped.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (issue #131 — a fresh dir onboards on first
    contact).
    """
    with _route(dir) as prelude:
        if prelude:
            return prelude
        _auto_rescan()
        import archrules as _arch

        return _arch.run(nav.clusters(), graph.get_graph())


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


def _capped_row(items: list, cap: int, render, indent: str = "  ") -> list[str]:
    """Row-wise sibling of _capped (issue #125): first `cap` items as
    rendered rows, then one explicit +N more line — the shared leaf for
    every hand-rolled row slice."""
    rows = [render(it) for it in items[:cap]]
    if len(items) > cap:
        rows.append(f"{indent}… +{len(items) - cap} more")
    return rows


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
        def _row(m: tuple) -> str:
            v, pp, cc = m
            return f"    {v:>3}  res://{pp}" + (f" ({cc})" if cc else "")

        lines.append(f"  members (top {min(12, len(members))} by in-degree):")
        lines.extend(_capped_row(members, 12, _row, indent="    "))
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
        def _nb_row(e: tuple) -> str:
            _w, nb, out_t, in_t = e
            parts = []
            if out_t:
                parts.append("-> " + _ctx_types(out_t))
            if in_t:
                parts.append("<- " + _ctx_types(in_t))
            return f"  res://{nb}  {'  '.join(parts)}"

        lines.extend(_capped_row(entries, 15, _nb_row))
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


# ---- background bake (issue #315) ------------------------------------------
# viz.ensure_bake() on a large store outlives any client timeout; run it on
# a dedicated baker thread fed by a FIFO of store configs (None = the boot
# store). The boot-store bake waits for _BOOT_READY first (baking a
# half-built index wastes minutes); a foreign dir bakes under the scope
# lock + config_scope because nav's globals are shared routing state.
_BAKE_LOCK = threading.Lock()
_BAKE_STATE: dict = {"running": False, "started": 0.0, "error": "", "done_at": 0.0, "out": ""}
_BAKE_QUEUE: list[Path | None] = []
_BAKE_WAKE = threading.Event()
_BAKE_START_LOCK = threading.Lock()
_BAKE_THREAD: threading.Thread | None = None


def _bake_loop() -> None:
    while True:
        _BAKE_WAKE.wait()
        _BAKE_WAKE.clear()
        while _BAKE_QUEUE:
            with _BAKE_LOCK:
                target = _BAKE_QUEUE.pop(0)
                _BAKE_STATE.update(running=True, started=time.monotonic(), error="")
            t0 = time.monotonic()
            note = target.as_posix() if target else "the boot store"
            _progress_set("bake", note=f"graph.html bake — {note}")
            try:
                import viz

                # Both arms hold _SCOPE_LOCK (PR #318 gate, GK P1 race): a
                # bake reads nav globals (STATE_DIR, its one chroma fetch)
                # minutes deep in _build_data — a concurrent dir-routed call
                # would swap them mid-bake and mix stores. RLock, no
                # deadlock: the boot gate opened before the lock was
                # released (READY is set after the boot sequence drops it)
                # and routed bodies release on exit — the bake just
                # serializes like any other scoped op.
                if _BOOT_THREAD is not None:
                    # in-process imports have no boot thread (pre-#273
                    # semantics) — the gate is already open for them
                    _BOOT_READY.wait(_BOOT_WAIT_S + LOCK_WAIT_S + 60.0)
                    with _SCOPE_LOCK:
                        out = viz.ensure_bake()
                else:
                    # foreign store: swap nav's globals for the bake only
                    with _SCOPE_LOCK:
                        with nav.config_scope(target):
                            out = viz.ensure_bake()
                with _BAKE_LOCK:
                    _BAKE_STATE.update(
                        running=False, done_at=time.monotonic(), out=str(out), error=""
                    )
                print(
                    f"neuronav: bake complete: {out} ({time.monotonic() - t0:.1f}s)",
                    file=sys.stderr,
                )
            except Exception as e:  # loud-failures law: a dead bake says so
                with _BAKE_LOCK:
                    _BAKE_STATE.update(
                        running=False, done_at=time.monotonic(),
                        error=f"{type(e).__name__}: {e}",
                    )
                print(f"neuronav: bake FAILED: {e!r}", file=sys.stderr)
            finally:
                _progress_set("done", note=f"bake finished — {note}")


def _start_baker() -> None:
    global _BAKE_THREAD
    with _BAKE_START_LOCK:
        if _BAKE_THREAD is None or not _BAKE_THREAD.is_alive():
            _BAKE_THREAD = threading.Thread(
                target=_bake_loop, name="neuronav-bake", daemon=True
            )
            _BAKE_THREAD.start()


@mcp.tool(annotations=READONLY)
async def visualize(dir: str = "", ctx: Context = None) -> str:
    """Generate the interactive 3D code-graph (rotatable neuron map).

    Nodes = files (colored by subsystem cluster, red-tinted when they contain
    dead-code candidates), edges = calls/instancing/signals. Search box,
    cluster filter chips, dead-code toggle, click for connections.
    Returns the bake path + its openable file:// URI — the file is fully
    self-contained and boots directly in a browser (issue #133). Regenerate
    after rescan if the graph changed materially.

    Issue #315: a bake on a large store outlives the client's timeout, so
    the tool no longer blocks on it. It validates, queues the bake on the
    background baker, and answers immediately with current build/bake
    progress; the bake result (path) and any failure surface on stderr and
    the neuronav://onboarding/status resource.

    dir="" serves the boot config's repo; any other path routes this bake
    to that checkout (issue #131 — a fresh dir indexes on first bake).
    """
    def _body() -> str:
        try:
            import viz  # noqa: F401 — delete-able-surface guard (unchanged)
        except ImportError:
            return ("viz add-on not installed — delete-able surface is viz.py + vendor/ + "
                    "tools/serve.py; core tools (search/repo_map/context/...) work without it. "
                    "Restore viz.py to re-enable the bake.")
        if dir:
            resolved = Path(dir).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError(f"not a directory: {dir}")
            cfg_path = resolved / ".neuronav" / "config.json"
            if not cfg_path.is_file():
                onboard.scaffold(resolved)
            else:
                _validate_foreign_config(cfg_path, resolved)
            target: Path | None = cfg_path
        else:
            target = None  # the boot config's store
        with _BAKE_LOCK:
            _BAKE_QUEUE.append(target)
            in_flight = _BAKE_STATE["running"]
            ahead = len(_BAKE_QUEUE) - 1
        _BAKE_WAKE.set()
        _start_baker()
        line = _progress_line() or "no build in flight"
        where = "bake running" if in_flight and ahead == 0 else (
            f"queued behind {ahead} bake(s)" if ahead or in_flight else "starting now"
        )
        return (
            f"bake accepted — {where}; build state: {line}. The bake runs in "
            "the background (minutes on a large store): watch stderr or poll "
            "the neuronav://onboarding/status resource — graph.html lands at "
            "the store's .neuronav when done, and a failure there is loud."
        )

    return await _serve(_body, ctx)


@mcp.resource("neuronav://onboarding/status")
def _onboarding_status() -> str:
    """Pollable build/bake state (issue #315) — the liveness channel that
    answers while tools are gated on a first index build or a bake. Reads
    two dicts under their locks; never touches nav, so it cannot block on
    the store or the scope lock."""
    lines = []
    if _BOOT_FATAL is not None:
        lines.append(f"boot: FATAL — {_BOOT_FATAL}")
    elif _BOOT_THREAD is not None and not _BOOT_READY.is_set():
        lines.append(f"boot: building — {_progress_line() or 'starting'}")
    else:
        lines.append("boot: ready")
    line = _progress_line()
    if line:
        lines.append(f"build: {line}")
    with _BAKE_LOCK:
        running = _BAKE_STATE["running"]
        started = _BAKE_STATE["started"]
        error = _BAKE_STATE["error"]
        done_at = _BAKE_STATE["done_at"]
        out = _BAKE_STATE["out"]
        queued = list(_BAKE_QUEUE)
    if running:
        lines.append(f"bake: running ({time.monotonic() - started:.0f}s in)")
    elif error:
        lines.append(f"bake: FAILED — {error}")
    elif out:
        lines.append(f"bake: done {time.monotonic() - done_at:.0f}s ago — {out}")
    else:
        lines.append("bake: not run this session")
    if queued:
        lines.append(
            "bake queue: " + ", ".join(q.as_posix() if q else "boot store" for q in queued)
        )
    return "\n".join(lines)


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


@mcp.tool(annotations=MUTATING_MEMORY)
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
    are refused). This tool mutates durable state (set overwrites,
    delete removes) — annotated destructiveHint, never read-only.
    """
    with _route(dir) as prelude:
        out = memories.run(verb, name, body)
        # a routed fresh dir onboards mid-call; unlike read tools we do
        # NOT return the prelude alone — dropping a mutating op to
        # report the build would silently lose the write
        return f"{prelude}\n{out}" if prelude else out

@mcp.tool(annotations=MUTATING_RESCAN)
async def rescan(dir: str = "", ctx: Context = None) -> str:
    """Re-index changed/new/deleted files: vectors, function index, graph.

    Fast when nothing changed. Run after pulling, branching, or mass
    edits. Read tools also auto-rescan on worktree drift (stat-gated,
    mtime/size fingerprint) — the boot project only; this explicit
    variant is also the freshness path for a non-boot dir (issue #131).

    When files changed, a capped changed/deleted path list (10 shown,
    "+N more" past it) follows the summary line. On a large first pass
    the call reports progress (issue #315): notifications/progress to
    tokened clients, and the same line on stderr.

    dir="" serves the boot config's repo; any other path routes this one
    call to that checkout (a fresh dir onboards on first contact).
    """
    def _body() -> str:
        with _route(dir) as prelude:
            # issue #41 law: the explicit rescan TOOL stays loud on a 0-file
            # walk — so the degraded-boot guidance (yielded by identity) is
            # NOT returned; we fall through to the bounded rescan, whose
            # RuntimeError names root/extensions. A mid-session recovery
            # prelude (a different string) still returns.
            if prelude and prelude is not _BOOT_DEGRADED:
                return prelude
            t0 = time.perf_counter()
            stats = _bounded_rescan()
            if stats["added"] or stats["updated"] or stats["deleted"]:
                _progress_set(
                    "graph",
                    note=f"rebuilding graph — {len(stats.get('changed', []))} changed",
                )
            g, fns, note = _sync_chain(stats)
            _progress_set("done", note="rescan complete")
            nav.stat_mark_synced()
            dt = time.perf_counter() - t0
            return (
                f"rescan: files {stats['added']}/{stats['updated']}/"
                f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d), "
                f"fns {fns['fns_upserted']} upserted, graph {len(g.files)} files, "
                f"in {dt:.1f}s{note}"
                + _rescan_paths(stats)
            )

    return await _serve(_body, ctx)


def _boot_sequence(t0: float) -> None:
    """The boot work main() used to run before the handshake (#273):
    census, embed probe, boot rescan, graph rebuild, raw-text banner.
    Serialized on _SCOPE_LOCK so routed dir= calls (which swap nav's
    process-wide config scope) never interleave with a boot-config
    rescan. The empty-store + dead-embedder abort raises SystemExit —
    the _boot_thread wrapper decides per context: os._exit(1) when the
    stdio session owns the process (#240's fix-in-the-message exit),
    _BOOT_FATAL when server was imported in-process (#273 lazy boot)."""
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    fns_up = 0
    watch_note = ""
    want_watch = False
    with _SCOPE_LOCK:
        # issue #240: what the root actually holds, extension filter
        # off — the 0-file verdict, the degraded-boot guidance and
        # the raw-text banner all read this one census
        census = nav.suffix_census()
        probe_fail = _probe_embedder()
        if not any(s in nav.EXTS for s in census):
            _enter_degraded(census, probe_fail, "boot walk matched 0 files")
        elif probe_fail is not None:
            if nav.count() == 0:
                # evidence-based abort (issue #240): an empty store
                # needs embeds to build — every path from here fails
                # mid-rescan. Exit with the fix in the message; the
                # handshake is already up, so the harness sees the
                # drop and stderr carries the fix.
                raise SystemExit(
                    f"neuronav: {probe_fail} The store is empty and every "
                    "index build embeds — aborting the session so the "
                    "failure carries the fix. Pull the model / start "
                    "the backend, then restart the session."
                )
            # warm store: serve it degraded (the #19 law already
            # covers embed failures mid-serve); skip the boot rescan
            # — it would die on the first new embed
            print(
                f"neuronav: {probe_fail} Serving the warm index degraded; "
                "rescans that need new embeddings retry with the "
                "tool-call cooldown until the backend is back.",
                file=sys.stderr,
            )
        else:
            try:
                stats = _bounded_rescan()
            except RuntimeError:
                # walk emptied between census and rescan — same
                # degraded path
                _enter_degraded(
                    nav.suffix_census(), None,
                    "boot rescan found the walk empty",
                )
            else:
                if stats["added"] or stats["updated"] or stats["deleted"]:
                    _progress_set(
                        "graph",
                        note=f"rebuilding graph — {len(stats.get('changed', []))} changed",
                    )
                g, fns, _ = _sync_chain(stats)
                _progress_set("done", note="boot build complete")
                nav.stat_mark_synced()
                fns_up = fns["fns_upserted"]
                _raw_text_banner(census)
                # boot config only by design (issue #131): the
                # watcher drives _auto_rescan, which is boot-gated —
                # routed dirs refresh explicitly
                want_watch = nav.WATCH_INTERVAL_S > 0
    # the boot gate opens with the store work done: imports are NOT
    # warmed here — scipy/sklearn on a side thread while the anyio
    # stdio loop runs deadlocks on Windows (the documented law),
    # so main() warms them on the main thread before the loop
    _BOOT_READY.set()
    # started strictly after _BOOT_READY so a fast first tick can
    # never race the boot rescan it would duplicate
    if want_watch:
        _start_watcher(nav.WATCH_INTERVAL_S)
        watch_note = f", watcher {nav.WATCH_INTERVAL_S:g}s"
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


def _boot_thread(t0: float) -> None:
    """_boot_sequence wrapper: owns the loud failure contract so the
    sequence body stays linear. SystemExit (the #240 empty-store abort)
    ends the process — the stdio session owns it; any other exception
    converts to _BOOT_FATAL so gated tool calls fail loudly, never a
    silent thread death."""
    global _BOOT_FATAL
    try:
        _boot_sequence(t0)
    except SystemExit as e:
        print(str(e), file=sys.stderr, flush=True)
        os._exit(1)
    except Exception as e:  # noqa: BLE001 — loud, never a silent thread death
        _BOOT_FATAL = (
            f"{type(e).__name__}: {e} — see the neuronav: stderr lines "
            "above; fix and restart the session."
        )
        print(f"neuronav: BOOT FAILED — {_BOOT_FATAL}", file=sys.stderr, flush=True)
        _BOOT_READY.set()


def _start_boot(t0: float) -> threading.Thread:
    """Exactly-once boot thread start (idempotent under the start lock);
    only main() starts it — in-process imports close the gate without
    boot work instead (_await_boot)."""
    global _BOOT_THREAD, _BOOT_T0
    with _BOOT_START_LOCK:
        if _BOOT_THREAD is None:
            _BOOT_T0 = time.monotonic()
            _BOOT_THREAD = threading.Thread(
                target=_boot_thread, args=(t0,),
                name="neuronav-boot", daemon=True,
            )
            _BOOT_THREAD.start()
        return _BOOT_THREAD


def main() -> None:
    """Console-script boot (issue #204) — the historic ``__main__`` body
    behind the ``neuronav-mcp`` entry point. #273 handshake-first: the
    config banner lands on stderr (#203 law — before any rescan work),
    then the stdio loop serves; census/probe/rescan/graph work runs on
    the daemon boot thread (_boot_sequence) and every tool call gates on
    the boot-ready event (bounded, loud on fatality or timeout) —
    connection time never includes opening or building the chroma store.
    The C-stack warm imports (networkx/numpy/scipy/sklearn, ~1.3s) stay
    on THIS thread before the loop: importing them on any side thread
    while the anyio loop serves deadlocks stdio on Windows, and they are
    cheap next to the store build the handshake no longer waits for.
    #240: a 0-file walk degrades to first-call guidance, and the
    empty-store + dead-embedder abort exits the session after the
    handshake with the fix on stderr."""
    t0 = time.perf_counter()
    if nav.CONFIG_PATH is not None:
        print(f"neuronav: config {nav.CONFIG_PATH}", file=sys.stderr)
    else:
        print(f"neuronav: pure defaults, root={nav.ROOT}", file=sys.stderr)
    # warm the clusters stack on the main thread BEFORE the event loop:
    # clusters/context/crosstalk all ride these imports, and importing
    # them inside a fastmcp tool call (anyio loop thread) — or on any
    # side thread while the loop serves — blocks the stdio server
    # indefinitely on Windows
    import networkx  # noqa: F401
    import numpy  # noqa: F401
    import scipy.cluster.hierarchy  # noqa: F401
    import sklearn.cluster  # noqa: F401
    _start_boot(t0)
    mcp.run(transport="stdio")
    # the client closed stdin: a pending boot abort must still land (the
    # daemon thread would otherwise die with the interpreter) — boot is
    # bounded by LOCK_WAIT_S + embed timeouts, so this join cannot wedge
    _BOOT_THREAD.join()
    if _BOOT_FATAL is not None:
        sys.exit(1)


if __name__ == "__main__":
    main()
