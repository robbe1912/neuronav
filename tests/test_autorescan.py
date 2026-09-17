# auto-rescan freshness gate (issue #19) — run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_autorescan.py
#
# Hermetic: generated temp target tree + config (never the real index),
# NEURONAV_EMBED_FAKE=1 (deterministic hash embeddings, no Ollama).
# Pins the issue #19 contract:
#   (a) external file add -> next read tool reflects it, no manual rescan
#       (stdio end-to-end against a spawned server.py)
#   (b) no-change TTL expiry re-embeds nothing
#   (c) watcher mode (watch_interval_s): touch -> indexed in the
#       background within ~interval + debounce (in-process + stdio e2e)
#   (d) embed-failure injection -> tool still answers, one stderr
#       warning, retry cooldown active, recovery after cooldown
#   (e) store lock held by a fake second process (issue #206) -> one
#       loud bounded-abort warn naming lock + holder, watcher thread
#       alive and serving, parked delta indexed on the next tick
import contextlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from filelock import FileLock  # the fake second process's lock holder

HERE = Path(__file__).resolve().parents[1]

TMP = Path(tempfile.mkdtemp(prefix="neuronav_autorescan_"))
(TMP / "src").mkdir(parents=True)
for i in range(3):
    (TMP / "src" / f"mod{i}_thing.py").write_text(
        f"def mod{i}_thing_run(scale):\n    return scale * {i}\n", encoding="utf-8"
    )

# per-run config under the suite's own mkdtemp — a fixed path in the
# shared tempdir let two overlapping autorescan runs clobber each
# other's config mid-read (same class as #152's baseindex note)
CFG = TMP / "config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "collection": "autorescan",
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
import server  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# The stdio sections below spawn server.py subprocesses, which read the
# real 3s TTL from nav.py — capture it before the in-process speedup.
REAL_TTL_WAIT = nav.STAT_TTL_S + 0.5
# Speed the in-process sections up: the TTL logic is what is pinned, not
# the 3s wall-clock default.
nav.STAT_TTL_S = 0.6
TTL_WAIT = 0.9


