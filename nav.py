"""nav core: whole-file semantic index of a checkout (GDScript/scenes,
Python/C++ — whatever the config's "extensions" list enables).

Config resolution (issue #27 — the install is read-only at onboarding
time; config travels with the project):
  1. $NEURONAV_CONFIG env var (explicit, always wins),
  2. ``<cwd>/.neuronav/config.json`` (project-local; ``onboard.py init``
     writes it, ``onboard.py wire`` scaffolds + wires MCP),
  3. ``config.json`` next to this file, but ONLY when cwd IS the checkout
     (legacy machine-local default for the install's own target),
  4. no config: pure defaults — root = cwd, include ``.``, extensions =
     every registered extractor suffix, state = ``<root>/.neuronav``.
Per-project state (issue #15): everything a config generates lives under
``state_dir`` - chroma store at ``chroma/``, base shards at ``base/``,
viz bake at ``graph.html``. No auto-migration. A config file MUST carry
the key (issue #91): the silent ``<root>/.neuronav`` default reads and
writes a store inside the scanned root, so a state_dir-less config aborts
at load — ``"default"`` is the explicit opt-in (onboard.py init writes
it); only the no-config pure-defaults leg keeps the implicit default.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import chromadb
import chromadb.errors  # drop CLI: NotFoundError is the only swallowable failure (#298)
from filelock import FileLock, Timeout
import httpx

# hybrid recall (BM25F + reciprocal-rank fusion + 1-hop context). Module-
# level so the python extractor's import liveness keeps recall.py's funcs
# alive in the self-index; recall.py binds nav/graph lazily inside its
# functions, so `nav.py --config <profile> ...` still switches profiles.
import recall
from extractors import WALK_EXTS, registry_for  # noqa: E402

# ---- build observability (issue #315) --------------------------------------
# Rescan/first-contact embed loops report (phase, count, total) through
# registered observers. The CLI registers none (zero behavior change);
# the MCP server registers one that feeds its progress snapshot.
# Observers must never break the build they observe: a raising hook is
# reported loudly on stderr and skipped.
PROGRESS_HOOKS: list = []


def _report_progress(phase: str, count: int, total: int) -> None:
    for hook in list(PROGRESS_HOOKS):
        try:
            hook(phase, count, total)
        except Exception as e:  # noqa: BLE001 — loud, never fatal
            print(
                f"neuronav: progress hook {getattr(hook, '__name__', hook)!r} "
                f"raised: {e!r} — ignored, the index build continues",
                file=sys.stderr,
            )

TOOL_DIR = Path(__file__).resolve().parent


def _discover_config() -> Path | None:
    """Issue #27 discovery: env beats project-local beats checkout-local.
    Returns ``None`` when nothing applies -> caller uses pure defaults."""
    env = os.environ.get("NEURONAV_CONFIG")
    if env is not None:
        if not env.strip():
            # #298: present-but-empty is an explicit config contract (#41
            # class) — falling through to defaults silently switches the
            # walk identity the same way a wrong path would
            raise SystemExit(
                "NEURONAV_CONFIG is set but empty — unset it or point it at "
                "a real config json (onboard.py init writes one)"
            )
        return Path(env)
    local = Path.cwd() / ".neuronav" / "config.json"
    if local.is_file():
        return local
    checkout = TOOL_DIR / "config.json"
    if checkout.is_file() and Path.cwd() == TOOL_DIR:
        return checkout
    return None


def _apply_config(path: Path | None) -> None:
    """(Re)bind the config-derived module globals. Called once at import
    and again by ``nav.py --config <path>`` (which also sets NEURONAV_CONFIG
    so subprocesses and sibling modules like graph.py agree). ``path=None``
    means no config anywhere: pure cwd defaults (issue #27)."""
    global CONFIG_PATH, COLLECTION, ROOT, INCLUDE_DIRS, EXTS, EXCLUDE_DIRS, \
        EMBED_URL, EMBED_MODEL, EMBED_DIM, EMBED_DOC_PREFIX, EMBED_PROVIDER, \
        EMBED_API_KEY, WATCH_INTERVAL_S, RECALL_TWO_PASS, CHUNK_CAST, \
        FILE_DOC_CAST, STATE_DIR, DB_DIR, BASE_DIR, GITIGNORE_PRUNE_DIRS
    if path is not None and not path.is_file():
        # issue #41: an explicit config path is a contract, not a hint —
        # silently degrading to walk-all defaults flips the walk identity
        # and the next rescan purges the previous profile's entries
        raise SystemExit(
            f"NEURONAV_CONFIG points at '{path}', which does not exist — "
            "unset the variable or point it at a real config json "
            "(onboard.py init writes one)"
        )
    # utf-8-sig: Windows tooling (PowerShell 5 Set-Content -Encoding utf8)
    # writes a BOM that plain utf-8 reads keep — json.loads then dies on
    # \ufeff with a cryptic JSONDecodeError (issue #119)
    CONFIG_PATH = path
    cfg: dict = json.loads(path.read_text(encoding="utf-8-sig")) if path is not None else {}
    # lazy import: extractors pulls graph-ish deps only for the suffix list
    from extractors import EXTENSIONS as _REGISTERED
    # issue #240 (the "." trap): a project-local config's config-dir-
    # relative "." IS .neuronav/ itself — state-only and on the default
    # exclude list, so a walk rooted there can match nothing. The
    # project is the PARENT dir; a missing root and "." both mean it.
    # Any other explicit root wins verbatim, config-dir-relative as
    # before (documented beside the config schema: config/AGENTS.md).
    _project_local = path is not None and path.parent.name == ".neuronav"
    _root_raw = cfg.get("root")
    if _project_local and (not _root_raw or Path(_root_raw) == Path(".")):
        ROOT = path.parent.parent.resolve()
    else:
        ROOT = Path(_root_raw) if _root_raw else Path.cwd()
        if not ROOT.is_absolute():
            # relative roots resolve against the config file's own directory,
            # so shipped profiles (config/neuronav.json) stay machine-portable
            ROOT = (path.parent / ROOT).resolve()
    COLLECTION = str(cfg.get("collection", "main"))
    # walk scope (issues #27 / #296): project-local and no-config legs walk
    # everything; a config that omits include_dirs also walks everything —
    # include_dirs is strictly opt-in (the old fallback was one target
    # repo's layout and walked 0 files everywhere else). Only the legacy
    # extensions/exclude fallbacks remain config-mode-scoped.
    _walk_all = path is None or _project_local
    INCLUDE_DIRS = tuple(cfg.get("include_dirs", WALK_DEFAULTS["include_dirs"]))
    EXTS = set(cfg.get("extensions", sorted(_REGISTERED) if _walk_all else WALK_EXTS))
    EXCLUDE_DIRS = frozenset(cfg.get("exclude_dirs", WALK_DEFAULTS["exclude_dirs"] if _walk_all else (".git", "__pycache__")))
    EXCLUDE_DIRS |= _neuroignore(path)
    # issue #296-B: gitignore-aware walking. Root .gitignore dir entries
    # join the exclude set via the same union as .neuroignore — additive
    # only, so precedence stays: explicit exclude_dirs > .neuroignore
    # (both in EXCLUDE_DIRS) > .gitignore dir entries (a separate set so
    # they can be attributed/counted, never re-included). Applied on every
    # leg: walk-all was the landmine surface, but a configured walk on a
    # fresh repo deserves the same honesty.
    GITIGNORE_PRUNE_DIRS = _gitignore_dirs(ROOT) - EXCLUDE_DIRS
    EXCLUDE_DIRS |= GITIGNORE_PRUNE_DIRS
    EMBED_URL = str(cfg.get("embed_url", "http://127.0.0.1:11434/api/embed"))
    EMBED_MODEL = str(cfg.get("embed_model", "qwen3-embedding:0.6b"))
    EMBED_DIM = int(cfg.get("embed_dim", 1024))
    # issue #75: passage-instruction text (JCE card "Candidate code
    # snippet:") prepended to the EMBEDDED document only — stored
    # documents stay raw. Symmetric to recall's query_prefix.
    EMBED_DOC_PREFIX = str(cfg.get("embed_doc_prefix", ""))
    # issue #17: the wire protocol follows the endpoint — Ollama /api/embed
    # or any OpenAI-compatible /embeddings (OpenAI, vLLM, LM Studio,
    # Ollama's own /v1 layer). Explicit "ollama"|"openai" wins; unset
    # auto-detects from the url path. NEURONAV_EMBED_KEY beats the config
    # key so secrets stay out of tracked profiles; the Bearer header only
    # goes out when a key is present, so keyless local servers still work.
    _provider = str(cfg.get("embed_provider", "")).strip().lower()
    if _provider and _provider not in ("ollama", "openai"):
        raise SystemExit(
            f"embed_provider '{_provider}' is not 'ollama' or 'openai' — "
            "fix the config json or unset it to auto-detect from embed_url"
        )
    EMBED_PROVIDER = (
        _provider
        if _provider
        else ("openai" if EMBED_URL.rstrip("/").endswith("/embeddings") else "ollama")
    )
    EMBED_API_KEY = os.environ.get("NEURONAV_EMBED_KEY") or str(cfg.get("embed_api_key", ""))
    # >0: the MCP server polls the stat gate every N seconds and
    # auto-rescans without waiting for a tool call (issue #19)
    WATCH_INTERVAL_S = float(cfg.get("watch_interval_s") or 0.0)
    # issue #74 (RepoCoder): two-pass retrieve — recall.search re-queries
    # with identifiers harvested from the pass-1 lexical top-k (embed
    # budget 2/query, hits marked two_pass). Bench A/B beats single-pass
    # on every metric (hit@1 0.40->0.56, hit@5 0.84->0.88, MRR
    # 0.587->0.706) but doubles query-side embeds on the shared
    # semantic_search path — default OFF, owner's flip after review.
    RECALL_TWO_PASS = bool(cfg.get("recall_two_pass", False))
    # issue #76/#141 (cAST chunking): split monster fn bodies into
    # statement-block chunk docs + fold micro fns into class context,
    # in the fn-level index only. The fresh-store bench A/B (real
    # qwen3 embeds, per-config scratch stores) showed NO lift and NO
    # regression — recall reads the file layer; the fn layer is
    # invisible to it — and the earlier regression table was a
    # stale-store artifact. The feature ships dark until a passing A/B
    # at flip time: 0.0 = OFF — byte-identical pre-#141 fn docs;
    # 1.0 = calibrated thresholds, other positives scale them.
    # Bench-opt-in, owner's flip after a passing A/B (same law as
    # recall_two_pass).
    CHUNK_CAST = float(cfg.get("chunk_cast", 0.0))
    # issue #229: cAST file-doc shaping — the graph fn-doc machinery
    # applied to the FILE docs this nav embeds (micro-fn merge into
    # class context, monster-fn split at block boundaries, signature
    # first, everything under MAX_EMBED_CHARS). 1.0 ships ON (the
    # fresh-store #229 bench A/B is the winning wire); 0.0 = raw file
    # text, the byte-identical pre-#229 surface; other positives scale
    # the fn-layer thresholds.
    FILE_DOC_CAST = float(cfg.get("chunk_file_doc", 1.0))
    # per-project state: chroma store, base shards and the viz bake all
    # derive from one dir — explicit "state_dir" honored; "default" is
    # the opt-in for <root>/.neuronav. Relative values resolve against
    # the config file's own dir (same law as "root"), so shipped
    # profiles stay portable. Issue #91: a config WITHOUT the key used
    # to silently default to <root>/.neuronav — a store inside the
    # scanned root, so any rescan read and wrote it directly (the door
    # that wiped a live store). Such configs abort at load with the
    # exact fix; only the no-config pure-defaults leg keeps the
    # implicit default — there is no config to fix there.
    _sd = cfg.get("state_dir")
    if path is not None and not _sd:
        raise SystemExit(
            f"config '{path}' sets no \"state_dir\" — its default "
            f"{ROOT / '.neuronav'} sits inside the scanned root, so any "
            "rescan would read and write that store directly (issue #91: "
            "this silent default is how a live store got wiped). Fix the "
            "config json: \"state_dir\": \"<scratch path>\" for a store "
            "of your own, or \"state_dir\": \"default\" to opt into "
            "<root>/.neuronav (onboard.py init writes the opt-in)"
        )
    STATE_DIR = ROOT / ".neuronav" if _sd is None or _sd == "default" else Path(_sd)
    if not STATE_DIR.is_absolute() and path is not None:
        STATE_DIR = (path.parent / STATE_DIR).resolve()
    else:
        STATE_DIR = STATE_DIR.resolve()
    DB_DIR = STATE_DIR / "chroma"
    BASE_DIR = STATE_DIR / "base"
    # cluster memo entries carry their store key (clusters()), so a
    # config switch self-segregates the cache — nothing to clear here


def _neuroignore(path: Path | None) -> frozenset[str]:
    """Extra exclude dir names from a .neuroignore beside the active config
    (project-local: <root>/.neuronav/.neuroignore; shipped profile:
    config/.neuroignore). One dir name per line, '#' comments and blanks
    ignored — user-adjustable without editing the config json."""
    if path is None:
        return frozenset()
    f = path.parent / ".neuroignore"
    if not f.is_file():
        return frozenset()
    # utf-8-sig: a PowerShell-5-written .neuroignore carries a BOM that a
    # plain utf-8 read would fold into the first name as \ufeff — silently
    # re-including what the user excluded (issue #119)
    names = {ln.strip() for ln in f.read_text(encoding="utf-8-sig").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")}
    return frozenset(n for n in names
                     if n not in ("", ".", "..") and "/" not in n and "\\" not in n)
def _gitignore_dirs(root: Path) -> frozenset[str]:
    # issue #296: the walk ignores the target repo's own .gitignore — a
    # machine-specific exclude list can't keep up (venv/target/build/dist/
    # next stay walkable). Reuse the .neuroignore mechanism (one bare dir
    # name per line) against <root>/.gitignore: dir-prune entries only.
    # Skipped: comments, negations (!name — gitignore negation semantics
    # don't map onto a prune union), globs (*?[]\ — no bare name to match
    # against os.walk dirnames), anchored (/name — root-anchored, not a
    # bare name) and interior-slash paths (nested gitignores are a
    # follow-up if they earn the risk). Duplicated names cost nothing.
    try:
        raw = (root / ".gitignore").read_text(encoding="utf-8-sig")  # BOM law (#119)
    except OSError:
        return frozenset()
    names = []
    for line in raw.splitlines():
        line = line.strip().rstrip("/")  # "dir/" and "dir" prune the same
        if not line or line.startswith(("#", "!")) or line.startswith("/"):
            continue
        if any(c in line for c in "*?[]\\") or "/" in line:
            continue
        if line not in (".", ".."):
            names.append(line)
    return frozenset(names)


# walk-everything defaults shared by no-config / project-local legs, the
# onboard scaffold and the root-wide prune floor (issue #27: one literal,
# three consumers; issue #296-C: WALK_DEFAULTS["exclude_dirs"] IS the one
# canonical prune set — _PRUNE_FLOOR derives from it, so the indexed walk,
# iter_root_files and stat_fingerprint can never disagree again; issue #286:
# .tmp/.team_scratch stay here — they are not gitignore-standard, they are
# THIS repo's scratch names). Issue #296-A: a config without include_dirs
# walks everything — include_dirs is strictly opt-in; there is no
# machine-specific directory fallback left to mistarget a foreign repo.
WALK_DEFAULTS = {
    "include_dirs": (".",),
    "exclude_dirs": (".git", ".godot", "__pycache__", ".venv", ".neuronav",
                     "node_modules", ".tmp", ".team_scratch"),
}
# one-shot stderr note when gitignore-sourced prunes hide a large subtree
# (issue #296-B): keeps the #290/#286 oversized-walk guard honest — the
# totals it counts may already be gitignore-pruned. Hermetic (FAKE) runs
# stay silent like the #286 hint.
GITIGNORE_PRUNE_WARN_N = 10_000
_gitignore_counted: set[str] = set()


def use_config(path: Path) -> None:
    """Point this process and its subprocesses at a config profile:
    env first so children inherit, then rebind the module globals."""
    os.environ["NEURONAV_CONFIG"] = str(path)
    _apply_config(path)



CONFIG_PATH: Path | None
COLLECTION: str
INCLUDE_DIRS: tuple[str, ...]
EXTS: set[str]
EXCLUDE_DIRS: frozenset[str]
GITIGNORE_PRUNE_DIRS: frozenset[str]
EMBED_URL: str
EMBED_MODEL: str
EMBED_DIM: int
EMBED_DOC_PREFIX: str
EMBED_PROVIDER: str
EMBED_API_KEY: str
WATCH_INTERVAL_S: float
RECALL_TWO_PASS: bool
CHUNK_CAST: float
FILE_DOC_CAST: float
STATE_DIR: Path
DB_DIR: Path
BASE_DIR: Path
# clusters() result cache (K2/#86, store-keyed for #131): same store +
# engine args + unchanged data -> the same list object, so 10 call-sites
# stop re-running Louvain; alternation cannot cross-wire stores because
# the store key rides every entry (see store_key()).
_clusters_memo: dict[tuple, list] = {}
_apply_config(_discover_config())


# ---- universal mount (issue #131): per-call config scoping -----------------
#
# The MCP server routes each tool call to the caller's `dir` by rebinding
# the config-derived globals above for that call's duration. config_scope
# saves and exact-restores everything config touches — including the
# stat-gate fingerprint slots (each project keeps its own drift baseline
# across alternation) — and swaps graph.py's parsed singleton to the one
# cached for the incoming store, so switching back does not re-parse.
# Chroma clients and write locks are cached per store below. The
# NEURONAV_CONFIG env is deliberately NOT touched: config-file-driven
# runs keep their #91 loud aborts, and subprocesses keep resolving the
# boot config.

_CONFIG_FIELDS = (
    "CONFIG_PATH",
    "ROOT", "COLLECTION", "INCLUDE_DIRS", "EXTS", "EXCLUDE_DIRS", "GITIGNORE_PRUNE_DIRS",
    "EMBED_URL", "EMBED_MODEL", "EMBED_DIM", "EMBED_DOC_PREFIX", "EMBED_PROVIDER",
    "EMBED_API_KEY", "WATCH_INTERVAL_S", "RECALL_TWO_PASS", "CHUNK_CAST",
    "FILE_DOC_CAST",
    "STATE_DIR", "DB_DIR", "BASE_DIR",
)
_GRAPH_CACHE: dict[tuple, object] = {}  # store key -> graph.py singleton
_FP_CACHE: dict[tuple, tuple] = {}  # store key -> fp slots (drift baseline)


def store_key() -> tuple[str, str]:
    """Identity of the store the active config resolves to — the cache key
    for chroma clients, write locks, cluster memos and parsed graphs
    (two configs resolving to one store share its caches by design)."""
    return (str(DB_DIR), COLLECTION)


@contextmanager
def config_scope(path: Path):
    """Serve one call under another config, then restore exact state.

    Pairs with server._route (issue #131): `with nav.config_scope(cfg):`
    rebinds every config-derived global (via _apply_config) plus the
    stat-gate slots, swaps graph.py's singleton for the one cached under
    the incoming store, and on exit puts everything back bit for bit — a
    raised tool error restores just as cleanly. Raises whatever
    _apply_config raises for a bad config (callers pre-validate)."""
    import graph as _graph_mod  # lazy: graph imports nav

    global _fp_clean, _fp_last, _fp_last_scan, _fp_dirty
    saved = {f: globals()[f] for f in _CONFIG_FIELDS}
    with _fp_lock:
        saved_fp = (_fp_clean, _fp_last, _fp_last_scan, _fp_dirty)
    saved_graph = _graph_mod._graph
    applied = False
    try:
        _apply_config(path)
        applied = True
        key = store_key()
        # this store's drift baseline, if a previous scope synced it:
        # entering with the boot baseline would judge every scoped scan
        # against the wrong project (sha-gate catches it, but every
        # routed rescan would pay the full content walk)
        with _fp_lock:
            fp = _FP_CACHE.get(key)
            if fp is not None:
                _fp_clean, _fp_last, _fp_last_scan, _fp_dirty = fp
        _graph_mod._graph = _GRAPH_CACHE.get(key)
        yield
    finally:
        if applied:
            # keep what this scope built for ITS store — and only after a
            # successful apply: a failed one leaves half-rebound globals
            # whose store key belongs to no served config
            key = store_key()
            _GRAPH_CACHE[key] = _graph_mod._graph
            with _fp_lock:
                _FP_CACHE[key] = (_fp_clean, _fp_last, _fp_last_scan, _fp_dirty)
        for f, v in saved.items():
            globals()[f] = v
        with _fp_lock:
            _fp_clean, _fp_last, _fp_last_scan, _fp_dirty = saved_fp
        _graph_mod._graph = saved_graph


def _memo_drop_current() -> None:
    """Invalidate the ACTIVE store's cluster memos (K2/#86): rescan/
    import_base/drop changed one store, so only that store's entries go —
    alternation keeps the other stores' entries warm (issue #131)."""
    key = store_key()
    for k in [k for k in _clusters_memo if k[:2] == key]:
        del _clusters_memo[k]

MAX_EMBED_CHARS = 30_000  # keep under Ollama context; head of .tscn has script links
EMBED_BATCH = 32
UPSERT_BATCH = 64
SHARD_SIZE = 250
MANIFEST_NAME = "manifest.json"

# stat-gate freshness (issue #19): the MCP read tools stat-scan the
# worktree and auto-rescan when it drifted from the last synced
# fingerprint. The walk collects mtime/size only — no read, no hash —
# and is TTL-cached below so bursts of tool calls do not re-stat the
# tree. The rescan behind the gate stays sha-gated, so a touched-but-
# identical file embeds nothing.
# NEURONAV_STAT_TTL_S (issue #286): test-pace knob for the TTL window —
# test_server_stdio's drift legs sleep one window per leg, so the suite
# sets 0.5 and its spawned servers inherit it. Default 3.0 everywhere
# else. Read once at import; in-process overrides patch nav.STAT_TTL_S
# directly (test_autorescan's precedent).
STAT_TTL_S = float(os.environ.get("NEURONAV_STAT_TTL_S") or 3.0)


def _embed_post(chunk: list[str], headers: dict[str, str] | None) -> dict:
    """One embeddings POST with 429 backoff (issue #17): a parseable
    Retry-After is honored (capped at 60s), no header means 1s/2s/4s.
    Any other failure raises loud via raise_for_status."""
    attempt = 0
    while True:
        resp = httpx.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "input": chunk},
            headers=headers,
            timeout=300.0,
        )
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
        if attempt >= 3:
            raise RuntimeError(
                f"{EMBED_PROVIDER} embed still rate-limited (429) after "
                f"{attempt + 1} attempts: {resp.text[:200]}"
            )
        raw = resp.headers.get("Retry-After")
        if raw is None:
            time.sleep(float(2**attempt))
        else:
            try:
                time.sleep(max(min(float(raw), 60.0), 0.0))
            except ValueError:
                time.sleep(float(2**attempt))  # Retry-After as HTTP-date
        attempt += 1


