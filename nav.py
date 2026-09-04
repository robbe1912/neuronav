"""swmg-nav core: whole-file semantic index of this checkout's GDScript/Godot scenes.

Per-checkout index: `.swmg-nav/.chroma` (gitignored). Base index shards
(`.swmg-nav/base/`) are tracked and give fresh clones a fast start; the
incremental rescan then heals the index to the current HEAD.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import chromadb
import httpx

TOOL_DIR = Path(__file__).resolve().parent

_cfg_path = TOOL_DIR / "config.json"
_cfg: dict = json.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.is_file() else {}

ROOT = Path(_cfg.get("root") or TOOL_DIR.parent)
DB_DIR = TOOL_DIR / ".chroma"
BASE_DIR = TOOL_DIR / "base"
COLLECTION = str(_cfg.get("collection", "swmg"))

INCLUDE_DIRS = tuple(_cfg.get("include_dirs", ("scripts", "scenes", "VFX", "ai", "tests", "tools")))
EXTS = {".gd", ".tscn"}

EMBED_URL = str(_cfg.get("embed_url", "http://127.0.0.1:11434/api/embed"))
EMBED_MODEL = str(_cfg.get("embed_model", "qwen3-embedding:0.6b"))
EMBED_DIM = int(_cfg.get("embed_dim", 1024))
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
    """Batch-embed via Ollama /api/embed. Truncates long inputs."""
    truncated = [t[:MAX_EMBED_CHARS] for t in texts]
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
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in EXTS:
                yield p


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


def _collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(DB_DIR))
    return client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def rescan() -> dict[str, int]:
    """Incremental index: add/update changed files, purge deleted ones."""
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


def clusters(k: int = 6, min_sim: float = 0.6) -> list[dict[str, object]]:
    """Subsystem clusters: union-find over MUTUAL kNN embedding neighbours
    (i and j are neighbours of each other, cosine >= min_sim). Mutual links
    resist transitive chaining, so components stay subsystem-sized.
    Returns [{id, size, paths: [(path, class_name)]}]."""
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

    # cluster scripts and scenes separately: tscn headers dominate embeddings,
    # mixing them chains unrelated files; scene↔script affinity is structural
    gd_idx = [i for i, m in enumerate(metas) if (m or {}).get("ext") == ".gd"]
    tscn_idx = [i for i, m in enumerate(metas) if (m or {}).get("ext") == ".tscn"]
    for subset in (gd_idx, tscn_idx):
        sset = set(subset)
        for i in subset:
            for j in knn[i]:
                j = int(j)
                if j in sset and i in knn[j] and sim[i, j] >= min_sim:
                    union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(ids)):
        groups.setdefault(find(i), []).append(i)
    out: list[dict[str, object]] = []
    for members in groups.values():
        items = sorted(
            (
                ids[m],
                str((metas[m] or {}).get("class_name", "")),
            )
            for m in members
        )
        out.append({"id": len(out), "size": len(items), "paths": items})
    out.sort(key=lambda c: -int(c["size"]))
    for idx, c in enumerate(out):
        c["id"] = idx
    return out


# ---- base index (tracked shards) -------------------------------------------


def export_base() -> dict[str, object]:
    """Dump ids+embeddings+metadata to tracked gz shards. No doc text
    (git has the file contents; import re-attaches from the checkout)."""
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
    for old in BASE_DIR.glob("shard-*.jsonl.gz"):
        old.unlink()
    shards = 0
    for i in range(0, len(rows), SHARD_SIZE):
        chunk = rows[i : i + SHARD_SIZE]
        shard = BASE_DIR / f"shard-{shards:04d}.jsonl.gz"
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
    (BASE_DIR / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def import_base() -> dict[str, int | str]:
    """Seed local chroma from tracked shards. Skips ids whose files no
    longer exist (deleted/renamed since export) — rescan heals the rest."""
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rescan"
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
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)