def main() -> None:
    # ---- bootstrap + fingerprint contract ---------------------------------
    stats0 = nav.rescan()
    nav.stat_mark_synced()
    check(
        "bootstrap: 3 files indexed",
        stats0["added"] == 3 and nav.count() == 3,
        str(stats0),
    )
    fp = nav.stat_fingerprint()
    check(
        "fingerprint keys == iter_files ids (same walk rules)",
        set(fp) == {nav.file_id(p) for p in nav.iter_files()},
        str(sorted(fp)),
    )
    check(
        "fingerprint values are (mtime_ns, size) pairs",
        all(isinstance(v, tuple) and len(v) == 2 for v in fp.values()),
    )

    calls = {"walks": 0, "rescans": 0, "embeds": 0}
    last_stats: dict = {}
    _orig_fp, _orig_rescan, _orig_embed = (
        nav.stat_fingerprint,
        nav.rescan,
        nav.embed,
    )

    def _fp_wrap():
        calls["walks"] += 1
        return _orig_fp()

    def _rescan_wrap(*args, **kwargs):
        calls["rescans"] += 1
        out = _orig_rescan(*args, **kwargs)
        last_stats.update(out)
        return out

    def _embed_wrap(texts):
        calls["embeds"] += 1
        return _orig_embed(texts)

    nav.stat_fingerprint = _fp_wrap
    nav.rescan = _rescan_wrap
    nav.embed = _embed_wrap

    # ---- burst protection + TTL expiry on a clean tree --------------------
    w0 = calls["walks"]
    server._auto_rescan()
    server._auto_rescan()
    check("burst: TTL window suppresses re-walks", calls["walks"] == w0)
    check("burst: clean tree triggers no rescan", calls["rescans"] == 0)

    time.sleep(TTL_WAIT)
    server._auto_rescan()
    check("TTL expiry re-walks once", calls["walks"] == w0 + 1)
    check("TTL expiry on unchanged tree: no rescan", calls["rescans"] == 0)

    # ---- external add -> gate rescans exactly the delta -------------------
    (TMP / "src" / "zeta_flux.py").write_text(
        "def fluxcapacitor_assembly(part):\n    return part * 3\n", encoding="utf-8"
    )
    time.sleep(TTL_WAIT)
    r0, e0 = calls["rescans"], calls["embeds"]
    server._auto_rescan()
    check("external add -> gate rescans", calls["rescans"] == r0 + 1)
    check(
        "external add -> only the delta embedded (file + fn batches)",
        calls["embeds"] >= e0 + 1
        and last_stats.get("added") == 1
        and last_stats.get("unchanged") == 3
        and last_stats.get("changed") == ["src/zeta_flux.py"],
        str(last_stats),
    )
    check("external add -> index reflects it", nav.count() == 4)
    r1 = calls["rescans"]
    server._auto_rescan()  # immediate second call: TTL-cached clean verdict
    check("post-sync burst: no re-rescan", calls["rescans"] == r1)

    # ---- (b) no-change TTL expiry re-embeds nothing ------------------------
    time.sleep(TTL_WAIT)
    e1 = calls["embeds"]
    stats_d = nav.rescan()
    check(
        "no-change rescan: all unchanged, zero embed calls",
        stats_d["unchanged"] == 4
        and stats_d["added"] == 0
        and stats_d["updated"] == 0
        and stats_d["deleted"] == 0
        and calls["embeds"] == e1,
        str(stats_d),
    )
    nav.stat_mark_synced()

    # ---- (d) embed-failure injection: loud degradation + cooldown ----------
    def _boom(texts):
        raise RuntimeError("simulated ollama outage")

    nav.embed = _boom
    mod0 = TMP / "src" / "mod0_thing.py"
    mod0.write_text(mod0.read_text(encoding="utf-8") + "# failure touch\n", encoding="utf-8")
    time.sleep(TTL_WAIT)
    r2 = calls["rescans"]
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out1 = server.semantic_search("fluxcapacitor", 5)
    check(
        "embed failure: tool still answers (degraded, hits intact)",
        "zeta_flux" in out1 and "degraded" in out1,
        out1[:160],
    )
    check("embed failure: rescan attempted once", calls["rescans"] == r2 + 1)
    check(
        "embed failure: exactly one stderr warning",
        err.getvalue().count("auto-rescan FAILED") == 1,
        err.getvalue().strip()[-200:],
    )

    time.sleep(TTL_WAIT)  # TTL expires, cooldown must still suppress
    with contextlib.redirect_stderr(err):
        server.semantic_search("fluxcapacitor", 3)
    check(
        "cooldown: retry suppressed after failure",
        calls["rescans"] == r2 + 1 and err.getvalue().count("auto-rescan FAILED") == 1,
    )

    server._rescan_failed_at = None  # simulate cooldown expiry
    time.sleep(TTL_WAIT)
    with contextlib.redirect_stderr(err):
        server.semantic_search("fluxcapacitor", 3)
    check(
        "cooldown expiry: retry happens",
        calls["rescans"] == r2 + 2 and err.getvalue().count("auto-rescan FAILED") == 2,
    )

    nav.embed = _embed_wrap  # backend recovers
    server._rescan_failed_at = None
    time.sleep(TTL_WAIT)
    with contextlib.redirect_stderr(err):
        server._auto_rescan()
    check(
        "recovery: rescan succeeds and clears failure state",
        calls["rescans"] == r2 + 3
        and last_stats.get("updated") == 1
        and server._rescan_failed_at is None,
        str(last_stats),
    )
    check("recovery: no new failure warning", err.getvalue().count("auto-rescan FAILED") == 2)

    # ---- (c) watcher mode, in-process: background rescan, no tool call -----
    watch_err = io.StringIO()
    _real_stderr = sys.stderr
    sys.stderr = watch_err
    try:
        server._start_watcher(0.3)
        (TMP / "src" / "omega_depot.py").write_text(
            "def omega_depot_run():\n    return 7\n", encoding="utf-8"
        )
        t0 = time.monotonic()
        deadline = t0 + 0.3 + server.WATCH_DEBOUNCE_S + 8.0
        while time.monotonic() < deadline and "auto-rescan: files" not in watch_err.getvalue():
            time.sleep(0.1)
        elapsed = time.monotonic() - t0
    finally:
        sys.stderr = _real_stderr
    check(
        "watcher: background rescan with no tool call in between",
        "auto-rescan: files" in watch_err.getvalue(),
        watch_err.getvalue().strip()[-200:],
    )
    check(
        "watcher: debounce delayed the rescan",
        elapsed >= server.WATCH_DEBOUNCE_S - 0.3,
        f"{elapsed:.1f}s",
    )
    check(
        "watcher: indexed within ~interval + debounce",
        elapsed < 0.3 + server.WATCH_DEBOUNCE_S + 8.0,
        f"{elapsed:.1f}s",
    )
    check("watcher: file indexed", nav.count() == 5, f"count={nav.count()}")

    # ---- (e) bounded lock wait (issue #206): a holder parks the gate --
    # never the watcher thread: one loud abort, cooldown, retry on release
    nav.rescan = _orig_rescan  # the counting wrap drops rescan's timeout
    real_wait = server.LOCK_WAIT_S
    server.LOCK_WAIT_S = 0.5
    watch_thread = next(
        (t for t in threading.enumerate() if t.name == "neuronav-watch"), None
    )
    check("lock leg: watcher thread located", watch_thread is not None)
    holder = FileLock(str(nav.DB_DIR / ".write.lock"))  # the other process
    holder.acquire()  # BEFORE the touch: no tick may win the rescan race
    lock_err = io.StringIO()
    try:
        (TMP / "src" / "held_vise.py").write_text(
            "def held_vise_clamp(force):\n    return force\n", encoding="utf-8"
        )
        with contextlib.redirect_stderr(lock_err):
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and (
                "gave up after" not in lock_err.getvalue()
            ):
                time.sleep(0.1)  # the daemon ticks, debounces, then aborts
            time.sleep(1.0)  # settle: a second abort inside 60s cooldown = bug
        served = server.search_text("omega_depot_run")
    finally:
        holder.release()
    warns = lock_err.getvalue()
    check(
        "lock held: exactly one loud abort naming lock + likely holder",
        warns.count("gave up after") == 1
        and ".write.lock" in warns
        and "another neuronav process" in warns,
        warns.strip()[-220:],
    )
    check("lock held: cooldown armed", server._rescan_failed_at is not None)
    check(
        "lock held: watcher thread stayed alive",
        watch_thread is not None and watch_thread.is_alive(),
    )
    check(
        "lock held: read tools still serve the current index",
        "omega_depot" in served,
        served[:160],
    )
    check("lock held: dirty file not indexed", nav.count() == 5, f"count={nav.count()}")

    rec_err = io.StringIO()
    with contextlib.redirect_stderr(rec_err):
        server._rescan_failed_at = None  # cooldown expiry (leg-d precedent)
        server._auto_rescan()  # the tick after release: rescan must proceed
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and nav.count() < 6:
            time.sleep(0.1)  # the daemon may own the recovery tick
    check(
        "released: next tick rescans the parked delta",
        nav.count() == 6 and "auto-rescan: files" in rec_err.getvalue(),
        rec_err.getvalue().strip()[-160:],
    )
    server.LOCK_WAIT_S = real_wait
    # ---- issue #239 pin: chroma reads survive the hnsw settle ----------
    chroma_retry_unit()

    # ---- (a) + (c) stdio end-to-end against a real server.py --------------

    e2e_gate()
    e2e_watcher()