def _fake_embeds() -> bool:
    """#298: =0 means OFF (unset/""/"0" falsy); only a non-"0" value swaps
    in the deterministic hash embeddings. Four sites used to read the env
    raw, so NEURONAV_EMBED_FAKE=0 silently faked every embed."""
    v = os.environ.get("NEURONAV_EMBED_FAKE")
    return v is not None and v != "" and v != "0"


def embed(texts: list[str]) -> list[list[float]]:
    """Batch-embed via the configured provider (issue #17): Ollama
    /api/embed or any OpenAI-compatible /embeddings endpoint. Truncates
    long inputs and chunks requests at EMBED_BATCH (OpenAI caps input
    array length).

    NEURONAV_EMBED_FAKE=1 swaps in deterministic hash embeddings (CI
    plumbing mode): same text -> same vector, so upsert/query/scoping all
    exercise for real while no model server is needed. NOT semantic -
    quality gates stay local with a real Ollama."""
    truncated = [t[:MAX_EMBED_CHARS] for t in texts]
    if _fake_embeds():
        out = []
        for t in truncated:
            rng = random.Random(f"neuronav-fake:{t}")
            out.append([rng.uniform(-1.0, 1.0) for _ in range(EMBED_DIM)])
        return out
    headers = {"Authorization": f"Bearer {EMBED_API_KEY}"} if EMBED_API_KEY else None
    out: list[list[float]] = []
    for i in range(0, len(truncated), EMBED_BATCH):
        chunk = truncated[i : i + EMBED_BATCH]
        data = _embed_post(chunk, headers)
        if EMBED_PROVIDER == "ollama":
            rows = data.get("embeddings")
        else:  # openai: data[i].embedding; row order is not guaranteed
            rows = [r.get("embedding") for r in sorted(data.get("data") or [], key=lambda r: r.get("index", 0))]
        if rows is None or any(r is None for r in rows):
            raise RuntimeError(f"{EMBED_PROVIDER} embed failed: {data}")
        if len(rows) != len(chunk):
            raise RuntimeError(
                f"{EMBED_PROVIDER} returned {len(rows)} embeddings for {len(chunk)} inputs"
            )
        out.extend(rows)
    return out

