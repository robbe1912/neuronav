# stdio end-to-end smoke test for the MCP server:
#   .venv/Scripts/python.exe -X utf8 tests/test_server_stdio.py
#
# Spawns server.py as a real subprocess, drives JSON-RPC over stdio
# (newline-delimited, MCP stdio transport): initialize -> initialized ->
# tools/list -> tools/call context{...}. Asserts the context tool is
# advertised and answers with a real subsystem map on the index's
# most-wired file (config-agnostic — no hardcoded target paths).
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import graph  # noqa: E402  (repo root on path)

_g = graph.get_graph()
_call_wires: dict[str, int] = {}
for (_s, _d), _tys in _g.edge_types.items():
    if "call" in _tys:
        _sfi = _s.partition("::")[0]
        _call_wires[_sfi] = _call_wires.get(_sfi, 0) + 1
# most call-wired file: guarantees the context tool's edge-type section
# shows a call row on ANY config (config-agnostic, no hardcoded paths)
TARGET = max(sorted(_call_wires), key=lambda p: _call_wires[p])

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


def _spawn(env: dict[str, str]) -> SimpleNamespace:
    """Start one stdio server + daemon drain threads; returns
    .proc/.send/.recv/.kill/.stderr_lines.

    The server logs (startup + ollama HTTP) can outgrow the stderr pipe
    buffer and deadlock it if nobody drains — keep daemon readers
    (lambda-wrapped targets give the extractor a visible call site).
    recv enforces a REAL deadline (a blocked readline would otherwise
    ignore the timeout)."""
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
    stderr_lines: list[str] = []
    _stdout_q: "queue.Queue[str]" = queue.Queue()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    threading.Thread(target=lambda: _drain_stderr(), daemon=True).start()

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

    def kill() -> None:
        proc.kill()
        time.sleep(0.5)

    return SimpleNamespace(
        proc=proc, send=send, recv=recv, kill=kill, stderr_lines=stderr_lines
    )


TOOL_NAMES = (
    "explore", "repo_map", "semantic_search", "find_functions", "search_text",
    "symbol_graph", "dead_code", "duplicates", "clusters", "crosstalk",
    "context", "visualize", "rescan",
)


def main() -> None:
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    srv = _spawn(env)
    proc = srv.proc
    stderr_lines = srv.stderr_lines
    send, recv = srv.send, srv.recv

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
        check("tools/list advertises repo_map", "repo_map" in names, f"tools={names}")
        ann = next(
            (t.get("annotations") for t in tools if t["name"] == "repo_map"), None
        )
        check(
            "repo_map: readOnlyHint set",
            bool(ann and ann.get("readOnlyHint") is True),
            json.dumps(ann),
        )

        # issue #68: search_text joins the surface — this suite deliberately
        # pins every advertised tool's contract, so it pins the new one too
        # (advertisement + readOnlyHint + one config-agnostic call: "." hits
        # any non-empty line in any config, stays capped by construction)
        check(
            "tools/list advertises search_text",
            "search_text" in names,
            f"tools={names}",
        )
        st_ann = next(
            (t.get("annotations") for t in tools if t["name"] == "search_text"), None
        )
        check(
            "search_text: readOnlyHint set",
            bool(st_ann and st_ann.get("readOnlyHint") is True),
            json.dumps(st_ann),
        )
        # issue #131: universal mount — every tool gains the optional dir
        # param (empty = boot config's repo); the suite pins the surface,
        # so it pins the new parameter on all 13 tools
        check(
            "tools/list advertises exactly the 13 tools",
            sorted(names) == sorted(TOOL_NAMES),
            f"tools={names}",
        )
        for t in tools:
            schema = t.get("inputSchema") or {}
            check(
                f"{t['name']}: optional dir param advertised",
                "dir" in schema.get("properties", {})
                and "dir" not in schema.get("required", []),
                json.dumps(schema)[:200],
            )
        send(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "search_text", "arguments": {"pattern": "def "}},
            }
        )
        st = text_of(recv(9)["result"])
        st_rows = [ln for ln in st.splitlines() if re.match(r"^\S+:\d+:", ln)]
        check(
            "search_text: capped file:line rows or graceful empty",
            (len(st_rows) > 0 and " matches in " in st)
            or st.startswith("no matches for "),
            st.splitlines()[:2],
        )

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

        # repo_map: read-only orientation preamble — bounded output +
        # you-are-here header on every response
        send(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "repo_map", "arguments": {"budget_tokens": 512}},
            }
        )
        small = text_of(recv(6)["result"])
        check(
            "repo_map: you-are-here header",
            bool(re.match(r"you are here: .+ — \d+ files, \d+ clusters", small)),
            small.splitlines()[:1],
        )
        check(
            "repo_map: output bounded to budget",
            len(small) <= 512 * 4 + 512,
            f"{len(small)} chars for budget 512",
        )

        send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "repo_map", "arguments": {}},
            }
        )
        full = text_of(recv(7)["result"])
        check(
            "repo_map: default budget admits more than a 512 sliver",
            len(full) > len(small),
            f"{len(small)} -> {len(full)} chars",
        )

        # semantic_search: hybrid recall surface — you-are-here header,
        # src provenance per hit, 1-hop ctx neighbor labels
        send(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "semantic_search",
                    "arguments": {"query": "movement input handling", "n": 5},
                },
            }
        )
        sr = text_of(recv(8)["result"])
        check(
            "semantic_search: you-are-here header",
            bool(re.match(r"you are here: .+ — \d+ files, \d+ clusters", sr)),
            sr.splitlines()[:1],
        )
        body = [ln for ln in sr.splitlines() if "src=" in ln]
        check(
            "semantic_search: ranked hits with src provenance",
            len(body) >= 1
            and all(re.search(r"src=(vec|bm25|both)(?:\s|$)", ln) for ln in body),
            sr.splitlines()[1:4],
        )
        check("semantic_search: ctx neighbor labels", "ctx=[" in sr, sr.splitlines()[1:2])

        # issue #131: universal mount — one server, per-call dir routing,
        # multi-project isolation, fresh-dir build pinned on fake embeds
        _universal_scenario()
    finally:
        srv.kill()
        if FAILS:
            print("--- server stderr (tail) ---")
            print("\n".join(stderr_lines[-15:]))