class ServerProc:
    """Minimal MCP stdio client: spawn server.py on a given config and
    drive JSON-RPC (newline-delimited) with drain threads on both pipes."""

    def __init__(self, cfg_path: Path):
        env = dict(os.environ)
        env["NEURONAV_CONFIG"] = str(cfg_path)
        env["NEURONAV_EMBED_FAKE"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(HERE / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=HERE,
            env=env,
        )
        self.out_q: queue.Queue[str] = queue.Queue()
        self.err_lines: list[str] = []
        threading.Thread(target=getattr(self, "_drain_out"), daemon=True).start()
        threading.Thread(target=getattr(self, "_drain_err"), daemon=True).start()
        self._id = 0
        self.last_raw: dict = {}

    def _drain_out(self) -> None:
        for line in self.proc.stdout:
            self.out_q.put(line)

    def _drain_err(self) -> None:
        for line in self.proc.stderr:
            self.err_lines.append(line)

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 120.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.out_q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            msg = json.loads(line)
            if "id" in msg:
                return msg
        raise TimeoutError(
            f"no response within {timeout:.0f}s; stderr tail: {''.join(self.err_lines[-10:])}"
        )

    def call(self, name: str, args: dict, timeout: float = 120.0) -> str:
        self._id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        msg = self.recv(timeout)
        self.last_raw = msg  # issue #239: FAIL details quote what we got
        result = msg.get("result") or {}
        return "\n".join(
            b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"
        )

    def handshake(self) -> None:
        self._id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "autorescan", "version": "0"},
                },
            }
        )
        self.recv()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        self.proc.kill()


