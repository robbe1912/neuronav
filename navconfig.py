"""navconfig — the config leaf of the old nav.py monolith (issue #344).

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

THE SPLIT LAW (issue #344): every config-derived global below is
REBOUND by _apply_config / config_scope, so sibling leaves (navstore,
navindex) and every importer must read them as THIS module's attributes
(``navconfig.ROOT``) — never ``from navconfig import ROOT``, which
freezes the import-time binding and goes stale the moment a second
config scopes the process. Only code inside this module may use the
bare names.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from extractors import WALK_EXTS

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
# canonical prune set — the indexed walk's _PRUNE_FLOOR derives from it,
# so the indexed walk, iter_root_files and the stat fingerprint can never
# disagree again; issue #286: .tmp/.team_scratch stay here — they are not
# gitignore-standard, they are THIS repo's scratch names). Issue #296-A:
# a config without include_dirs walks everything — include_dirs is
# strictly opt-in; there is no machine-specific directory fallback left
# to mistarget a foreign repo.
WALK_DEFAULTS = {
    "include_dirs": (".",),
    "exclude_dirs": (".git", ".godot", "__pycache__", ".venv", ".neuronav",
                     "node_modules", ".tmp", ".team_scratch"),
}


def _apply_config(path: Path | None) -> None:
    """(Re)bind the config-derived module globals. Called once at import
    and again by ``nav.py --config <path>`` (which also sets
    NEURONAV_CONFIG so subprocesses and sibling modules agree).
    ``path=None`` means no config anywhere: pure cwd defaults (#27)."""
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
    # snippet:") prepended to the EMBED_EMBEDDED document only — stored
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
    # applied to the FILE docs (micro-fn merge into class context,
    # monster-fn split at block boundaries, signature first, everything
    # under MAX_EMBED_CHARS). 1.0 ships ON (the fresh-store #229 bench
    # A/B is the winning wire); 0.0 = raw file text, the byte-identical
    # pre-#229 surface; other positives scale the fn-layer thresholds.
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
    # cluster memo entries carry their store key (navstore.clusters), so
    # a config switch self-segregates the cache — nothing to clear here


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
_apply_config(_discover_config())


# ---- universal mount (issue #131): per-call config scoping -----------------
#
# The MCP server routes each tool call to the caller's `dir` by rebinding
# the config-derived globals above for that call's duration. config_scope
# saves and exact-restores everything config touches — including the
# stat-gate fingerprint slots (each project keeps its own drift baseline
# across alternation) — and swaps graph.py's parsed singleton to the one
# cached for the incoming store, so switching back does not re-parse.
# Chroma clients and write locks are cached per store in navstore. The
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

    Pairs with server._route (issue #131): `with
    navconfig.config_scope(cfg):` rebinds every config-derived global
    (via _apply_config) plus the stat-gate slots, swaps graph.py's
    singleton for the one cached under the incoming store, and on exit
    puts everything back bit for bit — a raised tool error restores just
    as cleanly. Raises whatever _apply_config raises for a bad config
    (callers pre-validate)."""
    import graph as _graph_mod  # lazy: graph imports the nav leaves
    import navindex  # lazy: owns the stat-gate slots this scope swaps

    saved = {f: globals()[f] for f in _CONFIG_FIELDS}
    with navindex._fp_lock:
        saved_fp = (navindex._fp_clean, navindex._fp_last,
                    navindex._fp_last_scan, navindex._fp_dirty)
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
        with navindex._fp_lock:
            fp = _FP_CACHE.get(key)
            if fp is not None:
                (navindex._fp_clean, navindex._fp_last,
                 navindex._fp_last_scan, navindex._fp_dirty) = fp
        _graph_mod._graph = _GRAPH_CACHE.get(key)
        yield
    finally:
        if applied:
            # keep what this scope built for ITS store — and only after a
            # successful apply: a failed one leaves half-rebound globals
            # whose store key belongs to no served config
            key = store_key()
            _GRAPH_CACHE[key] = _graph_mod._graph
            with navindex._fp_lock:
                _FP_CACHE[key] = (navindex._fp_clean, navindex._fp_last,
                                  navindex._fp_last_scan, navindex._fp_dirty)
        for f, v in saved.items():
            globals()[f] = v
        with navindex._fp_lock:
            (navindex._fp_clean, navindex._fp_last,
             navindex._fp_last_scan, navindex._fp_dirty) = saved_fp
        _graph_mod._graph = saved_graph
