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

import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import anyio  # stdio loop primitives: async tool shells run bodies off-loop (#315)
from mcp.server.fastmcp import Context

import graph
from extractors import PRESETS, registry_for, res_to_rel  # noqa: E402
import memories
import navconfig, navindex, navstore
import recall

# the FastMCP instance, the #207 version pin, the #237 instructions and
# the tool annotations live in servercore (issue #345): every family
# module decorates against the same mcp object from there.
from servercore import MUTATING_BAKE, MUTATING_MEMORY, MUTATING_RESCAN, READONLY, mcp, _capped

# query families register their handlers at import (literal @mcp.tool
# decorators, issue #345): import order == the historical def order, so
# tools/list is byte-identical; their gate rails bind later in the
# composition below (after _auto_rescan is defined).
import server_search as _srv_search
import server_structure as _srv_structure
import server_clusters as _srv_clusters

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

_BOOT_STORE = (str(navconfig.STATE_DIR), navconfig.COLLECTION)
_SCOPE_LOCK = threading.RLock()  # nav globals are process-wide: one routed call at a time


def _at_boot() -> bool:
    return (str(navconfig.STATE_DIR), navconfig.COLLECTION) == _BOOT_STORE


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
        f"  root: {navconfig.ROOT.as_posix()}",
        f"  include_dirs: {list(navconfig.INCLUDE_DIRS)}",
        f"  extensions scanned: [{', '.join(sorted(navconfig.EXTS))}]",
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
                   if s not in navconfig.EXTS]
    if suggestions:
        block = {
            # "root": ".." — a project-local config resolves root against
            # .neuronav itself (the #240 trap); its parent IS the project
            "root": "..",
            "collection": "main",
            "state_dir": "default",
            "include_dirs": list(navconfig.WALK_DEFAULTS["include_dirs"]),
            "extensions": suggestions,
            "exclude_dirs": list(navconfig.WALK_DEFAULTS["exclude_dirs"]),
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
        navstore.embed(["."])
        return None
    except Exception as e:
        reason = navstore.embed_failure_reason(e)
        if navconfig.EMBED_PROVIDER == "ollama":
            fix = f"ollama pull {navconfig.EMBED_MODEL}"
        else:
            fix = (
                f"check embed_model '{navconfig.EMBED_MODEL}' at embed_url "
                f"'{navconfig.EMBED_URL}' (auth via NEURONAV_EMBED_KEY)"
            )
        return (
            f"embed probe FAILED — model '{navconfig.EMBED_MODEL}' via "
            f"{navconfig.EMBED_PROVIDER} at {navconfig.EMBED_URL}: {reason}. Fix: {fix}."
        )


def _raw_text_banner(census: dict[str, int]) -> None:
    """Loud unsupported-language announcement (issue #240): files that
    MATCH the configured extensions but have no extractor index as raw
    text — fns 0 is otherwise indistinguishable from an empty repo.
    stderr at boot and after in-session recovery; the degraded guidance
    names it too."""
    hits = sorted(
        (s, c) for s, c in census.items()
        if s in navconfig.EXTS and registry_for(s) is None
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
    if navconfig.CONFIG_PATH is not None:
        return None
    cfg_path = Path.cwd() / ".neuronav" / "config.json"
    if not cfg_path.is_file():
        return None
    _validate_foreign_config(cfg_path, Path.cwd())
    # the pre-recovery state, restored verbatim on failure: nav's
    # pure-defaults globals, the graph singleton (a mid-sync failure
    # must not leave it serving the candidate store), and the env
    # use_config exports for subprocesses
    saved_cfg = {f: getattr(navconfig, f) for f in navconfig._CONFIG_FIELDS}
    saved_graph = graph._graph
    try:
        navconfig.use_config(cfg_path)
        stats = _bounded_rescan()
        if stats["added"] or stats["updated"] or stats["deleted"]:
            _progress_set(
                "graph",
                note=f"rebuilding graph — {len(stats.get('changed', []))} changed",
            )
        g, fns, note = _sync_chain(stats)
        _progress_set("done", note="recovery complete")
        navindex.stat_mark_synced()
        _raw_text_banner(navindex.suffix_census())
        # only success re-points the boot identity: a failure past
        # use_config must leave _at_boot() describing the store the
        # session actually serves
        _BOOT_STORE = (str(navconfig.STATE_DIR), navconfig.COLLECTION)
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
            setattr(navconfig, f, v)
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
        if not navindex.stat_scan():
            return
        stats = _bounded_rescan()
        if stats["added"] or stats["updated"] or stats["deleted"]:
            _sync_chain(stats)
            print(
                f"neuronav: routed drift healed: files {stats['added']}/{stats['updated']}/"
                f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d) in "
                f"{navconfig.ROOT.as_posix()}",
                file=sys.stderr,
            )
        navindex.stat_mark_synced()
    except Exception as e:
        print(
            f"neuronav: routed drift heal FAILED ({e}); answering from the "
            f"current index — call rescan(dir=\"{navconfig.ROOT.as_posix()}\") once "
            "the embedding backend is back",
            file=sys.stderr,
        )


def _first_contact() -> str | None:
    """Build the active scope's fresh store: tracked base shards first
    (import_base skips cleanly when absent), then the incremental rescan
    heals to the worktree. Returns None when the store already serves;
    else a rescan()-format summary so a long build reports progress the
    same way an explicit rescan does."""
    if navstore._collection().count():
        _heal_routed_drift()
        return None
    navindex.import_base()
    t0 = time.perf_counter()
    stats = _bounded_rescan()
    if stats["added"] or stats["updated"] or stats["deleted"]:
        _progress_set("graph", note=f"rebuilding graph — {len(stats.get('changed', []))} changed")
    g, fns, note = _sync_chain(stats)
    _progress_set("done", note="first contact complete")
    navindex.stat_mark_synced()
    return (
        f"onboarded {navconfig.ROOT.as_posix()} — index built: files "
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


navindex.PROGRESS_HOOKS.append(_nav_progress_hook)


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
        have = navstore._collection().count() > 0
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
        with navconfig.config_scope(cfg_path):
            yield _first_contact()


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
                if target is None:
                    # boot store — issue #354: the arm is picked by TARGET,
                    # never by _BOOT_THREAD (that handle is never cleared
                    # after boot, so the old gate routed every live-server
                    # foreign bake here, baking the BOOT store while the ack
                    # named the foreign dir). Bake only once the boot gate
                    # opened (a half-built index wastes minutes);
                    # in-process imports have no boot thread (pre-#273
                    # semantics) — the gate is already open for them, and
                    # the bounded wait fails loud instead of stalling the
                    # queue behind a wedged boot.
                    if _BOOT_THREAD is not None:
                        wait_s = _BOOT_WAIT_S + LOCK_WAIT_S + 60.0
                        if not _BOOT_READY.wait(wait_s):
                            raise TimeoutError(
                                f"neuronav: boot still incomplete after "
                                f"{wait_s:g}s — bake aborted; see the "
                                "neuronav: stderr lines"
                            )
                    with _SCOPE_LOCK:
                        out = viz.ensure_bake()
                else:
                    # foreign store: swap nav's globals for the bake only
                    with _SCOPE_LOCK:
                        with navconfig.config_scope(target):
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

    dir="" serves the boot config's repo; any other path routes the call
    to that checkout (issue #131) through the same gate as the read tools:
    a fresh dir onboards in-call (scaffold + first-contact index build,
    progress on the usual channels) and the build summary rides above the
    ack; a warm dir heals drift and acks immediately. The queued bake then
    lands in THAT checkout's store, never the boot one (issue #354).
    """
    def _body() -> str:
        try:
            import viz  # noqa: F401 — delete-able-surface guard (unchanged)
        except ImportError:
            return ("viz add-on not installed — delete-able surface is viz.py + vendor/ + "
                    "tools/serve.py; core tools (search/repo_map/context/...) work without it. "
                    "Restore viz.py to re-enable the bake.")
        if dir:
            # issue #354: the routed arm rides the SAME _route gate as the
            # read tools (boot gate, scaffold/validate, config_scope, first
            # contact) — a fresh dir builds its index inside this call, so
            # the queued bake finds a served store instead of the #64
            # guard's empty-store refusal; the build summary rides above
            # the ack like memory's does.
            with _route(dir) as prelude:
                resolved = Path(dir).expanduser().resolve()
                target = resolved / ".neuronav" / "config.json"
        else:
            prelude = None
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
        ack = (
            f"bake accepted — {where}; build state: {line}. The bake runs in "
            "the background (minutes on a large store): watch stderr or poll "
            "the neuronav://onboarding/status resource — graph.html lands at "
            "the store's .neuronav when done, and a failure there is loud."
        )
        return f"{prelude}\n{ack}" if prelude else ack

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
# baseline, and a drifted worktree triggers the sha-gated navindex.rescan() +
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
    return navindex.rescan(timeout=LOCK_WAIT_S)


_rescan_busy = threading.Lock()  # in-flight trigger (cross-process is navstore._db_lock's job)
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
                if not navindex.stat_scan():
                    return
                stats = _bounded_rescan()
                if stats["added"] or stats["updated"] or stats["deleted"]:
                    _sync_chain(stats)
                    print(
                        f"neuronav: auto-rescan: files {stats['added']}/{stats['updated']}/"
                        f"{stats['unchanged']}/{stats['deleted']} (a/u/u/d)",
                        file=sys.stderr,
                    )
                navindex.stat_mark_synced()
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
    last = navindex.stat_fingerprint()
    deadline = time.monotonic() + WATCH_DEBOUNCE_MAX_S
    while time.monotonic() < deadline:
        time.sleep(WATCH_DEBOUNCE_S)
        cur = navindex.stat_fingerprint()
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
            dirty = navindex.stat_scan(force=True)
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


# ---- tool-handler families (issue #345) -----------------------------------
#
# The three read-only query families live in their own modules (verbatim
# moves: server_search / server_structure / server_clusters). They cannot
# import the gate rails from here (this module imports THEM — a cycle),
# so register() receives THIS module and binds each rail as a
# late-binding _Rail handle (issue #359): the family handlers resolve
# _route/_serve/_stale_prelude/_auto_rescan through this namespace at
# call time — the pinning suites (test_server_stdio,
# test_onboardprogress, test_autorescan) reach those names through
# server's own namespace, so the definitions cannot leave this module
# AND a post-registration rebind here (the test seam) reaches the
# family tools. Registration order preserves the historical def order,
# keeping tools/list byte-identical; register() returns the handlers so
# this module keeps binding them at module scope — the suites also call
# server.repo_map & co directly, and the wire names must stay reachable
# where they were.
import server_clusters as _srv_clusters
import server_search as _srv_search
import server_structure as _srv_structure

# sys.modules[__name__] names the true rail owner in both launch shapes
# (python server.py runs it as __main__, the neuronav-mcp console
# script imports it as server); register() runs mid-import, but the
# handles resolve at call time, long after this module is complete.
_RAILS_MOD = sys.modules[__name__]

explore, repo_map, semantic_search, find_functions, search_text = (
    _srv_search.register(_RAILS_MOD))
symbol_graph, impact, dead_code, duplicates = (
    _srv_structure.register(_RAILS_MOD))
clusters, crosstalk, arch_check, context = (
    _srv_clusters.register(_RAILS_MOD))


def _sync_chain(stats: dict) -> tuple[object, object, str]:
    """navindex.rescan -> graph rebuild -> fns sync. fns failures degrade
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
            navindex.stat_mark_synced()
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
        census = navindex.suffix_census()
        probe_fail = _probe_embedder()
        if not any(s in navconfig.EXTS for s in census):
            _enter_degraded(census, probe_fail, "boot walk matched 0 files")
        elif probe_fail is not None:
            if navstore.count() == 0:
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
                    navindex.suffix_census(), None,
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
                navindex.stat_mark_synced()
                fns_up = fns["fns_upserted"]
                _raw_text_banner(census)
                # boot config only by design (issue #131): the
                # watcher drives _auto_rescan, which is boot-gated —
                # routed dirs refresh explicitly
                want_watch = navconfig.WATCH_INTERVAL_S > 0
    # the boot gate opens with the store work done: imports are NOT
    # warmed here — scipy/sklearn on a side thread while the anyio
    # stdio loop runs deadlocks on Windows (the documented law),
    # so main() warms them on the main thread before the loop
    _BOOT_READY.set()
    # started strictly after _BOOT_READY so a fast first tick can
    # never race the boot rescan it would duplicate
    if want_watch:
        _start_watcher(navconfig.WATCH_INTERVAL_S)
        watch_note = f", watcher {navconfig.WATCH_INTERVAL_S:g}s"
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


# registration completes here in one ordered sequence — the historical
# def order — so tools/list is byte-identical to the pre-split single
# module (visualize/memory/rescan register after the query families;
# their defs sit above but registration is composition, issue #345).
mcp.tool(annotations=MUTATING_BAKE)(visualize)
mcp.tool(annotations=MUTATING_MEMORY)(memory)
mcp.tool(annotations=MUTATING_RESCAN)(rescan)


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
    if navconfig.CONFIG_PATH is not None:
        print(f"neuronav: config {navconfig.CONFIG_PATH}", file=sys.stderr)
    else:
        print(f"neuronav: pure defaults, root={navconfig.ROOT}", file=sys.stderr)
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