def _file_count(repo_map_out: str) -> int:
    m = re.search(r"(\d+) files", repo_map_out)
    return int(m.group(1)) if m else -1


def _e2e_cfg(name: str, state_subdir: str, extra: dict | None = None) -> Path:
    cfg = TMP / f"config_{name}.json"
    body = {
        "root": str(TMP),
        "collection": f"autorescan_{name}",
        "include_dirs": ["src"],
        "extensions": [".py"],
        "state_dir": str(TMP / state_subdir),
    }
    if extra:
        body.update(extra)
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def e2e_gate() -> None:
    """(a) watch off: an external add shows up in the NEXT read tool call."""
    sp = ServerProc(_e2e_cfg("e2e", "state_e2e"))
    try:
        sp.handshake()
        out_before = sp.call("repo_map", {"budget_tokens": 256})
        n_before = _file_count(out_before)
        check(
            "e2e gate: repo_map header parsed",
            n_before > 0,
            f"n={n_before}; answer={out_before[:200]!r}",
        )
        (TMP / "src" / "psi_barnacle.py").write_text(
            "def psi_barnacle_anchor(hold):\n    return hold\n", encoding="utf-8"
        )
        time.sleep(REAL_TTL_WAIT)  # let the server's TTL window expire
        after = sp.call("repo_map", {"budget_tokens": 256})
        check(
            "e2e gate: external add reflected in the next read tool",
            _file_count(after) == n_before + 1,
            f"{n_before} -> {_file_count(after)}; answer={after[:200]!r}",
        )
        hits = sp.call("semantic_search", {"query": "barnacle", "n": 5})
        check(
            "e2e gate: new file searchable without manual rescan",
            "psi_barnacle" in hits,
            hits[:200],
        )
        check(
            "e2e gate: auto-rescan logged on stderr",
            any("auto-rescan: files" in ln for ln in sp.err_lines),
            "".join(sp.err_lines[-5:]),
        )
    finally:
        sp.close()


def e2e_watcher() -> None:
    """(c) watch on: a touch is indexed in the background — the stderr
    rescan line must appear with NO tool call in between."""
    sp = ServerProc(_e2e_cfg("watch", "state_watch", {"watch_interval_s": 0.5}))
    try:
        sp.handshake()
        out0 = sp.call("repo_map", {"budget_tokens": 256})
        n0 = _file_count(out0)
        check(
            "e2e watcher: repo_map header parsed",
            n0 > 0,
            f"n={n0}; answer={out0[:200]!r}",
        )
        t0 = time.monotonic()
        (TMP / "src" / "tau_kiln.py").write_text(
            "def tau_kiln_fire(batch):\n    return batch\n", encoding="utf-8"
        )
        deadline = t0 + 0.5 + server.WATCH_DEBOUNCE_S + 8.0
        while time.monotonic() < deadline and not any(
            "auto-rescan: files" in ln for ln in sp.err_lines
        ):
            time.sleep(0.1)
        elapsed = time.monotonic() - t0
        check(
            "e2e watcher: background rescan without tool traffic",
            any("auto-rescan: files" in ln for ln in sp.err_lines),
            "".join(sp.err_lines[-5:]),
        )
        check(
            "e2e watcher: debounce honored",
            elapsed >= server.WATCH_DEBOUNCE_S - 0.5,
            f"{elapsed:.1f}s",
        )
        check(
            "e2e watcher: within ~interval + debounce",
            elapsed < 0.5 + server.WATCH_DEBOUNCE_S + 8.0,
            f"{elapsed:.1f}s",
        )
        out1 = sp.call("repo_map", {"budget_tokens": 256})
        n1 = _file_count(out1)
        check(
            "e2e watcher: next read tool sees the indexed file",
            n1 == n0 + 1,
            f"{n0} -> {n1}; answer={out1[:200]!r}",
        )
    finally:
        sp.close()


