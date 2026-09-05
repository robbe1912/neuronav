# stdio end-to-end smoke test for the MCP server:
#   .venv/Scripts/python.exe -X utf8 tests/test_server_stdio.py
#
# Spawns server.py as a real subprocess, drives JSON-RPC over stdio
# (newline-delimited, MCP stdio transport): initialize -> initialized ->
# tools/list -> tools/call context{...}. Asserts the context tool is
# advertised and answers with a real subsystem map on a known SWMG file.
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TARGET = "scripts/magic/magicplayer.gd"

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def text_of(result: dict) -> str:
    if result.get("isError"):
        return json.dumps(result)[:400]
    return "\n".join(
        b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"
    )


def main() -> None:
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(HERE / "server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=HERE,
        env=env,
    )

    # the server logs (startup + ollama HTTP) can outgrow the stderr pipe
    # buffer and deadlock it if nobody drains — keep a daemon reader
    # (lambda-wrapped targets give the extractor a visible call site)
    stderr_lines: list[str] = []
    _stderr_q: "queue.Queue[str]" = queue.Queue()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            _stderr_q.put(line)

    threading.Thread(target=lambda: _drain_stderr(), daemon=True).start()

    # stdout lines land in a queue; recv enforces a REAL deadline (a blocked
    # readline would otherwise ignore the timeout)
    _stdout_q: "queue.Queue[str]" = queue.Queue()

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            _stdout_q.put(line)

    threading.Thread(target=lambda: _drain_stdout(), daemon=True).start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(want_id, timeout: float = 300.0) -> dict:
        """Next response with `want_id`, or raise TimeoutError."""
        deadline = time.time() + timeout
        pending: list[str] = []
        while time.time() < deadline:
            try:
                line = _stdout_q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            msg = json.loads(line)
            if msg.get("id") == want_id:
                return msg
            pending.append(line)
        tail = "\n".join((stderr_lines + pending)[-12:])
        raise TimeoutError(
            f"no response for id={want_id} within {timeout:.0f}s.\n"
            f"server alive: {proc.poll() is None}\n--- tail ---\n{tail}"
        )

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"},
                },
            }
        )
        init = recv(1)
        check(
            "initialize handshake",
            "result" in init and "protocolVersion" in init.get("result", {}),
            json.dumps(init)[:200],
        )
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(2)["result"]["tools"]
        names = [t["name"] for t in tools]
        check("tools/list advertises context", "context" in names, f"tools={names}")

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "context", "arguments": {"path": TARGET, "depth": 1}},
            }
        )
        out = text_of(recv(3)["result"])
        check("context: file header", TARGET in out, out.splitlines()[:1])
        check("context: cluster section", "cluster:" in out)
        check("context: structural neighbors", "structural neighbors (depth 1):" in out)
        check("context: edge-type counts", "call x" in out)
        check("context: semantic neighbors", "semantic neighbors (cosine):" in out)
        check("context: hub rank", "hub:" in out and "rank" in out)

        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "context",
                    "arguments": {"path": "scripts/magic/definitely_missing.gd"},
                },
            }
        )
        miss = text_of(recv(4)["result"])
        check("context: unknown file is graceful", "unknown file" in miss, miss[:120])

        send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "context", "arguments": {}},
            }
        )
        over = text_of(recv(5)["result"])
        check(
            "context: no-path clusters overview",
            "clusters overview" in over and "ext=" in over,
            over.splitlines()[:1],
        )
    finally:
        proc.kill()
        time.sleep(0.5)
        if FAILS:
            print("--- server stderr (tail) ---")
            print("\n".join(stderr_lines[-15:]))


if __name__ == "__main__":
    main()

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
