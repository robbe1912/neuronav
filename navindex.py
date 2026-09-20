"""navindex — the index leaf of the old nav.py monolith (issue #344).

The rescan walk (walkguard #117 laws, gitignore pruning #296), the
stat-gate freshness machinery (issue #19), the incremental rescan
itself, the tracked base-index import/export (issue #102), and the
build-observability hook registry (issue #315).

SPLIT LAWS (issue #344):
- config-derived globals are REBINDABLE — read as ``navconfig.X``
  attributes, never from-imports;
- store/embed calls go through ``navstore`` attributes (``navstore.embed``,
  ``navstore._db_lock``) so in-process test patching on navstore keeps
  working exactly as patching ``nav.embed`` did;
- the stat-gate slots (_fp_*) and their lock live HERE and only here —
  navconfig.config_scope swaps them per store through this module.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Iterable, Iterator, Set
from datetime import UTC, datetime
from pathlib import Path

from filelock import Timeout

import navconfig
import navstore
from extractors import registry_for

SHARD_SIZE = 250
MANIFEST_NAME = "manifest.json"

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

# stat-gate freshness (issue #19): the MCP read tools stat-scan the
# worktree and auto-rescan when it drifted from the last synced
# fingerprint. The walk collects mtime/size only — no read, no hash —
# and is TTL-cached below so bursts of tool calls do not re-stat the
# tree. The rescan behind the gate stays sha-gated, so a touched-but-
# identical file embeds nothing.
# NEURONAV_STAT_TTL_S (issue #286): test-pace knob for the TTL window —
# test_server_stdio's drift legs sleep one window per leg, so the suite
# sets 0.5 and its spawned servers inherit it. Default 3.0 everywhere
# else. Read once at import; in-process overrides patch
# navindex.STAT_TTL_S directly (test_autorescan's precedent).
STAT_TTL_S = float(os.environ.get("NEURONAV_STAT_TTL_S") or 3.0)

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
        navconfig.CONFIG_PATH is None
        and not navstore._fake_embeds()
        and Path.cwd() == navconfig.ROOT
        and (navconfig.ROOT / ".git").is_dir()
    ):
        print(
            f"neuronav: walk scope {n:,} files under bare defaults — pass "
            "NEURONAV_CONFIG or extend exclude_dirs; continuing",
            file=sys.stderr,
        )


# one-shot stderr note when gitignore-sourced prunes hide a large subtree
# (issue #296-B): keeps the #290/#286 oversized-walk guard honest — the
# totals it counts may already be gitignore-pruned. Hermetic (FAKE) runs
# stay silent like the #286 hint.
GITIGNORE_PRUNE_WARN_N = 10_000
_gitignore_counted: set[str] = set()


def _gitignore_prune_tick(pruned: list[str]) -> None:
    # issue #296: surface large gitignore-sourced prunes once per dir name
    # per process — a fresh consumer whose whole build tree vanished from
    # the index deserves a breadcrumb, not a silent 0-file walk. Mirrors
    # the #286 hint: hermetic FAKE runs and non-repo cwd stay silent.
    if (navstore._fake_embeds()
            or navconfig.CONFIG_PATH is not None
            and not (navconfig.ROOT / ".git").is_dir()):
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
    total = sum(_count_pruned_files(navconfig.ROOT / dn) for dn in todo)
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

# ---- shared walk filter leaf (issue #375) ------------------------------------
# The three walk engines below (iter_files, iter_root_files, stat_fingerprint)
# were kept equivalent only by comments — the #296-C drift class, which
# already bit once (a fix applied to one engine missed the others). The
# per-directory prune and the suffix predicate live HERE, once; each
# traversal keeps its own shape and yield contract (include-walk /
# root-wide / scandir stack).


def _kept_dirs(names: Iterable[str], prune: Set[str]) -> list[str]:
    """Sorted dirnames surviving the prune set — the per-directory filter
    every walk engine routes through (sorted keeps each walk deterministic)."""
    return sorted(dn for dn in names if dn not in prune)


def _suffix_in(name: str, suffixes: Set[str]) -> bool:
    """The suffix predicate every walk engine's file filter routes through."""
    return Path(name).suffix in suffixes