def chroma_retry_unit() -> None:
    """Issue #239 pin (constructed interleaving — the loaded-battery race
    is far too rare to wait for): a chroma read that hits the hnsw
    segment-settling transient retries loudly and serves the read, an
    unrelated error stays immediate, an always-settling store still
    raises after the bounded schedule (nothing silently degrades), and
    recall's vector ranks survive one transient through the real code
    path with a flaky collection stand-in."""
    import io
    import recall

    real_pauses = nav._CHROMA_READ_PAUSES_S
    nav._CHROMA_READ_PAUSES_S = (0.0, 0.0)  # instant pin, same retry count
    err = io.StringIO()
    calls = {"flaky": 0, "other": 0}

    def settling():
        calls["flaky"] += 1
        if calls["flaky"] < 3:
            raise RuntimeError(
                "Error executing plan: Internal error: Error creating "
                "hnsw segment reader: Nothing found on disk"
            )
        return {"ids": ["src/one.py"]}

    def unrelated():
        calls["other"] += 1
        raise ValueError("connection closed")

    def never_settles():
        calls["other"] += 1
        raise RuntimeError("Error creating hnsw segment reader: Nothing found on disk")
    calls = {"flaky": 0, "other": 0, "vec": 0}
    class FlakyCol:
        def count(self):
            return 2

        def query(self, **kwargs):
            calls["vec"] += 1
            if calls["vec"] == 1:
                raise RuntimeError(
                    "Error creating hnsw segment reader: Nothing found on disk"
                )
            return {
                "ids": [["src/one.py", "src/two.py"]],
                "metadatas": [[{"path": "src/one.py"}, {"path": "src/two.py"}]],
                "distances": [[0.25, 0.5]],
            }

    try:
        with contextlib.redirect_stderr(err):
            got = nav.chroma_read("pin-settle", settling)
            check(
                "settle unit: hnsw transient retries to a clean read",
                got == {"ids": ["src/one.py"]}
                and calls["flaky"] == 3
                and err.getvalue().count("chroma read retry") == 2,
                err.getvalue().strip()[-200:],
            )
            err.truncate(0)
            err.seek(0)
            try:
                nav.chroma_read("pin-unrelated", unrelated)
                raised = False
            except ValueError:
                raised = True
            check(
                "settle unit: unrelated errors stay immediate",
                raised and calls["other"] == 1
                and "chroma read retry" not in err.getvalue(),
                err.getvalue().strip()[-200:],
            )
            err.truncate(0)
            err.seek(0)
            try:
                nav.chroma_read("pin-exhaust", never_settles)
                raised = False
            except RuntimeError:
                raised = True
            check(
                "settle unit: exhaustion raises loud, bounded",
                raised
                and calls["other"] == 2 + len(nav._CHROMA_READ_PAUSES_S)
                and err.getvalue().count("chroma read retry")
                == len(nav._CHROMA_READ_PAUSES_S),
                f"attempts={calls['other']} "
                f"notes={err.getvalue().count('chroma read retry')}",
            )
            # consumer leg: recall's vector ranks go through the retry
            real_col = nav._collection
            nav._collection = lambda: FlakyCol()
            try:
                ids, metas, sims = recall._vector_ranks("kiln fire", 8)
            finally:
                nav._collection = real_col
            check(
                "settle unit: recall vector ranks survive one transient",
                ids == ["src/one.py", "src/two.py"]
                and metas["src/two.py"]["path"] == "src/two.py"
                and sims["src/one.py"] == 0.75
                and calls["vec"] == 2,
                f"ids={ids} attempts={calls['vec']}",
            )
    finally:
        nav._CHROMA_READ_PAUSES_S = real_pauses


if __name__ == "__main__":
    main()

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