# Truthful degradation labels (issue #115): an embed-path failure that
# is really a model/config problem must say so — naming the model AND
# provider — instead of reading "backend unreachable" and sending the
# user to restart a server that is fine. Shared by recall.search, the
# server's find_functions fallback, explore's degraded notes and
# _ctx_semantic, so every degraded surface tells the same truth.
_MODEL_ERR_SIG = re.compile(
    r"model[^\n]{0,120}?(?:not\s+found|not\s+supported|unknown|invalid|"
    r"no\s+such|does\s+not\s+exist|missing)"
    r"|(?:not\s+found|not\s+supported|unknown|invalid|no\s+such|"
    r"does\s+not\s+exist|missing)[^\n]{0,120}?model",
    re.I,
)


def embed_failure_reason(exc: Exception) -> str:
    """One-line truthful reason for an embed-path exception, for the
    degraded-mode markers. Classes, in order:

    - the _check_model abort (index built under a different model or
      provider) -> "embed model/config mismatch" carrying the original
      actionable message (it already names both models + providers and
      the drop+rescan fix);
    - HTTP 4xx, or 5xx whose body carries a model-mismatch signature
      (ollama/openai "model not found" etc.) -> names the configured
      model + provider and says model/config error, not connectivity;
    - any other HTTP status -> endpoint error (backend reachable);
    - everything else (connection refused/timeouts) -> the one case
      that legitimately reads "embedding backend unreachable".
    """
    msg = str(exc)
    if "index was built with embed model" in msg:
        return f"embed model/config mismatch — {msg}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        snippet = " ".join((exc.response.text or "").split())[:160]
        if code // 100 == 4 or _MODEL_ERR_SIG.search(snippet):
            out = (
                f"{EMBED_PROVIDER} embed endpoint rejected model "
                f"'{EMBED_MODEL}' (HTTP {code}"
            )
            if snippet:
                out += f": {snippet}"
            return out + ") — model/config error, not connectivity"
        out = f"{EMBED_PROVIDER} embed endpoint error (HTTP {code}"
        if snippet:
            out += f": {snippet}"
        return out + ") — backend reachable, endpoint failing"
    return f"embedding backend unreachable ({type(exc).__name__})"


