# test_chromabounds — issue #327: the full-collection chroma reads (rescan
# existing-rows, re-stamp, base export, bake embeddings, clusters) must be BOUNDED —
# one unfiltered col.get() binds every row at once and sqlite dies with
# InternalError "too many SQL variables" past the build's ceiling
# (~32766 on this venv's chromadb 1.5.9 / sqlite 3.49.1) — and
# deterministically ordered, so exports stay byte-stable. Teeth are sized
# to the real ceiling via direct chroma adds (33k rows, ~10s), not a
# ~1200-doc comfort fixture; a just-under control (32.7k) pins the
# bounded path against the same store shape.

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if "--nav-dir" in sys.argv:  # pre-fix evidence runs against a base checkout
    NAV_DIR = Path(sys.argv[sys.argv.index("--nav-dir") + 1]).resolve()
else:
    NAV_DIR = HERE.parent
sys.path.insert(0, str(NAV_DIR))
sys.path.insert(0, str(HERE))

TMP = Path(tempfile.mkdtemp(prefix="nn-chromabounds-"))
CFG = TMP / "config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "cba",
            "include_dirs": ["."],
            "extensions": [".py"],
            "state_dir": str(TMP / "state"),
            "embed_dim": 8,
        }
    ),
    encoding="utf-8",
)
os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ["NEURONAV_EMBED_FAKE"] = "1"

import chromadb
from chromadb.errors import InternalError
import navconfig, navindex, navstore

from harness import check, finish  # noqa: E402


def _child_env(cfg: Path) -> dict:
    env = dict(os.environ)
    env["NEURONAV_CONFIG"] = str(cfg)
    env["NEURONAV_EMBED_FAKE"] = "1"
    return env


def _rescan(cfg: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(NAV_DIR / "nav.py"), "rescan"],
        capture_output=True, text=True, env=_child_env(cfg), timeout=300,
    )


def _mkstore(name: str, rows: int, tree_files: int = 1,
             vary_embs: bool = False, reverse: bool = False) -> Path:
    """A per-leg scratch: tree with tree_files real .py files (the #41
    zero-files abort needs >= 1) plus `rows` direct chroma adds of tiny
    docs under their own config/state_dir. vary_embs stamps every row
    with a deterministic per-id embedding (identical rows tie everywhere
    and pin nothing); reverse inserts the same ids in reverse order —
    the #118 store-HISTORY axis."""
    d = TMP / name
    (d / "src").mkdir(parents=True)
    for i in range(tree_files):
        (d / "src" / f"live{i}.py").write_text(f"# live {i}\n", encoding="utf-8")
    cfg = d / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "root": str(d),
                "collection": f"cba_{name}",
                "include_dirs": ["src"],
                "extensions": [".py"],
                "state_dir": str(d / "state"),
                "embed_dim": 8,
            }
        ),
        encoding="utf-8",
    )
    os.environ["NEURONAV_CONFIG"] = str(cfg)
    navconfig.use_config(cfg)
    col = navstore._collection()

    def _emb(i: int) -> list[float]:
        if not vary_embs:
            return [0.01 * (j + 1) for j in range(navconfig.EMBED_DIM)]
        return [(((i + 1) * (j + 3)) % 97) / 97.0
                for j in range(navconfig.EMBED_DIM)]

    order = list(range(rows))
    if reverse:
        order.reverse()
    BATCH = 2000
    for start in range(0, rows, BATCH):
        chunk = order[start:start + BATCH]
        col.add(
            ids=[f"dead{i:06d}" for i in chunk],
            embeddings=[_emb(i) for i in chunk],
            documents=["x"] * len(chunk),
        )
    return cfg


