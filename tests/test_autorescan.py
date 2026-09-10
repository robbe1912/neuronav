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

HERE = Path(__file__).resolve().parents[1]

TMP = Path(tempfile.mkdtemp(prefix="neuronav_autorescan_"))
(TMP / "src").mkdir(parents=True)
for i in range(3):
    (TMP / "src" / f"mod{i}_thing.py").write_text(
        f"def mod{i}_thing_run(scale):\n    return scale * {i}\n", encoding="utf-8"
    )

CFG = Path(tempfile.gettempdir()) / "neuronav_autorescan_config.json"
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

    def _rescan_wrap():
        calls["rescans"] += 1
        out = _orig_rescan()
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
        threading.Thread(target=self._drain_out, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()
        self._id = 0

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
        result = self.recv(timeout)["result"]
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
    cfg = Path(tempfile.gettempdir()) / f"neuronav_autorescan_{name}_config.json"
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
        n_before = _file_count(sp.call("repo_map", {"budget_tokens": 256}))
        check("e2e gate: repo_map header parsed", n_before > 0, f"n={n_before}")
        (TMP / "src" / "psi_barnacle.py").write_text(
            "def psi_barnacle_anchor(hold):\n    return hold\n", encoding="utf-8"
        )
        time.sleep(REAL_TTL_WAIT)  # let the server's TTL window expire
        after = sp.call("repo_map", {"budget_tokens": 256})
        check(
            "e2e gate: external add reflected in the next read tool",
            _file_count(after) == n_before + 1,
            f"{n_before} -> {_file_count(after)}",
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
        n0 = _file_count(sp.call("repo_map", {"budget_tokens": 256}))
        check("e2e watcher: repo_map header parsed", n0 > 0, f"n={n0}")
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
        n1 = _file_count(sp.call("repo_map", {"budget_tokens": 256}))
        check("e2e watcher: next read tool sees the indexed file", n1 == n0 + 1, f"{n0} -> {n1}")
    finally:
        sp.close()


if __name__ == "__main__":
    main()

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