# issue #286: an oversized BARE walk (no config, non-hermetic, cwd at a
# git root) says so once, mid-walk — before the caller's parse phase,
# where the real cost lands. A hint, never a gate: configured walks and
# FAKE runs stay silent.
WALK_SCOPE_WARN_N = 10_000
_walk_scope_warned = False


def _walk_scope_tick(n: int) -> None:
    global _walk_scope_warned
    if _walk_scope_warned:
        return
    _walk_scope_warned = True
    if (
        CONFIG_PATH is None
        and not _fake_embeds()
        and Path.cwd() == ROOT
        and (ROOT / ".git").is_dir()
    ):
        print(
            f"neuronav: walk scope {n:,} files under bare defaults — pass "
            "NEURONAV_CONFIG or extend exclude_dirs; continuing",
            file=sys.stderr,
        )
def _gitignore_prune_tick(pruned: list[str]) -> None:
    # issue #296: surface large gitignore-sourced prunes once per dir name
    # per process — a fresh consumer whose whole build tree vanished from
    # the index deserves a breadcrumb, not a silent 0-file walk. Mirrors
    # the #286 hint: hermetic FAKE runs and non-repo cwd stay silent.
    if _fake_embeds() or CONFIG_PATH is not None and not (ROOT / ".git").is_dir():
        return
    fresh = [dn for dn in pruned if dn not in _gitignore_counted]
    if not fresh:
        return
    todo = []
    for dn in fresh:
        _gitignore_counted.add(dn)
        todo.append(dn)
    if not todo:
        return
    total = sum(_count_pruned_files(ROOT / dn) for dn in todo)
    if total >= GITIGNORE_PRUNE_WARN_N:
        print(
            f"neuronav: .gitignore prunes ({', '.join(sorted(todo))}) holding "
            f"{total:,} files — not indexed; extend exclude_dirs to override",
            file=sys.stderr,
        )


def _count_pruned_files(base: Path, cap: int = 100_000) -> int:
    # capped file count inside a pruned dir (for the one-shot note above);
    # best-effort — unreadable entries count as zero, never fatal
    n = 0
    stack = [base]
    while stack and n < cap:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        n += 1
                        if n >= cap:
                            break
        except OSError:
            continue
    return n



