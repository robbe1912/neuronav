# base-index export/import shards (issue #102) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_baseindex.py
#
# Hermetic: generated temp target tree + config under the suite's own
# mkdtemp dir (never the real index), NEURONAV_EMBED_FAKE=1
# (deterministic hash embeddings, no Ollama). Pins the issue #102
# contract plus the CodeRabbit follow-up on PR #152:
#   (a) second-run idempotence: exporting over an existing base
#       succeeds on Windows and the on-disk manifest is the new run's
#   (b) failed-run non-destruction, per failure phase:
#       - mid-write: live base untouched, debris staged OUTSIDE it
#       - commit-phase interruption: live base byte-identical (rolled
#         back), never a mixed generation
#       - cleanup failure: export still commits, debris self-heals on
#         the next run
#       - retry after any failure heals to the new generation
#   (c) byte determinism: re-exporting the same store rewrites
#       identical shard bytes (gzip header mtime pinned to 0)
#   (d) stale-shard cleanup: a smaller re-export drops shards the new
#       run no longer carries — the disk shard set matches
#       manifest["shards"]
#   (e) roundtrip: shards + manifest copied into a fresh store import
#       every id; a non-empty store skips instead of double-seeding
import gzip
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

# both configs live under the suite's unique TMP root — concurrent suite
# processes can never overwrite each other's config (CodeRabbit, #152)
CFG = TMP / "config.json"
CFG2 = TMP / "config2.json"
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


def disk_manifest() -> dict:
    return json.loads(
        (nav.BASE_DIR / nav.MANIFEST_NAME).read_text(encoding="utf-8")
    )


def try_export() -> tuple[dict, str]:
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
    check("on-disk manifest is the second run's", disk_manifest() == m2)

    # ---- (c) byte determinism: same store, two exports, same bytes -----
    check(
        "re-export rewrites byte-identical shards",
        base_files()["shard-0000.jsonl.gz"] == bytes1["shard-0000.jsonl.gz"],
    )

# ---- (b) failed-run non-destruction ------------------------------------------
# mutate the indexed data first so the attempted export carries a REAL
# new generation — a partial-swap bug would then change base bytes
# (CodeRabbit: a no-op export can pass a corruption check vacuously)
(TMP / "src" / "extra_thing.py").write_text(
    "def extra_thing_run(scale):\n    return scale * 99\n", encoding="utf-8"
)
nav.rescan()
before = base_files()
check("base carried shards entering the failure legs",
      any(n.startswith("shard-") for n in before), str(sorted(before)))

# leg 1 — die mid-staging-write: live base must be untouched and the
# debris must live OUTSIDE it (the old design staged inside base/tmp)
_writes = {"n": 0}
_gz_write = gzip.GzipFile.write


def _gz_write_boom(self, data):
    _writes["n"] += 1
    if _writes["n"] >= 3:
        raise OSError(28, "simulated mid-write failure (disk full)")
    return _gz_write(self, data)


gzip.GzipFile.write = _gz_write_boom
_, errA = try_export()
gzip.GzipFile.write = _gz_write
check("mid-write failure raises", bool(errA), errA)
check("live base untouched after mid-write failure", base_files() == before,
      f"before={sorted(before)} after={sorted(base_files())}")
check("mid-write debris staged outside the live base",
      "tmp" not in [p.name for p in nav.BASE_DIR.iterdir()],
      str([p.name for p in nav.BASE_DIR.iterdir()]))

# leg 2 — interrupt the commit: the live base must stay byte-identical
# (rolled back), never a mix of two generations. Fires on a per-file
# swap at the manifest AND on a directory swap at the final rename.
_os_replace = os.replace


def _replace_boom(src, dst, *a, **k):
    dst = Path(dst)
    if (
        dst == nav.BASE_DIR and Path(src).name != "base.prev-export"
    ) or dst.name == nav.MANIFEST_NAME:
        raise OSError(13, "simulated commit failure (lock/permission)")
    return _os_replace(src, dst, *a, **k)


os.replace = _replace_boom
_, errB = try_export()
os.replace = _os_replace
check("commit-phase interruption raises", bool(errB), errB)
check("live base byte-identical after commit-phase interruption",
      base_files() == before,
      f"before={sorted(before)} after={sorted(base_files())}")

# retry heals: a clean run after the failures lands the new generation
mR = nav.export_base()
check("retry after failures heals to the new generation",
      mR["count"] == 5 and disk_manifest() == mR and disk_shards() == ["shard-0000.jsonl.gz"],
      f"{mR} disk={disk_shards()}")

# leg 3 — cleanup failure after a committed swap: the export must still
# succeed (the old generation is no longer live) and the debris must
# self-heal on the next run
_rmtree = shutil.rmtree


def _rmtree_noop(path, *a, **k):
    # faithful simulation of rmtree(ignore_errors=True) hitting a locked
    # dir: deletes nothing, raises nothing, leaves the debris in place
    if str(path).endswith("base.prev-export"):
        return
    return _rmtree(path, *a, **k)


shutil.rmtree = _rmtree_noop
mC = nav.export_base()
shutil.rmtree = _rmtree
check("cleanup failure does not fail a committed export",
      mC["count"] == 5 and disk_manifest() == mC, str(mC))
check("prev-generation debris tolerated for the next run",
      (nav.BASE_DIR.parent / "base.prev-export").is_dir())
nav.export_base()
check("next run self-heals the leftover debris",
      not (nav.BASE_DIR.parent / "base.prev-export").exists())

# ---- (d) stale-shard cleanup on a shrinking re-export ------------------------
write_tree(5 + nav.SHARD_SIZE + 6)  # 2 full shards + remainder
nav.rescan()
m3 = nav.export_base()
check("grown export spans multiple shards",
      m3["shards"] == 2 and len(disk_shards()) == 2,
      f"{m3['shards']} shards, disk={disk_shards()}")

for i in range(4, 5 + nav.SHARD_SIZE + 6):
    (TMP / "src" / f"mod{i:04d}_thing.py").unlink()
shrink = nav.rescan()
m4 = nav.export_base()
check("shrink rescan purged the grown files",
      shrink["deleted"] == nav.SHARD_SIZE + 7 and nav.count() == 5,
      f"deleted={shrink['deleted']} count={nav.count()}")
check("stale shard removed, disk set matches manifest",
      disk_shards() == ["shard-0000.jsonl.gz"] and m4["shards"] == 1,
      f"disk={disk_shards()} manifest_shards={m4['shards']}")

# ---- (e) roundtrip: shards into a fresh store --------------------------------
CFG2.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "baseindex",
            "include_dirs": ["src"],
            "extensions": [".py"],
            "state_dir": str(TMP / "state2"),
        }
    ),
    encoding="utf-8",
)
nav._apply_config(CFG2)  # selects the second config directly
shutil.copytree(TMP / "state" / "base", nav.BASE_DIR)

r = nav.import_base()
check("fresh store imports every id",
      r.get("imported") == 5 and r.get("manifest_count") == 5, str(r))
check("imported count matches the store", nav.count() == 5, str(nav.count()))
r2 = nav.import_base()
check("non-empty store skips re-import", r2 == {"skipped": 5}, str(r2))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