def _universal_scenario() -> None:
    """issue #131 acceptance: one server entry, per-call dir. Two fresh
    scratch projects onboard on first contact (NEURONAV_EMBED_FAKE=1
    pins the build path — no ollama in the loop), then alternating dir
    calls must never cross indexes: you-are-here headers follow the dir,
    exclusive markers stay exclusive, and the boot project keeps
    serving between routed calls."""
    import shutil

    scratch = HERE / ".team_scratch" / "universal_stdio"
    shutil.rmtree(scratch, ignore_errors=True)

    def mkproj(name: str, files: dict[str, str]) -> Path:
        p = scratch / name
        p.mkdir(parents=True)
        for fn, body in files.items():
            (p / fn).write_text(body, encoding="utf-8", newline="\n")
        return p

    boot = mkproj("boot", {
        "boot_marker.py": "def boot_only():\n    return 'boot'\n",
        "boot_helper.py": "def boot_helper():\n    return 2\n",
    })
    pa = mkproj("alpha", {
        "alpha_widget.py": "class AlphaWidget:\n    def alpha_widget(self):\n        return 1\n",
        "alpha_service.py": "def alpha_service():\n        return 2\n",
        "alpha_extra.py": "ALPHA_TOKEN = 'alpha_widget_service'\n",
    })
    pb = mkproj("beta", {
        "beta_gadget.py": "class BetaGadget:\n    def beta_gadget(self):\n        return 1\n",
        "beta_store.py": "def beta_store():\n        return 2\n",
    })
    boot_cfg = scratch / "boot.neuronav.json"
    boot_cfg.write_text(json.dumps({
        "root": str(boot.resolve()),
        "collection": "main",
        "state_dir": "default",
        "include_dirs": ["."],
        "extensions": [".py"],
        "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav", "node_modules"],
    }), encoding="utf-8", newline="\n")

    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env["NEURONAV_CONFIG"] = str(boot_cfg)
    env["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(env)
    send, recv = srv.send, srv.recv

    def call(mid: int, name: str, args: dict) -> dict:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        return recv(mid)["result"]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "universal", "version": "0"}}})
        check("universal: initialize handshake", "result" in recv(1), "")

        r = call(2, "repo_map", {"budget_tokens": 256})
        out = text_of(r)
        check("universal: boot repo serves without dir",
              out.startswith(f"you are here: {boot.resolve().as_posix()}"), out[:100])

        # first contact onboards: rescan-format build summary + scaffold
        r = call(3, "semantic_search", {"query": "alpha widget", "dir": str(pa)})
        on_a = text_of(r)
        check("universal: fresh dir onboards with build summary",
              on_a.startswith(f"onboarded {pa.resolve().as_posix()} — index built:")
              and "(a/u/u/d)" in on_a, on_a[:160])
        check("universal: alpha store scaffolded",
              (pa / ".neuronav" / "config.json").is_file()
              and (pa / ".neuronav" / "chroma").is_dir(),
              str(sorted(p.name for p in (pa / ".neuronav").iterdir())))
        r = call(4, "semantic_search", {"query": "beta gadget", "dir": str(pb)})
        on_b = text_of(r)
        check("universal: second fresh dir onboards",
              on_b.startswith(f"onboarded {pb.resolve().as_posix()} — index built:"), on_b[:120])

        # alternation: headers follow the dir; file counts stay per project
        for i, (proj, nfiles) in enumerate(((pa, 3), (pb, 2)) * 2):
            out = text_of(call(10 + i, "repo_map", {"budget_tokens": 256, "dir": str(proj)}))
            check(f"universal: routed repo_map header names {proj.name}",
                  out.startswith(
                      f"you are here: {proj.resolve().as_posix()} — {nfiles} files,"),
                  out[:100])

        # cross-leak: exclusive markers never cross stores
        out = text_of(call(20, "search_text", {"pattern": "alpha_widget", "dir": str(pb)}))
        check("universal: alpha marker absent in beta",
              out.startswith("no matches for "), out[:80])
        out = text_of(call(21, "search_text", {"pattern": "beta_gadget", "dir": str(pa)}))
        check("universal: beta marker absent in alpha",
              out.startswith("no matches for "), out[:80])
        out = text_of(call(22, "search_text", {"pattern": "alpha_widget", "dir": str(pa)}))
        check("universal: alpha marker present in alpha",
              " matches in " in out, out[:80])
        out = text_of(call(23, "search_text", {"pattern": "boot_only", "dir": str(pb)}))
        check("universal: boot marker absent in beta",
              out.startswith("no matches for "), out[:80])

        # routed fn index answers from the routed repo only (key = path#func)
        r = call(24, "find_functions", {"query": "alpha widget", "dir": str(pa)})
        rows = text_of(r)
        check("universal: find_functions routed to alpha",
              "alpha_widget.py#alpha_widget" in rows and "beta" not in rows,
              rows[:120])

        # boot state exact-restored between routed calls
        out = text_of(call(25, "repo_map", {"budget_tokens": 256}))
        check("universal: boot repo still serves after routed calls",
              out.startswith(f"you are here: {boot.resolve().as_posix()}"), out[:100])

        # missing dir: loud MCP error naming the dir (content blocks carry
        # the raw path — text_of's isError leg JSON-escapes backslashes)
        missing = str(scratch / "no_such_dir")
        r = call(26, "semantic_search", {"query": "x", "dir": missing})
        check("universal: missing dir is a loud MCP error",
              bool(r.get("isError"))
              and any(missing in b.get("text", "") for b in r.get("content", [])),
              text_of(r)[:200])

        # bad foreign config: loud error naming state_dir, server survives
        bad = mkproj("badcfg", {"bad_x.py": "X = 1\n"})
        (bad / ".neuronav").mkdir()
        (bad / ".neuronav" / "config.json").write_text('{"root": "."}', encoding="utf-8")
        r = call(27, "repo_map", {"dir": str(bad)})
        check("universal: state_dir-less foreign config is a loud error",
              bool(r.get("isError")) and "state_dir" in text_of(r), text_of(r)[:200])
        out = text_of(call(28, "repo_map", {"budget_tokens": 256}))
        check("universal: server alive after foreign-config error",
              out.startswith(f"you are here: {boot.resolve().as_posix()}"), out[:100])
    finally:
        srv.kill()
        if FAILS:
            print("--- universal server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))

if __name__ == "__main__":
    main()

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
