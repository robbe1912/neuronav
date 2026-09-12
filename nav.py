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
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import chromadb
from filelock import FileLock
import httpx

# hybrid recall (BM25F + reciprocal-rank fusion + 1-hop context). Module-
# level so the python extractor's import liveness keeps recall.py's funcs
# alive in the self-index; recall.py binds nav/graph lazily inside its
# functions, so `nav.py --config <profile> ...` still switches profiles.
import recall

TOOL_DIR = Path(__file__).resolve().parent


def _discover_config() -> Path | None:
    """Issue #27 discovery: env beats project-local beats checkout-local.
    Returns ``None`` when nothing applies -> caller uses pure defaults."""
    env = os.environ.get("NEURONAV_CONFIG")
    if env:
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
    global ROOT, COLLECTION, INCLUDE_DIRS, EXTS, EXCLUDE_DIRS, EMBED_URL, EMBED_MODEL, EMBED_DIM, EMBED_PROVIDER, EMBED_API_KEY, WATCH_INTERVAL_S, RECALL_TWO_PASS, STATE_DIR, DB_DIR, BASE_DIR
    if path is not None and not path.is_file():
        # issue #41: an explicit config path is a contract, not a hint —
        # silently degrading to walk-all defaults flips the walk identity
        # and the next rescan purges the previous profile's entries
        raise SystemExit(
            f"NEURONAV_CONFIG points at '{path}', which does not exist — "
            "unset the variable or point it at a real config json "
            "(onboard.py init writes one)"
        )
    cfg: dict = json.loads(path.read_text(encoding="utf-8")) if path is not None else {}
    # lazy import: extractors pulls graph-ish deps only for the suffix list
    from extractors import EXTENSIONS as _REGISTERED
    ROOT = Path(cfg.get("root") or Path.cwd())
    if not ROOT.is_absolute():
        # relative roots resolve against the config file's own directory,
        # so shipped profiles (config/neuronav.json) stay machine-portable
        ROOT = (path.parent / ROOT).resolve()
    COLLECTION = str(cfg.get("collection", "main"))
    # project-local / no-config defaults walk everything (issue #27); the
    # legacy install-config default keeps the original target-repo shape
    _walk_all = path is None or (path.parent.name == ".neuronav")
    INCLUDE_DIRS = tuple(cfg.get("include_dirs", WALK_DEFAULTS["include_dirs"] if _walk_all else ("scripts", "scenes", "VFX", "ai", "tests", "tools")))
    EXTS = set(cfg.get("extensions", sorted(_REGISTERED) if _walk_all else (".gd", ".tscn")))
    EXCLUDE_DIRS = frozenset(cfg.get("exclude_dirs", WALK_DEFAULTS["exclude_dirs"] if _walk_all else (".git", "__pycache__")))
    EXCLUDE_DIRS |= _neuroignore(path)
    EMBED_URL = str(cfg.get("embed_url", "http://127.0.0.1:11434/api/embed"))
    EMBED_MODEL = str(cfg.get("embed_model", "qwen3-embedding:0.6b"))
    EMBED_DIM = int(cfg.get("embed_dim", 1024))
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
    names = {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")}
    return frozenset(n for n in names
                     if n not in ("", ".", "..") and "/" not in n and "\\" not in n)
# walk-everything defaults shared by the no-config / project-local legs
# (issue #27) and the onboard scaffold — one literal, three consumers.
WALK_DEFAULTS = {
    "include_dirs": (".",),
    "exclude_dirs": (".git", "__pycache__", ".venv", ".neuronav", "node_modules"),
}


def use_config(path: Path) -> None:
    """Point this process and its subprocesses at a config profile:
    env first so children inherit, then rebind the module globals."""
    os.environ["NEURONAV_CONFIG"] = str(path)
    _apply_config(path)



ROOT: Path
COLLECTION: str
INCLUDE_DIRS: tuple[str, ...]
EXTS: set[str]
EXCLUDE_DIRS: frozenset[str]
EMBED_URL: str
EMBED_MODEL: str
EMBED_DIM: int
EMBED_PROVIDER: str
EMBED_API_KEY: str
WATCH_INTERVAL_S: float
RECALL_TWO_PASS: bool
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
    "ROOT", "COLLECTION", "INCLUDE_DIRS", "EXTS", "EXCLUDE_DIRS",
    "EMBED_URL", "EMBED_MODEL", "EMBED_DIM", "EMBED_PROVIDER",
    "EMBED_API_KEY", "WATCH_INTERVAL_S", "RECALL_TWO_PASS",
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
STAT_TTL_S = 3.0


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
    if os.environ.get("NEURONAV_EMBED_FAKE"):
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


def iter_files() -> Iterator[Path]:
    # os.walk (not rglob) so exclude_dirs are pruned from the traversal —
    # a repo-root include_dir would otherwise walk .venv/.chroma/etc.
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(dn for dn in dirnames if dn not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                if Path(name).suffix in EXTS:
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


def _class_name(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("class_name "):
            return s.split(None, 1)[1].split()[0]
    return ""


def _extends(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("extends "):
            return s.split(None, 1)[1].split()[0]
    return ""


def _db_lock() -> "FileLock":
    """Advisory cross-process writer lock (server, CLI, viz all write via
    nav functions). One lock per store, cached (issue #131): the
    universal server alternates configs, so the lock must follow the
    store rather than pin whichever was touched first. Readers skip it;
    sqlite handles the rest."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return _LOCKS.setdefault(str(DB_DIR), FileLock(str(DB_DIR / ".write.lock")))


_LOCKS: dict[str, "FileLock"] = {}


def _check_model(col: chromadb.Collection) -> chromadb.Collection:
    """Embedding fingerprint on the live collection — model AND provider
    (issue #17 stamped both): a same-name model behind a different
    provider is not guaranteed to be the same vector space, so a
    changed or missing key demands a re-embed; a re-stamp would copy
    wrong-provider vectors verbatim (CodeRabbit hardening on #151).
    Unstamped collections (pre-#17, both keys absent) take the copy
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
    if stored is not None and (stored != EMBED_MODEL
                               or meta.get("embed_provider") != EMBED_PROVIDER):
        raise RuntimeError(
            f"index was built with embed model '{stored}' (provider "
            f"'{meta.get('embed_provider', 'ollama')}') but config says "
            f"'{EMBED_MODEL}' (provider '{EMBED_PROVIDER}') — run "
            "`python nav.py drop` then rescan"
        )
    if stored == EMBED_MODEL and meta.get("hnsw:space") == "cosine":
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


def _restamp(col: chromadb.Collection) -> chromadb.Collection:
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
    write lock, reentrant from the export/import callers."""
    name = col.name
    tmp_name = f"{name}-restamp"
    with _db_lock():
        try:
            data = col.get(include=["embeddings", "documents", "metadatas"])
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
        tmp = client().create_collection(
            name=tmp_name,
            metadata={"hnsw:space": "cosine", "embed_model": EMBED_MODEL,
                      "embed_provider": EMBED_PROVIDER},
        )
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
    heals stale or wiped stamps (#103)."""
    col = client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine", "embed_model": EMBED_MODEL,
                  "embed_provider": EMBED_PROVIDER},
    )
    return _check_model(col)


def _collection() -> chromadb.Collection:
    """Main file-level collection."""
    return _named_collection(COLLECTION)


def fns_collection() -> chromadb.Collection:
    """Fn-level sibling (graph.sync_functions / find_functions)."""
    return _named_collection(fns_name())


def rescan() -> dict[str, int]:
    """Incremental index: add/update changed files, purge deleted ones.
    Warm passes skip read+hash via the stat fingerprint (issue #42); the
    sha stays the content identity."""
    _memo_drop_current()  # embeddings changed — recompute on demand
    with _db_lock():
        return _rescan_locked()


def _stored_fp(meta: dict) -> tuple[int, int] | None:
    """(mtime_ns, size) persisted at last hash — None on pre-gate entries
    (issue #42): those re-hash once and gain the keys, no migration."""
    try:
        return int(meta["mtime_ns"]), int(meta["size"])
    except (KeyError, TypeError, ValueError):
        return None


def _rescan_locked() -> dict[str, int]:
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
    existing: dict[str, dict] = {}
    if col.count():
        got = col.get(include=["metadatas"])
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
        vectors = embed(pending_docs)
        col.upsert(
            ids=pending_ids,
            embeddings=vectors,
            documents=pending_docs,
            metadatas=pending_meta,
        )
        pending_ids, pending_docs, pending_meta = [], [], []

    for path in files:
        fid = file_id(path)
        seen.add(fid)
        old = existing.get(fid)
        st = fp.get(fid)
        if old is not None and st is not None and _stored_fp(old) == st:
            stats["unchanged"] += 1
            continue
        digest = sha256_of(path)
        if old is not None and old.get("sha") == digest:
            # touched but byte-identical: sha-gated, embeds nothing —
            # refresh the stored fingerprint so the next warm pass skips
            stats["unchanged"] += 1
            if st is not None:
                refresh_ids.append(fid)
                refresh_meta.append({**old, "mtime_ns": st[0], "size": st[1]})
            continue
        text = _read_text(path)
        pending_ids.append(fid)
        pending_docs.append(text)
        meta: dict[str, object] = {
            "sha": digest,
            "ext": path.suffix,
            "class_name": _class_name(text),
            "extends": _extends(text),
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

    deleted = [fid for fid in existing if fid not in seen]
    if deleted:
        col.delete(ids=deleted)
        stats["deleted"] = len(deleted)
    stats["changed"] = changed_paths
    stats["deleted_paths"] = deleted
    return stats


def search(query: str, k: int = 12) -> list[dict[str, object]]:
    """Hybrid recall: chroma vector ranks fused (reciprocal-rank fusion,
    k=60) with BM25F lexical ranks over the structural graph; each hit
    carries bidirectional 1-hop context labels.

    Hit keys: file, score, src ("vec"|"bm25"|"both"), ctx (<=3 neighbor
    paths) + class_name/extends/ext. If the vector side is unavailable
    the results degrade LOUDLY to BM25F-only (stderr warning +
    ``degraded: True`` on every hit) — see recall.search.
    """
    return recall.search(query, k=k)


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
    got = col.get(include=["metadatas", "embeddings"])
    ids = list(got["ids"])
    embs = got.get("embeddings")
    metas = list(got.get("metadatas") or [])
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
    (git has the file contents; import re-attaches from the checkout).
    Atomic swap (issue #102): every new file lands via one os.replace
    (manifest last, the coherence marker) and stale shards are removed
    only afterward, so a failed run never destroys the previous base —
    and the gzip header is mtime-pinned, so the same store re-exports
    byte-identical shards."""
    with _db_lock():
        col = _collection()
        if col.count() == 0:
            raise RuntimeError("nothing indexed — run rescan first")
        got = col.get(include=["metadatas", "embeddings"])
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
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BASE_DIR / "tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        shards = 0
        for i in range(0, len(rows), SHARD_SIZE):
            chunk = rows[i : i + SHARD_SIZE]
            shard = tmp / f"shard-{shards:04d}.jsonl.gz"
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
        (tmp / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        # swap-in (issue #102): os.replace is the portable atomic
        # overwrite — Path.rename onto an existing target raises
        # WinError 183 on Windows, which killed every second export
        # after the old shards were already gone. All new files land
        # first, nothing existing is deleted until then.
        names = sorted(p.name for p in tmp.iterdir())
        for name in names:
            if name != MANIFEST_NAME:
                os.replace(tmp / name, BASE_DIR / name)
        os.replace(tmp / MANIFEST_NAME, BASE_DIR / MANIFEST_NAME)
        for old in BASE_DIR.glob("shard-*.jsonl.gz"):
            if old.name not in names:
                old.unlink()
        tmp.rmdir()
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
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("model") != EMBED_MODEL or manifest.get("dim") != EMBED_DIM:
            raise RuntimeError(
                f"base index model mismatch: {manifest.get('model')}/"
                f"{manifest.get('dim')} (provider "
                f"'{manifest.get('provider', 'ollama')}') vs config "
                f"{EMBED_MODEL}/{EMBED_DIM} (provider '{EMBED_PROVIDER}')"
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

if __name__ == "__main__":
    argv = list(sys.argv[1:])
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
    if cmd == "rescan":
        t0 = time.perf_counter()
        s = rescan()
        dt = time.perf_counter() - t0
        print(f"{s} in {dt:.1f}s, total={count()}")
    elif cmd == "search":
        for h in search(" ".join(sys.argv[2:])):
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
            except Exception:
                print(f"{name}: not present")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)