def main() -> None:
    # ---- leg A: the bounded helper exists, matches an unfiltered get,
    # and returns id-sorted rows regardless of insertion order ---------
    have = hasattr(navstore, "col_get_all") and hasattr(navstore, "GET_CHUNK")
    check("navstore.col_get_all/GET_CHUNK exist (bounded reads, #327)", have,
          "" if have else "nav module lacks the #327 bounded-read helper")
    if have:
        _mkstore("small", 1300)   # 2 full chunks + 276: crosses the page
        col = navstore._collection()   # boundary both ways
        got = navstore.col_get_all(col, ["documents", "metadatas"], "leg A")
        raw = col.get(include=["documents", "metadatas"])
        raw_rows = sorted(zip(raw["ids"], raw["documents"]),
                          key=lambda r: r[0])
        check(
            "bounded read == unfiltered get (sorted), 1300 rows",
            got["ids"] == [r[0] for r in raw_rows]
            and got["documents"] == [r[1] for r in raw_rows]
            and got["metadatas"] == [None] * 1300
            and got["ids"] == sorted(got["ids"]),
            f"{len(got['ids'])} rows, first={got['ids'][:1]}",
        )

    # ---- leg B: the teeth — 33k rows (> the ~32766 ceiling) rescan
    # clean through the bounded read; pre-fix this dies with the
    # sqlite InternalError before the walk ------------------------------
    cfg_b = _mkstore("over", 33_000)
    r = _rescan(cfg_b)
    sql_vars = "too many SQL variables" in (r.stderr or "")
    check(
        "rescan green past the ceiling: 33k-row store, rc=0, no SQL-vars",
        r.returncode == 0 and not sql_vars,
        f"rc={r.returncode} stderr tail={((r.stderr or '')[-300:])!r}",
    )
    if r.returncode == 0:
        col = navstore._collection()
        check("post-rescan store holds just the walked file",
              col.count() == 1, f"count={col.count()}")

    # ---- leg C: just-under control (32.7k < ceiling) — same shape,
    # proves the bounded path handles the near-ceiling store too -------
    cfg_c = _mkstore("under", 32_700)
    r = _rescan(cfg_c)
    check(
        "just-under control: 32.7k-row store rescan green",
        r.returncode == 0
        and "too many SQL variables" not in (r.stderr or ""),
        f"rc={r.returncode} stderr tail={((r.stderr or '')[-300:])!r}",
    )

    # ---- leg D: byte-stability law — the chunked export is a pure
    # function of the data: two export_base runs, identical digests ----
    cfg_d = _mkstore("export", 0, tree_files=14)
    r = _rescan(cfg_d)
    check("export fixture rescan green (14 real files)", r.returncode == 0,
          f"rc={r.returncode}")

    def _digest_base() -> str:
        navconfig.use_config(cfg_d)
        navindex.export_base()
        base = navconfig.BASE_DIR
        h = hashlib.sha256()
        files = sorted(p for p in base.rglob("*") if p.is_file())
        for p in files:
            h.update(p.relative_to(base).as_posix().encode())
            h.update(p.read_bytes())
        return f"{len(files)}:{h.hexdigest()}"

    d1, d2 = _digest_base(), _digest_base()
    check("base export two-run digest identical (chunked, #327 law)",
          d1 == d2 and not d1.startswith("0:"), f"{d1} vs {d2}")

    # ---- leg E: the converted re-stamp site — full-metadata copy of a
    # 600-row (2-chunk) store lands count-intact and id-sorted --------
    if have:
        _mkstore("restamp", 600)
        col = navstore._collection()
        fresh = navstore._restamp(col, embed_mode="fake", doc_shape="raw")
        got = navstore.col_get_all(fresh, ["metadatas"], "leg E")
        check(
            "re-stamp via bounded read: 600 rows copied, sorted, stamped",
            fresh.count() == 600
            and got["ids"] == sorted(f"dead{i:06d}" for i in range(600))
            and (fresh.metadata or {}).get("hnsw:space") == "cosine",
            f"count={fresh.count()} "
            f"hnsw={(fresh.metadata or {}).get('hnsw:space')!r}",
        )

    # ---- leg F: the fifth converted site (#355) — navstore.clusters()
    # reads the whole store; pre-fix its raw col.get died at 33k rows
    # with the sqlite InternalError before any cluster math ran. The
    # sentinel stands in for clusters.topk_desc — the first consumer
    # AFTER the read — so the leg drives the production read path
    # without paying the n^2 top-k peak (~17 GB at 33k; CI runners
    # have 16). Pre-fix the InternalError fires first and the sentinel
    # never trips. ----------------------------------------------------
    if have:
        import clusters as _clusters_mod

        class _Past(Exception):
            pass

        def _sentinel(sim, _k):
            raise _Past(sim.shape[0])

        _orig_topk = _clusters_mod.topk_desc
        _clusters_mod.topk_desc = _sentinel
        try:
            for label, nrows in (
                ("past the ceiling", 33_000),
                ("just-under control", 32_700),
            ):
                _mkstore(f"clu{nrows}", nrows, vary_embs=True)
                try:
                    navstore.clusters()
                    seen = None   # sentinel never tripped — read dodged?
                except _Past as p:
                    seen = p.args[0]
                except Exception as e:   # pre-fix: the InternalError lands here
                    seen = f"{type(e).__name__}: {e}"
                check(
                    f"clusters() bounded read {label}: all {nrows:,} rows "
                    "reach the cluster math, no SQL-vars death",
                    seen == nrows,
                    f"topk_desc saw: {seen}",
                )
        finally:
            _clusters_mod.topk_desc = _orig_topk

    # ---- leg G: the full pipeline through the chunked read — two
    # 1300-row stores (3 pages), identical per-id data, reversed
    # insertion order in the second (#118: chroma's raw order tracks
    # store history, not data) must yield an identical cluster
    # structure over the same rows ------------------------------------
    if have:
        def _clu_digest(cfg: Path):
            navconfig.use_config(cfg)
            res = navstore.clusters()
            h = hashlib.sha256()
            for c in res:
                h.update(
                    f"{c['id']}|{c['size']}|{c.get('label')}|"
                    f"{c.get('method')}|".encode()
                )
                for p, cn in c["paths"]:
                    h.update(f"{p}:{cn};".encode())
            return f"{len(res)}:{h.hexdigest()}", sum(c["size"] for c in res)

        g1, s1 = _clu_digest(_mkstore("cluhist1", 1300, vary_embs=True))
        g2, s2 = _clu_digest(
            _mkstore("cluhist2", 1300, vary_embs=True, reverse=True)
        )
        check(
            "clusters() chunked read is history-blind: fwd vs reversed "
            "insertion give identical digests over the same 1300 rows",
            g1 == g2 and s1 == 1300 and s2 == 1300,
            f"{g1} (sum {s1}) vs {g2} (sum {s2})",
        )

    # ---- leg H: fresh-store settle law (#402) — every read primitive
    # (counts, collection opens, paged gets) rides the bounded
    # hnsw-settle retry. Five rerun-green sightings red on exactly the
    # bare counts gating the gets (first bake, base export, rescan's
    # existing-rows gate, a routed store's first contact). Teeth inject
    # the real error type+signature at the chroma layer — fail N-1
    # times, then the real read; beyond-bound must fail LOUD with the
    # original error, never a silent swallow -------------------------
    if have:
        _mkstore("settle", 300)
        real_count = chromadb.Collection.count
        real_get = chromadb.Collection.get
        settle_err = InternalError(
            "Error executing plan: Internal error: Error creating hnsw "
            "segment reader: Nothing found on disk"
        )
        real_pauses = navstore._CHROMA_READ_PAUSES_S
        # #402 teeth: only the first pause is shortened (wall time); the
        # later settle margins stay real so a LIVE transient firing inside
        # this window still recovers — one did during gate runs, and a
        # 0.0-pause ladder exhausted instantly against it.
        navstore._CHROMA_READ_PAUSES_S = (0.05,) + real_pauses[1:]
        try:
            def _flaky_count(fails: int):
                st = {"n": 0}

                def count_(self):
                    st["n"] += 1
                    if st["n"] <= fails:
                        raise settle_err
                    return real_count(self)

                return count_, st

            # (a) the gated count recovers across two transients and
            # logs each bounded attempt, naming the read and the error
            flaky, st = _flaky_count(2)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.object(
                    chromadb.Collection, "count", flaky):
                n = navstore.count()
            notes = err.getvalue()
            check(
                "#402 settle: count retries the transient, recovers, "
                "logs each attempt",
                n == 300 and st["n"] == 3
                and notes.count("chroma read retry") == 2
                and "1/4" in notes and "2/4" in notes
                and "store count" in notes and "InternalError" in notes,
                f"n={n} calls={st['n']} stderr={notes.strip()[-240:]!r}",
            )

            # (b) beyond-bound: the ORIGINAL error, loud, after the
            # bounded attempt count
            flaky, st = _flaky_count(99)
            err = io.StringIO()
            raised = None
            with contextlib.redirect_stderr(err), mock.patch.object(
                    chromadb.Collection, "count", flaky):
                try:
                    navstore.count()
                except Exception as e:  # noqa: BLE001 — the loud original
                    raised = e
            notes = err.getvalue()
            check(
                "#402 settle: beyond-bound raises the original error loud",
                isinstance(raised, InternalError)
                and "hnsw segment reader" in str(raised)
                and st["n"] == 1 + len(navstore._CHROMA_READ_PAUSES_S)
                and notes.count("chroma read retry")
                == len(navstore._CHROMA_READ_PAUSES_S),
                f"raised={raised!r} calls={st['n']} "
                f"stderr={notes.strip()[-240:]!r}",
            )

            # (c) determinism law: a paged read across one transient
            # returns the SAME id-sorted rows as the clean read
            clean = navstore.col_get_all(navstore._collection(),
                                         ["documents"], "leg H control")
            gst = {"n": 0}

            def _flaky_get(self, **kwargs):
                gst["n"] += 1
                if gst["n"] == 1:
                    raise settle_err
                return real_get(self, **kwargs)

            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.object(
                    chromadb.Collection, "get", _flaky_get):
                got = navstore.col_get_all(navstore._collection(),
                                           ["documents"], "leg H settle")
            check(
                "#402 settle: paged read across a transient is "
                "row-identical to the clean read",
                got == clean and got["ids"] == sorted(got["ids"])
                and gst["n"] == 2
                and err.getvalue().count("chroma read retry") == 1,
                f"calls={gst['n']} "
                f"stderr={err.getvalue().strip()[-200:]!r}",
            )

            # (d) wrong-signature errors never retry — loud, immediate
            def _hard(self):
                raise InternalError(
                    "Error executing plan: Internal error: sqlite disk full"
                )

            err = io.StringIO()
            raised = None
            with contextlib.redirect_stderr(err), mock.patch.object(
                    chromadb.Collection, "count", _hard):
                try:
                    navstore.count()
                except Exception as e:  # noqa: BLE001 — must stay loud
                    raised = e
            check(
                "#402 settle: unrelated errors stay immediate",
                isinstance(raised, InternalError)
                and "disk full" in str(raised)
                and "chroma read retry" not in err.getvalue(),
                f"raised={raised!r} "
                f"stderr={err.getvalue().strip()[-160:]!r}",
            )
        finally:
            navstore._CHROMA_READ_PAUSES_S = real_pauses

        # GK rider (#402, authorized single line): the routed-store
        # first-contact gate — sighting 5's exact window — was the
        # last bare count() left after the navstore sweep. It now
        # rides navstore.count(), the identical read leg (a) proves
        # race-survivable; pin the cutover so a revert fails here.
        src = (NAV_DIR / "server.py").read_text(encoding="utf-8")
        start = src.index("def _first_contact")
        gate = src[start:src.index("\ndef ", start)]
        check(
            "#402 settle: the routed first-contact gate rides the "
            "retry (no bare collection count left)",
            "navstore.count()" in gate
            and "._collection().count()" not in gate,
            gate.strip().splitlines()[0],
        )


    finish()


if __name__ == "__main__":
    main()
