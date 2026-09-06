"""nav core: whole-file semantic index of a checkout (GDScript/scenes,
Python — whatever the config's "extensions" list enables).

Config resolution: $NEURONAV_CONFIG env var, else ``config.json`` next to
this file. A second config (e.g. ``config/neuronav.json`` for self-indexing)
switches root/include_dirs/extensions/collection without touching the
primary one. Per-checkout index: ``.chroma`` (gitignored). Base index
shards (``base/``) are tracked and give fresh clones a fast start; the
incremental rescan then heals the index to the current HEAD.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import shutil
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import chromadb
from filelock import FileLock
import httpx

TOOL_DIR = Path(__file__).resolve().parent


def _apply_config(path: Path) -> None:
    """(Re)bind the config-derived module globals. Called once at import
    and again by ``nav.py --config <path>`` (which also sets NEURONAV_CONFIG
    so subprocesses and sibling modules like graph.py agree)."""
    global ROOT, COLLECTION, INCLUDE_DIRS, EXTS, EXCLUDE_DIRS, EMBED_URL, EMBED_MODEL, EMBED_DIM
    cfg: dict = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    ROOT = Path(cfg.get("root") or TOOL_DIR.parent)
    if not ROOT.is_absolute():
        # relative roots resolve against the config file's own directory,
        # so shipped profiles (config/neuronav.json) stay machine-portable
        ROOT = (path.parent / ROOT).resolve()
    COLLECTION = str(cfg.get("collection", "swmg"))
    INCLUDE_DIRS = tuple(cfg.get("include_dirs", ("scripts", "scenes", "VFX", "ai", "tests", "tools")))
    EXTS = set(cfg.get("extensions", (".gd", ".tscn")))
    EXCLUDE_DIRS = frozenset(cfg.get("exclude_dirs", (".git", "__pycache__")))
    EMBED_URL = str(cfg.get("embed_url", "http://127.0.0.1:11434/api/embed"))
    EMBED_MODEL = str(cfg.get("embed_model", "qwen3-embedding:0.6b"))
    EMBED_DIM = int(cfg.get("embed_dim", 1024))


ROOT: Path
COLLECTION: str
INCLUDE_DIRS: tuple[str, ...]
EXTS: set[str]
EXCLUDE_DIRS: frozenset[str]
EMBED_URL: str
EMBED_MODEL: str
EMBED_DIM: int
_apply_config(Path(os.environ.get("NEURONAV_CONFIG") or TOOL_DIR / "config.json"))

DB_DIR = TOOL_DIR / ".chroma"
BASE_DIR = TOOL_DIR / "base"

MAX_EMBED_CHARS = 30_000  # keep under Ollama context; head of .tscn has script links
EMBED_BATCH = 32
UPSERT_BATCH = 64
SHARD_SIZE = 250
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class Hit:
    path: str
    score: float
    class_name: str
    extends: str
    ext: str


def embed(texts: list[str]) -> list[list[float]]:
    """Batch-embed via Ollama /api/embed. Truncates long inputs.

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
    resp = httpx.post(
        EMBED_URL,
        json={"model": EMBED_MODEL, "input": truncated},
        timeout=300.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if "embeddings" not in data:
        raise RuntimeError(f"Ollama embed failed: {data}")
    out: list[list[float]] = data["embeddings"]
    if len(out) != len(texts):
        raise RuntimeError(
            f"Ollama returned {len(out)} embeddings for {len(texts)} inputs"
        )
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
    nav functions). Readers skip it; sqlite handles the rest."""
    global _LOCK
    if _LOCK is None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        _LOCK = FileLock(str(DB_DIR / ".write.lock"))
    return _LOCK


_LOCK: FileLock | None = None


def _check_model(col: chromadb.Collection) -> None:
    """Embedding-model fingerprint on the live collection: a same-dim
    different-model swap silently mixes vector spaces otherwise."""
    meta = col.metadata or {}
    stored = meta.get("embed_model")
    if stored is None:
        try:
            col.modify(metadata={"embed_model": EMBED_MODEL})
        except Exception:
            pass  # chroma refusing metadata modify is non-fatal
    elif stored != EMBED_MODEL:
        raise RuntimeError(
            f"index was built with embed model '{stored}' but config says "
            f"'{EMBED_MODEL}' — run `python nav.py drop` then rescan"
        )


def _collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(DB_DIR))
    col = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    _check_model(col)
    return col


def rescan() -> dict[str, int]:
    """Incremental index: add/update changed files, purge deleted ones."""
    with _db_lock():
        return _rescan_locked()


def _rescan_locked() -> dict[str, int]:
    col = _collection()
    existing: dict[str, str] = {}
    if col.count():
        got = col.get(include=["metadatas"])
        existing = {
            rid: (meta or {}).get("sha", "")
            for rid, meta in zip(got["ids"], got["metadatas"])
        }

    seen: set[str] = set()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    changed_paths: list[str] = []
    pending_ids: list[str] = []
    pending_docs: list[str] = []
    pending_meta: list[dict[str, str]] = []

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

    for path in iter_files():
        fid = file_id(path)
        seen.add(fid)
        digest = sha256_of(path)
        if existing.get(fid) == digest:
            stats["unchanged"] += 1
            continue
        text = _read_text(path)
        pending_ids.append(fid)
        pending_docs.append(text)
        pending_meta.append(
            {
                "sha": digest,
                "ext": path.suffix,
                "class_name": _class_name(text),
                "extends": _extends(text),
            }
        )
        stats["added" if fid not in existing else "updated"] += 1
        changed_paths.append(fid)
        if len(pending_ids) >= UPSERT_BATCH:
            flush()
    flush()

    deleted = [fid for fid in existing if fid not in seen]
    if deleted:
        col.delete(ids=deleted)
        stats["deleted"] = len(deleted)
    stats["changed"] = changed_paths
    stats["deleted_paths"] = deleted
    return stats


def search(query: str, n: int = 8) -> list[Hit]:
    col = _collection()
    count = col.count()
    if count == 0:
        return []
    vector = embed([query])[0]
    got = col.query(
        query_embeddings=[vector],
        n_results=min(n, count),
        include=["metadatas", "distances"],
    )
    hits: list[Hit] = []
    for fid, dist, meta in zip(
        got["ids"][0], got["distances"][0], got["metadatas"][0]
    ):
        meta = meta or {}
        hits.append(
            Hit(
                path=fid,
                score=round(1.0 - float(dist), 4),
                class_name=str(meta.get("class_name", "")),
                extends=str(meta.get("extends", "")),
                ext=str(meta.get("ext", "")),
            )
        )
    return hits


def count() -> int:
    return _collection().count()


def clusters(
    k: int = 6,
    min_sim: float = 0.6,
    split_sim: float = 0.65,
    blob_min: int = 60,
    cross_sim: float = 0.75,
    resolution: float = 1.0,
) -> list[dict[str, object]]:
    """Subsystem clusters. Default engine (resolution not None): Louvain
    community detection over a hybrid weighted graph — mutual-kNN
    embedding sims (weight = sim * 0.7) + structural edges from graph.py
    (call/signal capped 5 per file pair, attach/inst 1.5); tests/ files
    get their own community, loose files join the community dominating
    their dir seed (see clusters.communities_graph). Resolution 1.5
    chosen by sweep ({1.0: 49 clusters/largest 131, 1.2: 35/74, 1.5:
    29/69, 1.8: 30/70}). Legacy engine (resolution=None): dir-seeded
    mutual-kNN union-find (full-dir-chain seeds, blob-scale seed groups
    need cross_sim). Either way mega-blobs are then split + every cluster
    labeled (see clusters.finalize).
    Returns [{id, size, paths: [(path, class_name)], label, confidence,
    method}]."""
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
    knn = np.argsort(-sim, axis=1)[:, :k]

    import clusters as _clusters

    out: list[dict[str, object]] = []
    adj = None  # structural adjacency from the louvain engine (hub gating)
    units = None  # welded scene+script units (survive finalize splits)
    if resolution is None:
        # legacy engine: dir-seeded mutual-kNN union-find
        parent = list(range(len(ids)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        from collections import Counter

        seed = [_clusters.dir_seed(p) for p in ids]
        seed_n = Counter(s for s in seed if s)

        # cluster scripts and scenes separately: tscn headers dominate
        # embeddings, mixing them chains unrelated files
        gd_idx = [i for i, m in enumerate(metas) if (m or {}).get("ext") == ".gd"]
        tscn_idx = [i for i, m in enumerate(metas) if (m or {}).get("ext") == ".tscn"]
        for subset in (gd_idx, tscn_idx):
            sset = set(subset)
            for i in subset:
                for j in knn[i]:
                    j = int(j)
                    if j in sset and i in knn[j] and sim[i, j] >= min_sim:
                        si, sj = seed[i], seed[j]
                        # same-seed unions at min_sim only while the seed
                        # group is small; blob-scale flat folders need the
                        # high bar (Qwen3-0.6B over-merge chaining)
                        if si is None or sj is None:
                            union(i, j)
                        elif si == sj:
                            if sim[i, j] >= cross_sim or seed_n[si] < _clusters.BIG_SEED_MAX:
                                union(i, j)
                        elif sim[i, j] >= cross_sim:
                            union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(len(ids)):
            groups.setdefault(find(i), []).append(i)
        for members in groups.values():
            items = sorted(
                (
                    ids[m],
                    str((metas[m] or {}).get("class_name", "")),
                )
                for m in members
            )
            out.append({"id": len(out), "size": len(items), "paths": items})
    else:
        # louvain hybrid: structural edges + embedding sims; adj feeds the
        # labeler's autoload hub gating, units keep scene+script welds
        # intact through finalize's embedding split passes
        out, adj, units = _clusters.communities_graph(
            ids, metas, mat, sim, knn, min_sim=min_sim, resolution=resolution
        )
    out.sort(key=lambda c: -int(c["size"]))
    for idx, c in enumerate(out):
        c["id"] = idx

    return _clusters.finalize(
        out, ids, mat, split_sim=split_sim, blob_min=blob_min, adj=adj, units=units
    )


# ---- base index (tracked shards) -------------------------------------------


def export_base() -> dict[str, object]:
    """Dump ids+embeddings+metadata to tracked gz shards. No doc text
    (git has the file contents; import re-attaches from the checkout).
    Atomic: new shards + manifest land in a tmp dir and are swapped in
    only after every write succeeded, so an interrupted export never
    destroys the previous base."""
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
            with gzip.open(shard, "wt", encoding="utf-8", compresslevel=9) as f:
                for rid, emb, meta in chunk:
                    f.write(
                        json.dumps(
                            {"id": rid, "emb": emb, "meta": meta},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            shards += 1
        manifest = {
            "model": EMBED_MODEL,
            "dim": EMBED_DIM,
            "count": len(rows),
            "shards": shards,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (tmp / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        for old in BASE_DIR.glob("shard-*.jsonl.gz"):
            old.unlink()
        for f in tmp.iterdir():
            f.rename(BASE_DIR / f.name)
        tmp.rmdir()
        return manifest


def import_base() -> dict[str, int | str]:
    """Seed local chroma from tracked shards. Skips ids whose files no
    longer exist (deleted/renamed since export) — rescan heals the rest."""
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
                f"base index model mismatch: {manifest.get('model')}/{manifest.get('dim')}"
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
        os.environ["NEURONAV_CONFIG"] = str(cfg_file)
        _apply_config(cfg_file)
        argv = argv[2:]
    cmd = argv[0] if argv else "rescan"
    if cmd == "rescan":
        t0 = time.perf_counter()
        s = rescan()
        dt = time.perf_counter() - t0
        print(f"{s} in {dt:.1f}s, total={count()}")
    elif cmd == "search":
        for h in search(" ".join(sys.argv[2:]), n=8):
            print(f"{h.score:0.3f}  {h.path}  class={h.class_name} extends={h.extends}")
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
        print(
            f"crosstalk: {rep['clusters']} clusters, "
            f"internal {rep['internal_edges']} edges, "
            f"cross-cluster {rep['external_edges']} edges "
            f"({rep['external_ratio'] * 100:.1f}% of clustered)"
        )
        if rep["unclustered_endpoint_edges"]:
            print(f"  ({rep['unclustered_endpoint_edges']} edges touch unclustered files)")
        print("per cluster (top 10 by external):")
        for r in rep["by_cluster"][:10]:
            print(
                f"  [{r['id']:>2}] {r['label'][:34]:<34} n={r['size']:<3}"
                f" internal {r['internal']:<4} out {r['external_out']:<4}"
                f" in {r['external_in']:<4} ext {r['external_share'] * 100:.0f}%"
            )
        if rep["worst_pairs"]:
            print("worst pairs:")
            for wp in rep["worst_pairs"]:
                tops = ", ".join(f"{t['pair']} x{t['w']}" for t in wp["top_files"])
                print(f"  {wp['a']} <-> {wp['b']} : {wp['edges']} edges (top: {tops})")
    elif cmd == "drop":
        import chromadb as _c
        client = _c.PersistentClient(path=str(DB_DIR))
        for name in (COLLECTION, f"{COLLECTION}-fns"):
            try:
                client.delete_collection(name)
                print(f"dropped {name}")
            except Exception:
                print(f"{name}: not present")
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)
