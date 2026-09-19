"""Handshake-first boot (issue #273): the MCP initialize/tools-list
handshake must be answered while the boot work (chroma store open/build,
rescan, warm C-extension imports) is still in progress — connection time
may never include creating the store.

Deterministic teeth: the parent process HOLDS the store's cross-process
write lock (the same FileLock navindex.rescan acquires), so the server's boot
is provably blocked mid-rescan; the handshake must still complete. At the
pre-fix HEAD the boot ran on the main thread before mcp.run(), so
initialize received no answer until the lock released — this suite FAILS
there via the bounded recv, and passes once boot moved behind the
handshake on the daemon thread.

Run: python -X utf8 tests/test_bootgate.py [server_dir]
server_dir defaults to the checkout holding this tests/ tree; passing a
different checkout runs the same scenario against that code (how the
pre-fix FAIL evidence was recorded).
"""

import json
import os
import subprocess
import sys
import tempfile
import shutil
import subprocess
import threading
import time
from pathlib import Path

os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")

SERVER_DIR = (
    Path(sys.argv[1]).resolve() if len(sys.argv) > 1
    else Path(__file__).resolve().parents[1]
)
sys.path.insert(0, str(SERVER_DIR))  # the checkout under test wins over any editables



import harness

check = harness.styled("colon")  # byte pin: "PASS: <name>" lines

def recv_id(proc: subprocess.Popen, want_id: int, deadline_s: float) -> dict | None:
    """Next JSON-RPC response with the wanted id, or None at the deadline."""
    deadline = time.monotonic() + deadline_s
    got: dict | None = None

    def reader() -> None:
        nonlocal got
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError as e:
                print(f"[dbg-json] {e}", file=sys.stderr, flush=True)
                continue
            if msg.get("id") == want_id:
                got = msg
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(max(0.1, deadline - time.monotonic()))
    return got


def main() -> None:
    # best-effort cleanup: on Windows the parent's nav import can keep
    # sqlite handles on the scratch store past teardown — a cleanup
    # failure must never mask the verdict
    with tempfile.TemporaryDirectory(
        prefix="nn-bootgate-", ignore_cleanup_errors=True
    ) as td:
        root = Path(td)
        proj = root / "proj"
        (proj / "pkg").mkdir(parents=True)
        (proj / "pkg" / "core.py").write_text(
            "def bootgate_fixture_fn():\n    return 42\n", encoding="utf-8"
        )
        (proj / "pkg" / "side.py").write_text(
            "from pkg.core import bootgate_fixture_fn\n"
            "def side_user():\n    return bootgate_fixture_fn()\n",
            encoding="utf-8",
        )
        state = root / "state"
        cfg = root / "probe.neuronav.json"
        cfg.write_text(json.dumps({
            "root": str(proj),
            "collection": "bootgate",
            "include_dirs": ["pkg"],
            "extensions": [".py"],
            "exclude_dirs": [],
            "state_dir": str(state),
        }), encoding="utf-8")

        env = {**os.environ, "NEURONAV_CONFIG": str(cfg)}
        # parent-side nav under the SAME config: the lock file derives
        # from the same state_dir, so this is the server's boot lock
        os.environ["NEURONAV_CONFIG"] = str(cfg)
        import navconfig, navindex, navstore
        assert navconfig.CONFIG_PATH is not None

        lock = navstore._db_lock(timeout=10)
        lock.acquire()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(SERVER_DIR / "server.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(SERVER_DIR),
                env=env,
            )
            assert proc.stdin is not None and proc.stderr is not None
            stderr_lines: list[str] = []

            def _drain() -> None:
                for line in proc.stderr:
                    stderr_lines.append(line)

            threading.Thread(target=_drain, daemon=True).start()
            try:
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "bootgate", "version": "0"},
                    },
                }) + "\n")
                proc.stdin.flush()
                init = recv_id(proc, 0, 10.0)
                check(
                    "initialize answered while boot blocked on the store lock",
                    init is not None and "result" in init,
                    "" if init else "no response within 10s (boot ran pre-handshake)",
                )
                if init is not None:
                    proc.stdin.write(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    }) + "\n")
                    proc.stdin.flush()
                    proc.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    }) + "\n")
                    proc.stdin.flush()
                    tools = recv_id(proc, 1, 10.0)
                    names = (
                        sorted(t["name"] for t in tools["result"]["tools"])
                        if tools and "result" in tools else []
                    )
                    check(
                        "tools/list answered pre-boot",
                        "repo_map" in names and "crosstalk" in names,
                        f"{len(names)} tools",
                    )
            finally:
                lock.release()  # boot may proceed (LOCK_WAIT_S=60 budget)

            boot_done = False
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                if any("startup files" in l for l in stderr_lines):
                    boot_done = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            check("boot completes after lock release", boot_done,
                  "\n".join(stderr_lines[-4:]))

            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "repo_map", "arguments": {}},
            }) + "\n")
            proc.stdin.flush()
            rep = recv_id(proc, 2, 60.0)
            text = (
                rep["result"]["content"][0]["text"]
                if rep and "result" in rep else ""
            )
            check("repo_map serves the fixture post-boot",
                  "bootgate_fixture_fn" in text or "core.py" in text,
                  text[:120])
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()

    # byte pin: pass/total ratio summary, now off the shared sink
    print(f"\n{harness.EXECUTED - len(harness.FAILURES)}/{harness.EXECUTED} checks passed")
    if harness.FAILURES:
        sys.exit(1)


if __name__ == "__main__":
    main()