def iter_files(all_suffixes: bool = False) -> Iterator[Path]:
    # os.walk (not rglob) so exclude_dirs are pruned from the traversal —
    # a repo-root include_dir would otherwise walk .venv/.chroma/etc.
    # Overlapping include_dirs ([".", "tests"]) dedupe on the index key
    # (issue #117): each file yields exactly once — first include wins,
    # order stays the per-dir sorted walk — so a rescan counts it once
    # instead of double-embedding both copies into one upsert batch.
    seen: set[str] = set()
    n = 0
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            if GITIGNORE_PRUNE_DIRS:
                _gitignore_prune_tick(
                    [dn for dn in dirnames if dn in GITIGNORE_PRUNE_DIRS]
                )
            dirnames[:] = sorted(dn for dn in dirnames if dn not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                # all_suffixes (issue #240): same walk rules with the
                # extension filter off — the degraded-boot suffix census
                if all_suffixes or Path(name).suffix in EXTS:
                    p = Path(dirpath) / name
                    fid = file_id(p)
                    if fid in seen:
                        continue
                    seen.add(fid)
                    n += 1
                    if n == WALK_SCOPE_WARN_N:
                        _walk_scope_tick(n)
                    yield p


# issue #240: census cap — the boot guidance's on-disk survey stays
# bounded on huge trees; the sorted walk keeps the cut deterministic
CENSUS_CAP = 50_000


def suffix_census() -> dict[str, int]:
    """File count per suffix under the active walk rules with the
    extension filter IGNORED (issue #240): what the root actually holds.
    Feeds the degraded-boot guidance's paste-ready config and the
    raw-text degradation banner. Extensionless files key as their whole
    name; binary-ish noise is filtered by the consumer. Deterministic —
    sorted walk, capped at CENSUS_CAP files."""
    counts: dict[str, int] = {}
    n = 0
    for p in iter_files(all_suffixes=True):
        n += 1
        if n > CENSUS_CAP:
            break
        key = p.suffix.lower() or p.name
        counts[key] = counts.get(key, 0) + 1
    return counts


# standard cache prune floor for root-wide wiring walks (issue #117) —
# issue #296-C: derived from the one canonical set (WALK_DEFAULTS) so the
# indexed walk (config exclude_dirs + .neuroignore + .gitignore via
# _apply_config) and the root-wide scans can't drift again; consumers
# derive, none re-hardcodes a second list
_PRUNE_FLOOR = frozenset(WALK_DEFAULTS["exclude_dirs"])


def iter_root_files(suffixes: set[str] | frozenset[str]) -> Iterator[Path]:
    """Root-wide pruned walk (sorted, deterministic) for wiring passes —
    the .tres scan's rglob replacement (issue #117): honors the SAME
    exclude contract as iter_files (config exclude_dirs + .neuroignore)
    plus the standard cache prune floor, so .venv/node_modules/.tmp/
    .neuronav are pruned from the traversal instead of read and filtered
    afterwards."""
    prune = EXCLUDE_DIRS | _PRUNE_FLOOR
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(dn for dn in dirnames if dn not in prune)
        for name in sorted(filenames):
            if Path(name).suffix in suffixes:
                yield Path(dirpath) / name


# ---- stat-gate freshness (issue #19) ---------------------------------------

_fp_lock = threading.Lock()
_fp_clean: dict[str, tuple[int, int]] | None = None  # last synced baseline
_fp_last: dict[str, tuple[int, int]] | None = None  # most recent walk
_fp_last_scan = float("-inf")  # monotonic ts of that walk
_fp_dirty = False  # its verdict vs the baseline


def stat_fingerprint() -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) per indexed file, mirroring iter_files' walk
    (same include/exclude/suffix rules). Stat-only, so it is cheap
    enough to run on every read-tool call."""
    fp: dict[str, tuple[int, int]] = {}
    n = 0
    root_len = len(str(ROOT)) + 1
    stack = [ROOT / d for d in INCLUDE_DIRS]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in EXCLUDE_DIRS:
                                stack.append(e.path)
                            continue
                        if Path(e.name).suffix not in EXTS:
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue  # vanished mid-walk; the next scan reconciles
                    fp[e.path[root_len:].replace(os.sep, "/")] = (
                        st.st_mtime_ns,
                        st.st_size,
                    )
                    n += 1
                    if n == WALK_SCOPE_WARN_N:
                        _walk_scope_tick(n)
        except OSError:
            continue  # include_dir vanished; empty is a valid fingerprint
    return fp


def stat_scan(force: bool = False) -> bool:
    """True when the worktree drifted from the synced baseline. The walk
    is TTL-cached: inside STAT_TTL_S the cached verdict comes back
    without touching the filesystem (force bypasses it — the watcher's
    poll). The walked fingerprint is kept for stat_mark_synced."""
    global _fp_last, _fp_last_scan, _fp_dirty
    with _fp_lock:
        if not force and time.monotonic() - _fp_last_scan < STAT_TTL_S:
            return _fp_dirty
    fp = stat_fingerprint()
    with _fp_lock:
        _fp_last = fp
        _fp_last_scan = time.monotonic()
        _fp_dirty = _fp_clean is None or fp != _fp_clean
        return _fp_dirty


def stat_mark_synced() -> None:
    """Record the worktree state the last rescan covered as the clean
    baseline: the fingerprint of the scan that triggered it (edits that
    land mid-rescan then re-dirty on the next scan and converge), or a
    fresh walk when no scan preceded (server startup)."""
    global _fp_clean, _fp_dirty, _fp_last, _fp_last_scan
    with _fp_lock:
        if _fp_last is None:
            _fp_last = stat_fingerprint()
        _fp_clean = _fp_last
        _fp_dirty = False
        _fp_last_scan = time.monotonic()


def file_id(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")



def _db_lock(timeout: float | None = None) -> "FileLock":
    """Advisory cross-process writer lock (server, CLI, viz all write via
    nav functions). One lock per store, cached (issue #131): the
    universal server alternates configs, so the lock must follow the
    store. ``timeout`` bounds the acquire (issue #203 boot hardening);
    None waits forever (the filelock default). Always assigned: the
    instance is cached per store, so a boot-bounded acquire must not
    leak its bound onto later default callers."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    lock = _LOCKS.setdefault(str(DB_DIR), FileLock(str(DB_DIR / ".write.lock")))
    lock.timeout = -1 if timeout is None else timeout
    return lock


_LOCKS: dict[str, "FileLock"] = {}


def _check_model(col: chromadb.Collection) -> chromadb.Collection:
    """Embedding fingerprint on the live collection — model AND provider
    (issue #17 stamped both): a same-name model behind a different
    provider is not guaranteed to be the same vector space, so a
    CHANGED key demands a re-embed; a re-stamp would copy
    wrong-provider vectors verbatim (CodeRabbit hardening on #151).
    A MISSING provider key is lineage, not drift (issue #159): pre-#17
    stores carry the model yet no provider, and the pre-#17 client
    spoke only the Ollama wire protocol — so the stamp heals to
    EMBED_PROVIDER via the re-stamp path when the config is ollama,
    while a provider-less store under any other config is genuine
    drift and still refuses. Mismatch messages print the raw stored
    provider (None reads as unstamped), never a fabricated default.
    Fully unstamped collections (both keys absent) take the copy
    path — nothing contradicts the config.

    The stamp must also keep hnsw:space=cosine (issue #103): chroma's
    modify() REPLACES the metadata dict and refuses hnsw:* keys outright
    ("changing the distance function ... is not supported", verified on
    the pinned 1.5.9), so a bare stamp both wipes the space key and
    cannot restore it — any later rebuild-from-metadata silently falls
    back to l2. A compatible collection missing only stamp/space keys
    is re-created with the full metadata, vectors copied (chroma's f32
    write quantization settles once, <= 1 ulp, then bit-stable),
    temp on the next call."""
    meta = col.metadata or {}
    stored = meta.get("embed_model")
    provider = meta.get("embed_provider")
    # absent provider = pre-#17 lineage: the only client that could
    # have built the store spoke ollama, so that is the effective stamp
    lineage = provider if provider is not None else "ollama"
    if stored is not None and (stored != EMBED_MODEL or lineage != EMBED_PROVIDER):
        raise RuntimeError(
            f"index was built with embed model '{stored}' (provider "
            f"{provider!r}) but config says "
            f"'{EMBED_MODEL}' (provider '{EMBED_PROVIDER}') — run "
            "`python nav.py drop` then rescan"
        )
    if (stored == EMBED_MODEL and provider == EMBED_PROVIDER
            and meta.get("hnsw:space") == "cosine"):
        return _adopt_orphan(col)
    return _restamp(col)


def _adopt_orphan(col: chromadb.Collection) -> chromadb.Collection:
    """Heal a re-stamp that crashed between dropping the source and
    renaming the temp in (CodeRabbit on #151): get_or_create has since
    re-made the name with correct metadata but partial data — it would
    pass _check_model and silently serve a truncated index. A strictly
    richer temp wins the name back; a poorer one is stale garbage from
    a mid-build crash. Double-checked under the write lock so a
    concurrent _restamp builder is never raced."""
    tmp_name = f"{col.name}-restamp"
    try:
        client().get_collection(tmp_name)
    except Exception:
        return col  # no temp: the common path, one cheap lookup
    with _db_lock():
        try:
            tmp = client().get_collection(tmp_name)
            if tmp.count() > col.count():
                n = tmp.count()
                client().delete_collection(col.name)
                tmp.modify(name=col.name)
                print(f"neuronav: adopted orphaned re-stamp temp for "
                      f"'{col.name}' ({n} vectors; a previous repair "
                      "crashed mid-swap)", file=sys.stderr)
                return client().get_collection(col.name)
            client().delete_collection(tmp_name)
        except Exception as e:
            raise RuntimeError(
                f"failed to settle re-stamp temp '{tmp_name}': {e}"
            ) from e
        return col


def _restamp(col: chromadb.Collection,
             embed_mode: str | None = None,
             doc_shape: str | None = None) -> chromadb.Collection:
    """Re-create `col` with the full metadata (issue #103) — the only
    write that keeps hnsw:space, since modify() replaces the dict and
    rejects hnsw:* keys. Build-and-validate before the swap (CodeRabbit
    on #151): the copy lands in a durable '<name>-restamp' temp and is
    count-checked, so an add() failure leaves the source untouched and
    never strands a truncated collection that still passes the checks;
    the cutover is delete + rename, and a crash in between is healed
    by _adopt_orphan from the temp (export_base's manifest-last law is
    the precedent). Vectors are provider output, model+provider-gated
    by _check_model; documents included. Chroma's f32 quantization
    settles once on copy (<= 1 ulp, then bit-stable). Under the
    write lock, reentrant from the export/import callers. ``embed_mode``
    stamps the new collection's vector-space lineage (#220); None (the
    default) preserves the stored key verbatim, and a store that never
    carried one stays unstamped — pre-#220 lineage is real. ``doc_shape``
    is the same law for the doc-construction lineage (#229)."""
    name = col.name
    tmp_name = f"{name}-restamp"
    with _db_lock():
        try:
            # issue #239 rider: retry the hnsw-settle transient so the
            # re-stamp heals instead of tripping the raced-collection
            # fallback on self-healing noise; real failures still fall
            data = col_get_all(
                col, ["embeddings", "documents", "metadatas"], "re-stamp read"
            )
        except Exception as e:
            print(f"neuronav: collection '{name}' needs a metadata re-stamp but "
                  f"reading its vectors failed ({e}); metadata left as-is",
                  file=sys.stderr)
            try:  # a concurrent process may have finished the re-stamp
                raced = client().get_collection(name)
            except Exception:
                raise RuntimeError(
                    f"collection '{name}' vanished during metadata re-stamp"
                ) from e
            m = raced.metadata or {}
            if (m.get("embed_model"), m.get("embed_provider"), m.get("hnsw:space")) != (
                EMBED_MODEL, EMBED_PROVIDER, "cosine"
            ):
                raise RuntimeError(
                    f"collection '{name}' carries foreign metadata after a "
                    f"re-stamp race (embed_model={m.get('embed_model')!r}, "
                    f"embed_provider={m.get('embed_provider')!r}, "
                    f"hnsw:space={m.get('hnsw:space')!r}) — run "
                    "`python nav.py drop` then rescan"
                ) from e
            return raced
        try:
            client().delete_collection(tmp_name)  # stale partial from an earlier crash
        except Exception:
            pass
        mode_key = ((col.metadata or {}).get("embed_mode") if embed_mode is None
                    else embed_mode)
        shape_key = ((col.metadata or {}).get("doc_shape") if doc_shape is None
                     else doc_shape)
        stamp = {"hnsw:space": "cosine", "embed_model": EMBED_MODEL,
                 "embed_provider": EMBED_PROVIDER}
        if mode_key is not None:
            stamp["embed_mode"] = mode_key
        if shape_key is not None:
            stamp["doc_shape"] = shape_key
        tmp = client().create_collection(name=tmp_name, metadata=stamp)
        try:
            for i in range(0, len(data["ids"]), UPSERT_BATCH):
                tmp.add(ids=data["ids"][i : i + UPSERT_BATCH],
                        embeddings=data["embeddings"][i : i + UPSERT_BATCH],
                        documents=data["documents"][i : i + UPSERT_BATCH],
                        metadatas=data["metadatas"][i : i + UPSERT_BATCH])
            if tmp.count() != len(data["ids"]):
                raise RuntimeError(
                    f"re-stamp copy of '{name}' landed {tmp.count()} of "
                    f"{len(data['ids'])} vectors — source untouched, retry"
                )
        except Exception:
            try:
                client().delete_collection(tmp_name)  # never leave a partial temp
            except Exception:
                pass
            raise
        client().delete_collection(name)
        tmp.modify(name=name)
    print(f"neuronav: re-stamped collection '{name}' with full metadata "
          f"(hnsw:space=cosine, #103): {len(data['ids'])} vectors copied",
          file=sys.stderr)
    return client().get_collection(name)



def client() -> "chromadb.PersistentClient":
    """Chroma client at the configured store — one per store for the
    process (issue #131): alternation must not churn handles (deletes
    were observed silently no-op-ing under client churn) and each open
    client holds sqlite resources in its store dir."""
    return _CLIENTS.setdefault(str(DB_DIR), chromadb.PersistentClient(path=str(DB_DIR)))


_CLIENTS: dict[str, "chromadb.PersistentClient"] = {}


def fns_name() -> str:
    """Fn-level sibling collection name — '-fns' rides the main
    collection so per-config stores never mix function vectors."""
    return f"{COLLECTION}-fns"


def _named_collection(name: str) -> chromadb.Collection:
    """Born-correct metadata — fresh stores never need a re-stamp; an
    existing collection keeps its stored metadata and _check_model
    heals stale or wiped stamps (#103). The born stamp records the
    embed mode (#220) and the doc-construction shape (#229) so a later
    rescan in the other mode — or under a different doc shaper —
    refuses to silently reuse the vectors."""
    col = client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine", "embed_model": EMBED_MODEL,
                  "embed_provider": EMBED_PROVIDER, "embed_mode": embed_mode(),
                  "doc_shape": doc_shape()},
    )
    return _check_model(col)


