# base-index export/import shards (issue #102) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_baseindex.py
#
# Hermetic: generated temp target tree + config under the system temp dir
# (never the real index), NEURONAV_EMBED_FAKE=1 (deterministic hash
# embeddings, no Ollama). Pins the issue #102 contract:
#   (a) second-run idempotence: exporting over an existing base succeeds
#       on Windows (Path.rename onto an existing manifest raised WinError
#       183 after the old shards were already unlinked) and the on-disk
#       manifest is the new run's — nothing stranded in base/tmp
#   (b) non-destruction: a failure during the swap leaves the previous
#       base byte-identical (unlink-first used to destroy it first)
#   (c) byte determinism: re-exporting the same store rewrites identical
#       shard bytes (gzip header mtime pinned to 0)
#   (d) stale-shard cleanup: a smaller re-export drops shards the new run
#       no longer carries — the disk shard set matches manifest["shards"]
#   (e) roundtrip: shards + manifest copied into a fresh store import
#       every id; a non-empty store skips instead of double-seeding
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

TMP = Path(tempfile.mkdtemp(prefix="neuronav_baseindex_"))
(TMP / "src").mkdir(parents=True)


def write_tree(n: int) -> None:
    for i in range(n):
        (TMP / "src" / f"mod{i:04d}_thing.py").write_text(
            f"def mod{i:04d}_thing_run(scale):\n    return scale * {i}\n",
            encoding="utf-8",
        )


write_tree(4)

CFG = Path(tempfile.gettempdir()) / "neuronav_baseindex_config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "baseindex",
            "include_dirs": ["src"],
            "extensions": [".py"],
            "state_dir": str(TMP / "state"),
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(HERE))

import nav  # noqa: E402  (binds the temp config above)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def base_files() -> dict[str, bytes]:
    return {
        p.name: p.read_bytes()
        for p in sorted(nav.BASE_DIR.iterdir())
        if p.is_file()
    }


def disk_shards() -> list[str]:
    return sorted(p.name for p in nav.BASE_DIR.glob("shard-*.jsonl.gz"))


def try_export() -> tuple[dict, str]:
    """export_base(), with the issue #102 crash captured as a failure
    detail instead of a suite-killing traceback."""
    try:
        return nav.export_base(), ""
    except OSError as e:
        return {}, f"{type(e).__name__}: {e}"


# ---- (a) second-run idempotence ---------------------------------------------
nav.rescan()
m1 = nav.export_base()
check("first export shape", m1["count"] == 4 and m1["shards"] == 1, str(m1))
bytes1 = base_files()

m2, err2 = try_export()
check("second export succeeds (issue #102)", not err2, err2)

if not err2:
    disk_man = json.loads(
        (nav.BASE_DIR / nav.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    check("on-disk manifest is the second run's", disk_man == m2, str(disk_man))
    stranded = (
        sorted(p.name for p in (nav.BASE_DIR / "tmp").iterdir())
        if (nav.BASE_DIR / "tmp").is_dir()
        else []
    )
    check("no files stranded in base/tmp", not stranded, str(stranded))

    # ---- (c) byte determinism: same store, two exports, same bytes -----
    check(
        "re-export rewrites byte-identical shards",
        base_files()["shard-0000.jsonl.gz"] == bytes1["shard-0000.jsonl.gz"],
    )

# ---- (b) non-destruction on a mid-swap failure -------------------------------
before = base_files()
_orig_replace, _orig_rename = os.replace, Path.rename


def _replace_boom(src, dst, *a, **k):
    if nav.BASE_DIR == Path(dst) or nav.BASE_DIR in Path(dst).parents:
        raise OSError(13, "simulated mid-swap failure (lock/permission)")
    return _orig_replace(src, dst, *a, **k)


def _rename_boom(self, target):
    if nav.BASE_DIR == Path(target) or nav.BASE_DIR in Path(target).parents:
        raise OSError(13, "simulated mid-swap failure (lock/permission)")
    return _orig_rename(self, target)


# patch both move primitives: whichever the swap uses (Path.rename
# pre-fix, os.replace post-fix), the failure lands mid-swap
os.replace = _replace_boom
Path.rename = _rename_boom
try:
    nav.export_base()
    raised = False
except OSError:
    raised = True
finally:
    os.replace = _orig_replace
    Path.rename = _orig_rename
check("mid-swap failure raises", raised)
check("base carried shards entering the failure test",
      any(n.startswith("shard-") for n in before), str(sorted(before)))
check("previous base byte-identical after failed run", base_files() == before,
      f"before={sorted(before)} after={sorted(base_files())}")

# ---- (d) stale-shard cleanup on a shrinking re-export ------------------------
write_tree(4 + nav.SHARD_SIZE + 6)  # 2 full shards + remainder
nav.rescan()
m3, err3 = try_export()
check("grown export spans multiple shards",
      not err3 and m3.get("shards") == 2 and len(disk_shards()) == 2,
      err3 or f"{m3['shards']} shards, disk={disk_shards()}")

for i in range(4, 4 + nav.SHARD_SIZE + 6):
    (TMP / "src" / f"mod{i:04d}_thing.py").unlink()
shrink = nav.rescan()
m4, err4 = try_export()
check("shrink rescan purged the grown files",
      shrink["deleted"] == nav.SHARD_SIZE + 6 and nav.count() == 4,
      f"deleted={shrink['deleted']} count={nav.count()}")
check("stale shard removed, disk set matches manifest",
      not err4 and disk_shards() == ["shard-0000.jsonl.gz"] and m4.get("shards") == 1,
      err4 or f"disk={disk_shards()} manifest_shards={m4['shards']}")

# ---- (e) roundtrip: shards into a fresh store --------------------------------
STATE2 = TMP / "state2"
CFG2 = Path(tempfile.gettempdir()) / "neuronav_baseindex_config2.json"
CFG2.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "baseindex",
            "include_dirs": ["src"],
            "extensions": [".py"],
            "state_dir": str(STATE2),
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(CFG2)
nav._apply_config(CFG2)
shutil.copytree(TMP / "state" / "base", nav.BASE_DIR)

r = nav.import_base()
check("fresh store imports every id",
      r.get("imported") == 4 and r.get("manifest_count") == 4, str(r))
check("imported count matches the store", nav.count() == 4, str(nav.count()))
r2 = nav.import_base()
check("non-empty store skips re-import", r2 == {"skipped": 4}, str(r2))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