def iter_files(all_suffixes: bool = False) -> Iterator[Path]:
    # os.walk (not rglob) so exclude_dirs are pruned from the traversal —
    # a repo-root include_dir would otherwise walk .venv/.chroma/etc.
    # Overlapping include_dirs ([".", "tests"]) dedupe on the index key
    # (issue #117): each file yields exactly once — first include wins,
    # order stays the per-dir sorted walk — so a rescan counts it once
    # instead of double-embedding both copies into one upsert batch.
    seen: set[str] = set()
    n = 0
    for d in navconfig.INCLUDE_DIRS:
        base = navconfig.ROOT / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            if navconfig.GITIGNORE_PRUNE_DIRS:
                _gitignore_prune_tick(
                    [dn for dn in dirnames if dn in navconfig.GITIGNORE_PRUNE_DIRS]
                )
            dirnames[:] = _kept_dirs(dirnames, navconfig.EXCLUDE_DIRS)
            for name in sorted(filenames):
                # all_suffixes (issue #240): same walk rules with the
                # extension filter off — the degraded-boot suffix census
                if all_suffixes or _suffix_in(name, navconfig.EXTS):
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
_PRUNE_FLOOR = frozenset(navconfig.WALK_DEFAULTS["exclude_dirs"])


def iter_root_files(suffixes: set[str] | frozenset[str]) -> Iterator[Path]:
    """Root-wide pruned walk (sorted, deterministic) for wiring passes —
    the .tres scan's rglob replacement (issue #117): honors the SAME
    exclude contract as iter_files (config exclude_dirs + .neuroignore)
    plus the standard cache prune floor, so .venv/node_modules/.tmp/
    .neuronav are pruned from the traversal instead of read and filtered
    afterwards."""
    prune = navconfig.EXCLUDE_DIRS | _PRUNE_FLOOR
    for dirpath, dirnames, filenames in os.walk(navconfig.ROOT):
        dirnames[:] = _kept_dirs(dirnames, prune)
        for name in sorted(filenames):
            if _suffix_in(name, suffixes):
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
    root_len = len(str(navconfig.ROOT)) + 1
    stack = [navconfig.ROOT / d for d in navconfig.INCLUDE_DIRS]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                subdirs: list[str] = []
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            subdirs.append(e.name)
                            continue
                        if not _suffix_in(e.name, navconfig.EXTS):
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
                # issue #375: the same per-directory filter as the other two
                # engines (one truth); collecting first keeps the #374
                # per-entry OSError legs intact
                stack.extend(
                    os.path.join(d, dn)
                    for dn in _kept_dirs(subdirs, navconfig.EXCLUDE_DIRS)
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
    return path.relative_to(navconfig.ROOT).as_posix()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def rescan(timeout: float | None = None) -> dict[str, int]:
    """Incremental index: add/update changed files, purge deleted ones.
    Warm passes skip read+hash via the stat fingerprint (issue #42); the
    sha stays the content identity, and a mode-mismatched store re-embeds
    everything regardless of shas (issue #220). ``timeout`` bounds the
    cross-process store-lock wait (issue #203): exceeded, the rescan
    aborts loudly naming the lock and the likely holder instead of
    queueing forever."""
    navstore._memo_drop_current()  # embeddings changed — recompute on demand
    lock = navstore._db_lock(timeout)
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
    import graph as _graph_mod  # lazy: graph imports the nav leaves (#229 file-doc shaper)

    files = list(iter_files())
    if not files:
        # issue #41: zero files means the config matches nothing (typo'd
        # root/include_dirs/extensions) — proceeding would report a silent
        # zero-file success and purge the previous walk's entries
        raise RuntimeError(
            f"rescan found 0 files under root={navconfig.ROOT} "
            f"include_dirs={list(navconfig.INCLUDE_DIRS)} "
            f"extensions={sorted(navconfig.EXTS)} — "
            "fix the config or unset NEURONAV_CONFIG (deliberate wipe: "
            "python nav.py drop)"
        )
    col = navstore._collection()
    # issue #220: the stamp carries the embed mode; a store built in the
    # other mode must re-embed even when file shas are unchanged — an
    # absent key is pre-#220 real lineage, never a mismatch. Loud, never
    # silent: quietly reusing the wrong vector space is the #219 failure.
    # issue #229 extends the same law to the doc-construction shape: the
    # shaper rewrites docs for unchanged bytes, so sha-gating alone would
    # keep serving vectors built from the other shape — an absent key is
    # pre-#229 raw lineage.
    stamped_mode = (col.metadata or {}).get("embed_mode", "real")
    mode = navstore.embed_mode()
    shape = navstore.doc_shape()
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
        got = navstore.col_get_all(col, ["metadatas"], "rescan existing-rows read")
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
        vectors = (navstore.embed([navconfig.EMBED_DOC_PREFIX + d for d in pending_docs])
                   if navconfig.EMBED_DOC_PREFIX else navstore.embed(pending_docs))
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
        try:
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
        except OSError as e:
            # issue #374: the file vanished between the listing and its
            # read (editor atomic-save, checkout switch) — the same race
            # the parse pass guards (#117). Skip with a note and drop the
            # id from `seen` so the purge leg reconciles it this pass
            # (deleted_paths carries it; the summary stays truthful)
            # instead of one vanished temp file aborting the rescan.
            print(f"neuronav: rescan skipped {fid}: {e}", file=sys.stderr)
            seen.discard(fid)
            continue
        pending_ids.append(fid)
        # issue #229: the embed doc is the cAST-shaped file doc, not the
        # raw text (raw only under the shape fallbacks). The stored
        # document equals the embed input, so the bench's #220 store
        # coherence check (re-embed stored docs) stays truthful.
        pending_docs.append(
            _graph_mod.file_doc(path, fid, text, navconfig.FILE_DOC_CAST))
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
        if len(pending_ids) >= navstore.UPSERT_BATCH:
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
        col = navstore._restamp(col, embed_mode=mode, doc_shape=shape)
    stats["changed"] = changed_paths
    stats["deleted_paths"] = deleted
    return stats


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
    with navstore._db_lock():
        col = navstore._collection()
        if col.count() == 0:
            raise RuntimeError("nothing indexed — run rescan first")
        got = navstore.col_get_all(
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
        staging = navconfig.BASE_DIR.parent / "base.tmp-export"
        prev = navconfig.BASE_DIR.parent / "base.prev-export"
        # self-heal leftovers from a hard-killed run: staging is always
        # garbage; a surviving prev with no live base means the kill
        # landed between the two commit renames — prev IS the last
        # committed generation, so restore it
        if staging.exists():
            shutil.rmtree(staging)
        if prev.exists():
            if (navconfig.BASE_DIR / MANIFEST_NAME).is_file():
                shutil.rmtree(prev)
            else:
                os.replace(prev, navconfig.BASE_DIR)
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
            "model": navconfig.EMBED_MODEL,
            "dim": navconfig.EMBED_DIM,
            "provider": navconfig.EMBED_PROVIDER,
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
            if navconfig.BASE_DIR.is_dir():
                os.replace(navconfig.BASE_DIR, prev)
                moved_live = True
            os.replace(staging, navconfig.BASE_DIR)
        except BaseException:
            if moved_live and not navconfig.BASE_DIR.is_dir():
                os.replace(prev, navconfig.BASE_DIR)
            raise
        # old generation is no longer live — removal is best-effort;
        # debris never affects reads and the self-heal reaps it
        if prev.is_dir():
            shutil.rmtree(prev, ignore_errors=True)
        return manifest


def import_base() -> dict[str, int | str]:
    """Seed local chroma from tracked shards. Skips ids whose files no
    longer exist (deleted/renamed since export) — rescan heals the rest."""
    navstore._memo_drop_current()  # store repopulated — recompute on demand
    with navstore._db_lock():
        col = navstore._collection()
        if col.count():
            return {"skipped": col.count()}
        manifest_path = navconfig.BASE_DIR / MANIFEST_NAME
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
            manifest.get("model") != navconfig.EMBED_MODEL
            or (dim is not None and dim != navconfig.EMBED_DIM)
            or (m_prov is not None and m_prov != navconfig.EMBED_PROVIDER)
        ):
            raise RuntimeError(
                f"base index model mismatch: {manifest.get('model')}/{dim} "
                f"(provider {manifest.get('provider')!r}) vs config "
                f"{navconfig.EMBED_MODEL}/{navconfig.EMBED_DIM} (provider "
                f"'{navconfig.EMBED_PROVIDER}') — run "
                "`python nav.py drop` then rescan"
            )
        ids: list[str] = []
        embs: list[list[float]] = []
        metas: list[dict[str, str]] = []
        for shard in sorted(navconfig.BASE_DIR.glob("shard-*.jsonl.gz")):
            with gzip.open(shard, "rt", encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    if not (navconfig.ROOT / row["id"]).is_file():
                        continue
                    ids.append(row["id"])
                    embs.append(row["emb"])
                    metas.append(row["meta"])
        for i in range(0, len(ids), navstore.UPSERT_BATCH):
            col.upsert(
                ids=ids[i : i + navstore.UPSERT_BATCH],
                embeddings=embs[i : i + navstore.UPSERT_BATCH],
                metadatas=metas[i : i + navstore.UPSERT_BATCH],
            )
        return {"imported": len(ids), "manifest_count": int(manifest.get("count", 0)),
                "exported_at": str(manifest.get("exported_at", ""))}