def _collection() -> chromadb.Collection:
    """Main file-level collection."""
    return _named_collection(COLLECTION)


def fns_collection() -> chromadb.Collection:
    """Fn-level sibling (graph.sync_functions / find_functions)."""
    return _named_collection(fns_name())


def chroma_read(what: str, read):
    """Run a chroma read, retrying only the hnsw-settling transient
    (issue #239): right after embedding upserts — the boot rescan or a
    watcher tick — chroma's on-disk hnsw segment can lag the sqlite
    metadata for a moment under load, and a read then fails with
    "Error creating hnsw segment reader: Nothing found on disk" from
    the Rust executor. The segment settles by itself, so the read is
    retried on exactly that signature: a loud stderr note per retry;
    anything else — or exhaustion — raises unchanged. No silent
    degradation, no changed auto-rescan semantics."""
    for pause in _CHROMA_READ_PAUSES_S:
        try:
            return read()
        except Exception as exc:
            if _HNSW_SETTLING not in str(exc):
                raise
            print(
                f"neuronav: chroma read retry ({what}): hnsw segment still "
                f"settling after upserts — next try in {pause:g}s "
                f"({len(_CHROMA_READ_PAUSES_S)} retries max)",
                file=sys.stderr,
            )
            time.sleep(pause)
    return read()


_HNSW_SETTLING = "hnsw segment reader"
_CHROMA_READ_PAUSES_S = (0.5, 1.0, 2.0, 4.0)

GET_CHUNK = 512  # bounded reads (#327): safely under the ~999 SQL
# variable ceiling of old bundled sqlite builds and far under the
# ~32766 of modern ones — an unfiltered col.get() binds every row's
# columns at once and dies with InternalError "too many SQL variables"
# at monorepo scale


def col_get_all(col, include, what="chunked read"):
    """Full-collection read via bounded, deterministically-ordered pages
    (issue #327). One unfiltered col.get() trips the sqlite build's
    bound-variable ceiling on stores past it, so reads page through
    GET_CHUNK-sized chunks (each under the caller's store lock, each
    with the hnsw-settle retry) and merge sorted by id — the result,
    and every export or store-copy built from it, is a function of the
    data alone, not of chroma's internal row order. A row-count
    mismatch across pages is a loud error, never a silent short read."""
    total = col.count()
    rows: list[tuple] = []
    for off in range(0, total, GET_CHUNK):
        got = chroma_read(
            f"{what} (rows {off + 1}..{min(off + GET_CHUNK, total)})",
            lambda off=off: col.get(
                include=include, limit=GET_CHUNK, offset=off
            ),
        )
        rows.extend(zip(got["ids"], *(got[k] for k in include)))
    if len(rows) != total:
        raise RuntimeError(
            f"neuronav: {what} on '{col.name}' merged {len(rows)} of "
            f"{total} rows — the store changed mid-read despite the lock"
        )
    rows.sort(key=lambda r: r[0])
    out: dict[str, list] = {"ids": [r[0] for r in rows]}
    for pos, key in enumerate(include, start=1):
        out[key] = [r[pos] for r in rows]
    return out


def embed_mode() -> str:
    """Vector-space lineage of the current process (#220): "fake" under
    NEURONAV_EMBED_FAKE, else "real". Recorded next to embed_model in
    the collection stamp so a rescan in the OTHER mode force-re-embeds
    instead of silently reusing sha-gated vectors from the wrong space
    (the #219 rig failure: hash-embed bootstrap, real bench, cosine 0)."""
    return "fake" if _fake_embeds() else "real"


def doc_shape() -> str:
    """Doc-construction lineage of the current process (#229): "raw"
    when file-doc shaping is off, else "cast<rev>@<scale>" — the graph
    shaper revision plus the config scale. Stamped next to embed_mode
    (same #220 law, doc side): sha-gating skips re-embeds on unchanged
    BYTES, so a store whose vectors were built from the other doc shape
    must be re-embedded loudly, never silently reused."""
    if FILE_DOC_CAST <= 0.0:
        return "raw"
    import graph  # lazy: the shaper revision lives with the shaper

    return f"cast{graph.FILE_DOC_REV}@{FILE_DOC_CAST:g}"


def rescan(timeout: float | None = None) -> dict[str, int]:
    """Incremental index: add/update changed files, purge deleted ones.
    Warm passes skip read+hash via the stat fingerprint (issue #42); the
    sha stays the content identity, and a mode-mismatched store re-embeds
    everything regardless of shas (issue #220). ``timeout`` bounds the
    cross-process store-lock wait (issue #203): exceeded, the rescan
    aborts loudly naming the lock and the likely holder instead of
    queueing forever."""
    _memo_drop_current()  # embeddings changed — recompute on demand
    lock = _db_lock(timeout)
    try:
        with lock:
            return _rescan_locked()
    except Timeout:
        raise SystemExit(
            f"neuronav: gave up after {timeout:g}s waiting for the store "
            f"write lock {lock.lock_file} — another neuronav process "
            "(server, CLI rescan or viz bake) holds it; a stale MCP server "
            "from a dead session is the usual suspect. End that process "
            "and retry — the lock releases itself when its holder exits."
        ) from None


def _stored_fp(meta: dict) -> tuple[int, int] | None:
    """(mtime_ns, size) persisted at last hash — None on pre-gate entries
    (issue #42): those re-hash once and gain the keys, no migration."""
    try:
        return int(meta["mtime_ns"]), int(meta["size"])
    except (KeyError, TypeError, ValueError):
        return None


def _rescan_locked() -> dict[str, int]:
    import graph as _graph_mod  # lazy: graph imports nav (#229 file-doc shaper)

    files = list(iter_files())
    if not files:
        # issue #41: zero files means the config matches nothing (typo'd
        # root/include_dirs/extensions) — proceeding would report a silent
        # zero-file success and purge the previous walk's entries
        raise RuntimeError(
            f"rescan found 0 files under root={ROOT} "
            f"include_dirs={list(INCLUDE_DIRS)} extensions={sorted(EXTS)} — "
            "fix the config or unset NEURONAV_CONFIG (deliberate wipe: "
            "python nav.py drop)"
        )
    col = _collection()
    # issue #220: the stamp carries the embed mode; a store built in the
    # other mode must re-embed even when file shas are unchanged — an
    # absent key is pre-#220 real lineage, never a mismatch. Loud, never
    # silent: quietly reusing the wrong vector space is the #219 failure.
    # issue #229 extends the same law to the doc-construction shape: the
    # shaper rewrites docs for unchanged bytes, so sha-gating alone would
    # keep serving vectors built from the other shape — an absent key is
    # pre-#229 raw lineage.
    stamped_mode = (col.metadata or {}).get("embed_mode", "real")
    mode = embed_mode()
    shape = doc_shape()
    stamped_shape = (col.metadata or {}).get("doc_shape", "raw")
    reembed_all = stamped_mode != mode or stamped_shape != shape
    if reembed_all:
        why = []
        if stamped_mode != mode:
            why.append(f"{stamped_mode!r}-mode vectors but this rescan embeds "
                       f"{mode!r} (#220)")
        if stamped_shape != shape:
            why.append(f"docs shaped {stamped_shape!r} but this rescan shapes "
                       f"{shape!r} (#229)")
        print(
            f"neuronav: store '{col.name}' re-embedding every file — "
            + "; ".join(why),
            file=sys.stderr,
        )
    existing: dict[str, dict] = {}
    if col.count():
        got = col_get_all(col, ["metadatas"], "rescan existing-rows read")
        existing = {
            rid: (meta or {})
            for rid, meta in zip(got["ids"], got["metadatas"])
        }

    seen: set[str] = set()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    changed_paths: list[str] = []
    pending_ids: list[str] = []
    pending_docs: list[str] = []
    pending_meta: list[dict[str, object]] = []
    refresh_ids: list[str] = []  # touched-but-identical: fingerprint-only update
    refresh_meta: list[dict] = []
    # issue #42: stat-only walk mirroring iter_files' rules — stats equal
    # to the ones persisted at hash time mean the stored sha still holds
    fp = stat_fingerprint()

    def flush() -> None:
        nonlocal pending_ids, pending_docs, pending_meta
        if not pending_ids:
            return
        _report_progress("embed", len(seen), len(files))  # issue #315
        vectors = embed([EMBED_DOC_PREFIX + d for d in pending_docs]) if EMBED_DOC_PREFIX else embed(pending_docs)
        col.upsert(
            ids=pending_ids,
            embeddings=vectors,
            documents=pending_docs,
            metadatas=pending_meta,
        )
        pending_ids, pending_docs, pending_meta = [], [], []
        _report_progress("embed", len(seen), len(files))

    for path in files:
        fid = file_id(path)
        seen.add(fid)
        old = existing.get(fid)
        st = fp.get(fid)
        if (not reembed_all and old is not None and st is not None
                and _stored_fp(old) == st):
            stats["unchanged"] += 1
            continue
        digest = sha256_of(path)
        if not reembed_all and old is not None and old.get("sha") == digest:
            # touched but byte-identical: sha-gated, embeds nothing —
            # refresh the stored fingerprint so the next warm pass skips
            stats["unchanged"] += 1
            if st is not None:
                refresh_ids.append(fid)
                refresh_meta.append({**old, "mtime_ns": st[0], "size": st[1]})
            continue
        text = _read_text(path)
        pending_ids.append(fid)
        # issue #229: the embed doc is the cAST-shaped file doc, not the
        # raw text (raw only under the shape fallbacks). The stored
        # document equals the embed input, so the bench's #220 store
        # coherence check (re-embed stored docs) stays truthful.
        pending_docs.append(_graph_mod.file_doc(path, fid, text, FILE_DOC_CAST))
        # class_name/extends sniffing is gdscript territory — the
        # registry's stat_tags hook answers for whichever language owns
        # the suffix ("" for languages without the notion)
        mod = registry_for(path.suffix)
        cls, ext = mod.stat_tags(text) if mod is not None else ("", "")
        meta: dict[str, object] = {
            "sha": digest,
            "ext": path.suffix,
            "class_name": cls,
            "extends": ext,
        }
        if st is not None:
            meta["mtime_ns"], meta["size"] = st
        pending_meta.append(meta)
        stats["added" if fid not in existing else "updated"] += 1
        changed_paths.append(fid)
        if len(pending_ids) >= UPSERT_BATCH:
            flush()
    flush()
    if refresh_ids:
        col.update(ids=refresh_ids, metadatas=refresh_meta)

    # issue #118: `existing` iterates in chroma insertion order (store
    # history) — sort so the purge list, like the store's data, is a
    # function of what is deleted, not of how the store grew
    deleted = sorted(fid for fid in existing if fid not in seen)
    if deleted:
        col.delete(ids=deleted)
        stats["deleted"] = len(deleted)
    if reembed_all:
        # stamp the healed store so the next same-mode/shape pass is cheap
        col = _restamp(col, embed_mode=mode, doc_shape=shape)
    stats["changed"] = changed_paths
    stats["deleted_paths"] = deleted
    return stats


def search(
    query: str,
    k: int = 12,
    two_pass: bool = False,
    graph_boost: float | None = None,
) -> list[dict[str, object]]:
    """Hybrid recall: chroma vector ranks fused (reciprocal-rank fusion,
    k=30) with BM25F lexical ranks over the structural graph; each hit
    carries bidirectional 1-hop context labels.

    Hit keys: file, score, src ("vec"|"bm25"|"both"), ctx (<=3 neighbor
    paths) + class_name/extends/ext. two_pass/graph_boost pass straight
    through to recall.search (issues #74/#73/#228 — the server's
    semantic_search exposes them on the wire); graph_boost None rides
    the shipped recall default (λ 0.25 — the #228 grid winner), 0.0 is
    the explicit off wire. If the vector side is unavailable the results
    degrade LOUDLY to BM25F-only (stderr warning + ``degraded: True``
    on every hit) — see recall.search.
    """
    return recall.search(query, k=k, two_pass=two_pass, graph_boost=graph_boost)


def count() -> int:
    return _collection().count()


def clusters(
    k: int = 6,
    min_sim: float = 0.6,
    split_sim: float = 0.65,
    blob_min: int = 60,
) -> list[dict[str, object]]:
    """Subsystem clusters: Louvain community detection over a hybrid
    weighted graph — mutual-kNN embedding sims (weight = sim * 0.7) +
    structural edges from graph.py (call/signal capped 5 per file pair,
    attach/inst 1.5); tests/ files get their own community, loose files
    join the community dominating their dir seed (see
    clusters.communities_graph). Resolution swept {1.0: 49 clusters/
    largest 131, 1.2: 35/74, 1.5: 29/69, 1.8: 30/70} — the knob lives
    in clusters.communities_graph (default 1.0). Mega-blobs are then
    split + every cluster labeled (see clusters.finalize).
    Returns [{id, size, paths: [(path, class_name)], label, confidence,
    method}]. Memoized (K2/#86): callers share ONE list per (store, engine-args)
    tuple while that store is unchanged — rescan()/import_base()/`drop`
    drop the store's entries, and a profile switch cannot cross-wire
    stores because the store key rides every entry (issue #131). Treat
    the returned list as read-only."""
    memo_key = store_key() + (k, min_sim, split_sim, blob_min)
    hit = _clusters_memo.get(memo_key)
    if hit is not None:
        return hit
    import numpy as np

    col = _collection()
    if col.count() == 0:
        return []
    got = chroma_read("clusters", lambda: col.get(include=["metadatas", "embeddings"]))
    # issue #118: chroma returns ids in insertion order — a function of
    # store HISTORY, not data (a fresh store and a grown one over the
    # same files disagree). Sort every column by id so union-find roots,
    rows = sorted(
        zip(
            got["ids"],
            got.get("embeddings") if got.get("embeddings") is not None else [],
            got.get("metadatas") or [],
        ),
        key=lambda r: r[0],
    )
    ids = [r[0] for r in rows]
    embs = [r[1] for r in rows]
    metas = [r[2] for r in rows]
    mat = np.array([e.tolist() if hasattr(e, "tolist") else e for e in embs], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat /= norms
    sim = mat @ mat.T
    np.fill_diagonal(sim, -1.0)
    import clusters as _clusters

    knn = _clusters.topk_desc(sim, k)

    # louvain hybrid: structural edges + embedding sims; adj feeds the
    # labeler's autoload hub gating, units keep scene+script welds
    # intact through finalize's embedding split passes
    out, adj, units = _clusters.communities_graph(
        ids, metas, mat, sim, knn, min_sim=min_sim
    )
    out.sort(key=lambda c: -int(c["size"]))
    for idx, c in enumerate(out):
        c["id"] = idx

    result = _clusters.finalize(
        out, ids, mat, split_sim=split_sim, blob_min=blob_min, adj=adj, units=units
    )
    _clusters_memo[memo_key] = result
    return result


# ---- base index (tracked shards) -------------------------------------------


def export_base() -> dict[str, object]:
    """Dump ids+embeddings+metadata to tracked gz shards. No doc text
    in the shards — import_base upserts ids+embeddings+metadatas only,
    so imported rows carry no documents until a rescan re-embeds them
    (#298: the old "import re-attaches from the checkout" claim was
    false; the bench coherence check crashes on the None documents).
    Commit safety (issue #102): the complete generation is staged in a
    SIBLING dir (state/base.tmp-export) and validated before the live
    base is touched at all, then committed by a whole-directory swap
    with rollback — no per-file mutation of the active base, so a
    failed run (mid-write, mid-swap, cleanup) always leaves the
    previous base byte-intact. The gzip header is mtime-pinned and the
    row json canonical, so the same store re-exports byte-identical
    shards."""
    with _db_lock():
        col = _collection()
        if col.count() == 0:
            raise RuntimeError("nothing indexed — run rescan first")
        got = col_get_all(
            col, ["metadatas", "embeddings"], "base export"
        )
        embeddings = got.get("embeddings")
        embeddings = [] if embeddings is None else list(embeddings)
        metadatas = got.get("metadatas")
        metadatas = [] if metadatas is None else list(metadatas)
        rows = sorted(
            (
                (
                    rid,
                    emb.tolist() if hasattr(emb, "tolist") else list(emb),
                    meta,
                )
                for rid, emb, meta in zip(got["ids"], embeddings, metadatas)
            ),
            key=lambda r: r[0],
        )
        staging = BASE_DIR.parent / "base.tmp-export"
        prev = BASE_DIR.parent / "base.prev-export"
        # self-heal leftovers from a hard-killed run: staging is always
        # garbage; a surviving prev with no live base means the kill
        # landed between the two commit renames — prev IS the last
        # committed generation, so restore it
        if staging.exists():
            shutil.rmtree(staging)
        if prev.exists():
            if (BASE_DIR / MANIFEST_NAME).is_file():
                shutil.rmtree(prev)
            else:
                os.replace(prev, BASE_DIR)
        staging.mkdir(parents=True)
        shards = 0
        for i in range(0, len(rows), SHARD_SIZE):
            chunk = rows[i : i + SHARD_SIZE]
            shard = staging / f"shard-{shards:04d}.jsonl.gz"
            # mtime=0 pins the gzip header (no timestamp; the only other
            # variable field, the shard's own basename, is already stable)
            with gzip.GzipFile(
                filename=str(shard), mode="wb", compresslevel=9, mtime=0
            ) as f:
                for rid, emb, meta in chunk:
                    f.write(
                        (
                            json.dumps(
                                {"id": rid, "emb": emb, "meta": meta},
                                ensure_ascii=False,
                                # chroma hands metadatas back in no
                                # guaranteed key order — canonicalize or
                                # the shard bytes wobble between exports
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
            shards += 1
        manifest = {
            "model": EMBED_MODEL,
            "dim": EMBED_DIM,
            "provider": EMBED_PROVIDER,
            "count": len(rows),
            "shards": shards,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        # manifest last: it exists only once every shard row is on disk
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
            newline="\n",  # issue #118: tracked file — pin LF cross-platform
        )
        # validate the generation is complete BEFORE touching the live
        # base — exactly the shards the manifest claims, all non-empty
        staged = sorted(p.name for p in staging.iterdir())
        if (
            MANIFEST_NAME not in staged
            or len(staged) != shards + 1
            or any((staging / n).stat().st_size == 0 for n in staged)
        ):
            raise RuntimeError("incomplete export staging — base left untouched")
        # commit: swap whole directories. Any exception rolls the
        # previous base back into place; only a hard kill can land in
        # the two-rename window, and the self-heal above restores it on
        # the next run
        moved_live = False
        try:
            if BASE_DIR.is_dir():
                os.replace(BASE_DIR, prev)
                moved_live = True
            os.replace(staging, BASE_DIR)
        except BaseException:
            if moved_live and not BASE_DIR.is_dir():
                os.replace(prev, BASE_DIR)
            raise
        # old generation is no longer live — removal is best-effort;
        # debris never affects reads and the self-heal reaps it
        if prev.is_dir():
            shutil.rmtree(prev, ignore_errors=True)
        return manifest


def import_base() -> dict[str, int | str]:
    """Seed local chroma from tracked shards. Skips ids whose files no
    longer exist (deleted/renamed since export) — rescan heals the rest."""
    _memo_drop_current()  # store repopulated — recompute on demand
    with _db_lock():
        col = _collection()
        if col.count():
            return {"skipped": col.count()}
        manifest_path = BASE_DIR / MANIFEST_NAME
        if not manifest_path.is_file():
            return {"skipped": 0}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        dim = manifest.get("dim")
        # stamps gate when present (#159): a manifest without dim is not
        # a mismatch — the model is the fingerprint, the shards carry
        # the true vectors — and the message shows raw stored values
        m_prov = manifest.get("provider")
        # #298 D4: a same-name model behind a different provider is not
        # guaranteed to be the same vector space (#17/#159 — _check_model
        # already treats this as drift for live stores); None-safe so
        # pre-stamp manifests keep importing
        if (
            manifest.get("model") != EMBED_MODEL
            or (dim is not None and dim != EMBED_DIM)
            or (m_prov is not None and m_prov != EMBED_PROVIDER)
        ):
            raise RuntimeError(
                f"base index model mismatch: {manifest.get('model')}/{dim} "
                f"(provider {manifest.get('provider')!r}) vs config "
                f"{EMBED_MODEL}/{EMBED_DIM} (provider '{EMBED_PROVIDER}') — run "
                "`python nav.py drop` then rescan"
            )
        ids: list[str] = []
        embs: list[list[float]] = []
        metas: list[dict[str, str]] = []
        for shard in sorted(BASE_DIR.glob("shard-*.jsonl.gz")):
            with gzip.open(shard, "rt", encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    if not (ROOT / row["id"]).is_file():
                        continue
                    ids.append(row["id"])
                    embs.append(row["emb"])
                    metas.append(row["meta"])
        for i in range(0, len(ids), UPSERT_BATCH):
            col.upsert(
                ids=ids[i : i + UPSERT_BATCH],
                embeddings=embs[i : i + UPSERT_BATCH],
                metadatas=metas[i : i + UPSERT_BATCH],
            )
        return {"imported": len(ids), "manifest_count": int(manifest.get("count", 0)),
                "exported_at": str(manifest.get("exported_at", ""))}


_NO_ARG_COMMANDS = ("rescan", "count", "export-base", "import-base",
                    "crosstalk", "drop")  # `search` alone takes free text


def _reject_trailing(cmd: str, extra: list[str]) -> None:
    """Loud rejection of unconsumed trailing argv (issue #332): the
    no-argument subcommands used to ignore extra argv silently, so
    `nav.py rescan --config X` ran the PURE-DEFAULTS rescan — walking
    the cwd and writing <cwd>/.neuronav, the #91 wipe-door class
    reachable by an argument-order slip. Name the token and the correct
    global-prefix form instead; no positional tolerance, no fallback."""
    print(
        f"nav.py {cmd}: unrecognized argument(s): {' '.join(extra)}\n"
        "`--config <path>` is a global prefix, not a trailing flag — "
        f"use: nav.py --config <path> {cmd}",
        file=sys.stderr,
    )
    sys.exit(2)


def _cli(argv: list[str] | None = None) -> None:
    """CLI dispatch (nav.py <cmd>); argv override for in-process tests (#298)."""
    # issue #119: a direct run piped through a cp1252/ascii console raises
    # UnicodeEncodeError the moment a hit path is non-ASCII — the CLI is a
    # console program, so force UTF-8 output regardless of the locale (the
    # -X utf8 flag only arrives when launched via a wired onboard entry)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--config":
        # switch to a second config (self-index etc.) before running:
        # rebind globals + set NEURONAV_CONFIG so sibling modules (graph.py,
        # clusters.py) and subprocesses resolve the same root/collection
        if len(argv) < 3:
            print("usage: nav.py --config <path> <command>", file=sys.stderr)
            sys.exit(2)
        cfg_file = Path(argv[1])
        if not cfg_file.is_file():
            print(f"config not found: {cfg_file}", file=sys.stderr)
            sys.exit(2)
        use_config(cfg_file)
        argv = argv[2:]
    cmd = argv[0] if argv else "rescan"
    if cmd in _NO_ARG_COMMANDS and len(argv) > 1:  # issue #332
        _reject_trailing(cmd, argv[1:])
    if cmd == "rescan":
        t0 = time.perf_counter()
        s = rescan()
        dt = time.perf_counter() - t0
        print(f"{s} in {dt:.1f}s, total={count()}")
    elif cmd == "search":
        # #298 D1: join the sliced argv — sys.argv still carries the
        # "--config <cfg> search" prefix, feeding the config path INTO
        # the query (reproduced: 12 spurious bm25 hits on "json"/"search")
        for h in search(" ".join(argv[1:])):
            ctx = f"  ctx=[{', '.join(h['ctx'])}]" if h["ctx"] else ""
            print(f"{h['score']:0.4f}  {h['file']}  src={h['src']}{ctx}")
    elif cmd == "count":
        print(count())
    elif cmd == "export-base":
        print(json.dumps(export_base(), indent=2))
    elif cmd == "import-base":
        print(json.dumps(import_base(), indent=2))
    elif cmd == "crosstalk":
        # coupling-hotspot report: cross-cluster structural edges
        import clusters as _clusters
        import graph as _graph

        rep = _clusters.crosstalk(clusters(), _graph.get_graph())
        print(_clusters.fmt_crosstalk(rep, align=True))
    elif cmd == "drop":
        _memo_drop_current()  # the store is going away
        cl = client()
        for name in (COLLECTION, fns_name()):
            try:
                cl.delete_collection(name)
                print(f"dropped {name}")
            except chromadb.errors.NotFoundError:
                print(f"{name}: not present")
            except Exception as e:
                # #298: a Windows file lock / IO error is NOT "not present" —
                # swallowing it here reads as a successful drop and the user
                # debugs a store that was never dropped
                raise RuntimeError(
                    f"drop: deleting collection {name!r} failed: {e}"
                ) from e
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _cli()
