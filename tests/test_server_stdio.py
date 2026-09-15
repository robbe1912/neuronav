# stdio end-to-end smoke test for the MCP server:
#   .venv/Scripts/python.exe -X utf8 tests/test_server_stdio.py
#
# Spawns server.py as a real subprocess, drives JSON-RPC over stdio
# (newline-delimited, MCP stdio transport): initialize -> initialized ->
# tools/list -> tools/call context{...}. Asserts the context tool is
# advertised and answers with a real subsystem map on the index's
# most-wired file (config-agnostic — no hardcoded target paths).
#
# Two legs, self-selected (issue #180): with the owner's checkout-local
# config.json the main server binds the default profile exactly as
# before. On a fresh checkout (CI: no config anywhere, NEURONAV_EMBED_FAKE
# in the env) the suite binds the self-index profile and self-populates
# its store in-job via one FAKE-embed rescan (the #166 count==0
# bootstrap from test_recall) — the store lives in the checkout's
# gitignored .neuronav, never committed. The drift, routed-freshness,
# recall-knobs and degraded scenarios below run on hermetic scratch
# trees in both legs.
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

# CI hermetic leg (issue #180): the spawned main server strips
# NEURONAV_CONFIG and re-runs nav's discovery from cwd=HERE —
# project-local .neuronav/config.json, then the checkout-local
# config.json. Neither exists on a fresh checkout, so with FAKE embeds
# available the suite self-selects the self-index profile instead
# (setdefault: an explicit export still wins, the test_explore law).
CI_HERMETIC = (
    os.environ.get("NEURONAV_EMBED_FAKE") == "1"
    and not (HERE / ".neuronav" / "config.json").is_file()
    and not (HERE / "config.json").is_file()
)
EMBED_FAKE = os.environ.get("NEURONAV_EMBED_FAKE") == "1"
if CI_HERMETIC:
    os.environ.setdefault(
        "NEURONAV_CONFIG", str(HERE / "config" / "neuronav.json")
    )

import graph  # noqa: E402  (repo root on path)
import nav  # noqa: E402

if CI_HERMETIC and nav.count() == 0:
    # GK #166 F1 bootstrap: fresh checkout, empty self-index store — one
    # FAKE-embed rescan self-populates it (deterministic hash vectors,
    # sorted walk); skipped when the store already serves (test_recall
    # ran earlier in the CI job)
    nav.rescan()

_g = graph.get_graph()
_call_wires: dict[str, int] = {}
for (_s, _d), _tys in _g.edge_types.items():
    if "call" in _tys:
        _sfi = _s.partition("::")[0]
        _call_wires[_sfi] = _call_wires.get(_sfi, 0) + 1
# most call-wired file: guarantees the context tool's edge-type section
# shows a call row on ANY config (config-agnostic, no hardcoded paths)
TARGET = max(sorted(_call_wires), key=lambda p: _call_wires[p])

# issue #125: most-called fn, derived from the graph like TARGET above —
# the symbol_graph count checks key on a hub that exists on ANY config.
HUBFN = max(
    sorted(_g.reverse),
    key=lambda k: len(_g.reverse.get(k) or ()),
    default="",
).partition("::")[2]

# the stat gate's TTL cache is real (3s) — drift legs wait one window
# out so the next read tool re-walks (test_autorescan's e2e precedent)
TTL_WAIT = nav.STAT_TTL_S + 0.5

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _pkg_version() -> str:
    """The version serverInfo must report (issue #207) — importlib
    metadata for an installed copy, else the pyproject beside the
    checkout. Same derivation as server.py's _version, so the pin
    tracks the source of truth in either install state."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("neuronav")
    except PackageNotFoundError:
        import tomllib
        with open(HERE / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]


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
        proc.wait(timeout=10)  # reap: an unwaited child holds pipes (CI flake)
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
    # issue #160: the MCP crosstalk shape keeps the pre-#144 top-2
    # top-files per pair (fmt_crosstalk top_n=2), unpadded pair colon
    import clusters as _clusters

    _rep = {
        "clusters": 2, "internal_edges": 3, "external_edges": 4,
        "external_ratio": 0.5, "unclustered_endpoint_edges": 0,
        "tests_endpoint_edges": 0,
        "by_cluster": [],
        "worst_pairs": [{"a": "A", "b": "B", "edges": 4, "top_files": [
            {"pair": "x -> y", "w": 3}, {"pair": "p -> q", "w": 2},
            {"pair": "m -> n", "w": 1},
        ]}],
    }
    _out = _clusters.fmt_crosstalk(_rep, top_n=2)
    check(
        "crosstalk MCP shape: top-2 files, unpadded colon (issue #160)",
        "x -> y x3" in _out and "p -> q x2" in _out and "m -> n" not in _out
        and "A <-> B:" in _out and "cross-cluster 4 (" in _out,
        _out.splitlines()[-1],
    )

    if CI_HERMETIC:
        # hermetic leg: the main server rides the self-index profile the
        # bootstrap above populated
        env = dict(os.environ)
    else:
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
        # issue #207: serverInfo must answer neuronav's package version,
        # not the mcp library's — the lowlevel Server defaults to
        # pkg_version("mcp") when nothing pins it
        sinfo = init.get("result", {}).get("serverInfo", {})
        check(
            "serverInfo answers the neuronav version (issue #207)",
            sinfo.get("name") == "neuronav" and sinfo.get("version") == _pkg_version(),
            f"serverInfo={sinfo} expected {_pkg_version()}",
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

        # issue #125: context names the file's own API surface (capped
        # defines rows) instead of leaving the agent to open it blind
        check("context: defines section lists the file's API (issue #125)",
              "defines:" in out and "func(s)" in out
              and any(ln.startswith("defines: ") for ln in out.splitlines()),
              out.splitlines()[:3])

        # issue #125: symbol_graph rows carry true counts (derived hub —
        # config-agnostic), and explore's orientation knob rides the wire
        send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
              "params": {"name": "symbol_graph",
                         "arguments": {"symbol": HUBFN, "depth": 1}}})
        sg = text_of(recv(10)["result"])
        check("symbol_graph: rows carry true caller/callee counts (issue #125)",
              bool(re.search(r"callers: \d+ \(", sg))
              and bool(re.search(r"callees: \d+ \(", sg)),
              sg.splitlines()[:3])
        ex_schema = next(
            (t.get("inputSchema") or {} for t in tools if t["name"] == "explore"),
            {},
        )
        check("explore: orientation advertised optional boolean (issue #125)",
              ex_schema.get("properties", {}).get("orientation", {}).get("type")
              == "boolean"
              and "orientation" not in ex_schema.get("required", []),
              json.dumps(ex_schema)[:200])
        send({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
              "params": {"name": "explore",
                         "arguments": {"query": "cluster labeling", "n": 3,
                                       "orientation": False}}})
        ex = text_of(recv(11)["result"])
        check("explore: orientation=False skips the preamble (issue #125)",
              "== repo map ==" not in ex and "== clusters ==" not in ex,
              ex[:160])

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

        # issue #180: the recall knobs (#73 graph_boost, #74 two_pass)
        # ride the MCP surface too, not just the library call
        _recall_knobs_scenario(srv)

        # issue #131: universal mount — one server, per-call dir routing,
        # multi-project isolation, fresh-dir build pinned on fake embeds
        _universal_scenario()

        # issue #180 CI legs: drift/stat-gate consistency + degraded
        # semantics, both on hermetic scratch trees (run in both modes)
        _drift_scenario()
        _degraded_scenario()
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

        # issue #180 routed drift: alpha's tree grows between routed
        # calls — the next routed call must heal it (config_scope's
        # per-config fingerprint cache feeds the stat gate), never serve
        # silently stale; and the boot scope's gate stays unpolluted.
        (pa / "alpha_drift.py").write_text(
            "def routed_drift_marker():\n    return 3\n",
            encoding="utf-8", newline="\n",
        )
        time.sleep(TTL_WAIT)
        out = text_of(call(29, "search_text",
                           {"pattern": "routed_drift_marker", "dir": str(pa)}))
        time.sleep(0.3)  # stderr drain settle
        check("universal: routed drift heals on the next routed call",
              " matches in " in out
              and any("routed drift healed" in ln for ln in srv.stderr_lines),
              out[:120])
        out = text_of(call(30, "repo_map", {"budget_tokens": 256, "dir": str(pa)}))
        check("universal: healed routed index serves the grown file count",
              out.startswith(f"you are here: {pa.resolve().as_posix()} — 4 files,"),
              out[:100])
        time.sleep(TTL_WAIT)
        n_boot = len([ln for ln in srv.stderr_lines if "auto-rescan: files" in ln])
        out = text_of(call(31, "repo_map", {"budget_tokens": 256}))
        time.sleep(0.3)
        check("universal: boot scope gate unpolluted after routed heal",
              out.startswith(f"you are here: {boot.resolve().as_posix()}")
              and len([ln for ln in srv.stderr_lines
                       if "auto-rescan: files" in ln]) == n_boot,
              out[:100])
    finally:
        srv.kill()
        if FAILS:
            print("--- universal server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))


def _recall_knobs_scenario(srv) -> None:
    """issue #180: the recall knobs ride the MCP surface, not just the
    library call — optional params advertised, negative boost refused
    loudly over the wire, graph_boost=16 strictly lifts a 1-hop neighbor
    of the first wired hit (the #73/#166-F2 rank-up invariant, ctx
    labels as the wire-visible adjacency), two_pass tags its rows, and
    both double-runs are byte-stable across the process boundary."""
    send, recv = srv.send, srv.recv
    q = "graph signal wiring edges"  # proven non-vacuous (test_recall)

    def call(mid: int, args: dict) -> str:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": "semantic_search",
                         "arguments": {"query": q, **args}}})
        return text_of(recv(mid)["result"])

    send({"jsonrpc": "2.0", "id": 90, "method": "tools/list"})
    schema = next(
        (t.get("inputSchema") or {}) for t in recv(90)["result"]["tools"]
        if t["name"] == "semantic_search"
    )
    props = schema.get("properties", {})
    check(
        "wire: two_pass + graph_boost advertised optional",
        {"two_pass", "graph_boost"} <= set(props)
        and not ({"two_pass", "graph_boost"} & set(schema.get("required", []))),
        json.dumps(schema)[:200],
    )

    send({"jsonrpc": "2.0", "id": 91, "method": "tools/call",
          "params": {"name": "semantic_search",
                     "arguments": {"query": q, "n": 3, "graph_boost": -0.5}}})
    bad = recv(91)["result"]
    check(
        "wire: graph_boost<0 refused loudly (isError naming the knob)",
        bool(bad.get("isError")) and "graph_boost" in text_of(bad),
        text_of(bad)[:160],
    )

    row_re = re.compile(r"^(\d+\.\d+)  (\S+)  src=(\S+)  ctx=\[([^\]]*)\]", re.M)
    base = call(92, {"n": 12})
    base2 = call(93, {"n": 12})
    if EMBED_FAKE:
        check(
            "wire: semantic_search byte-stable (double-run)",
            base == base2 and len(row_re.findall(base)) == 12,
            base[:140],
        )
    else:
        # real-embed legs: backend float noise (4th-decimal score wobble,
        # the #175/#184 flake class) even reorders near-tied rows across
        # back-to-back calls — observed 0.0305/0.0308 with the n=12
        # boundary row swapping seats. Byte-exact double-runs are the
        # FAKE contract; the real leg pins the row shape and loud-skips
        # the pair compare.
        print("SKIP byte-exact double-run compare (real embeds: float "
              "noise reorders near-ties) — row shape still pinned")
        check(
            "wire: semantic_search row shape (real-embed leg)",
            len(row_re.findall(base)) == 12,
            base[:140],
        )
    check("wire: no 2pass tag without two_pass", " 2pass" not in base, base[:140])

    strong = call(94, {"n": 12, "graph_boost": 16.0})
    pb = [(m.group(2), m.group(4)) for m in row_re.finditer(base)]
    ps = [(m.group(2), m.group(4)) for m in row_re.finditer(strong)]
    wired = next(
        ((i, f, [c.strip() for c in ctx.split(",") if c.strip()])
         for i, (f, ctx) in enumerate(pb) if ctx),
        None,
    )
    check("wire: wired hit carries ctx neighbors (non-vacuous)",
          wired is not None, base[:160])
    if wired:
        _i0, f0, nb0 = wired
        rank_b = {f: i for i, (f, _) in enumerate(pb)}
        lifted = [
            (f, rank_b.get(f, len(pb)), i)
            for i, (f, _) in enumerate(ps)
            if f in nb0 and i < rank_b.get(f, len(pb))
        ]
        check(
            "wire: graph_boost=16 strictly lifts a wired 1-hop neighbor",
            bool(lifted),
            f"wired0={f0} nb0={nb0[:3]}",
        )

    tp = call(95, {"n": 12, "two_pass": True})
    tp2 = call(96, {"n": 12, "two_pass": True})
    if "degraded: BM25F-only" in base:
        # dead backend: pass 2 is never attempted on a degraded vector
        # side — the BM25F-only contract serves pass-1 bytes unchanged
        check(
            "wire: two_pass skipped on a degraded vector side (contract)",
            tp == base,
            tp[:140],
        )
    else:
        tp_rows = [ln for ln in tp.splitlines() if "src=" in ln]
        check(
            "wire: two_pass marks every row",
            len(tp_rows) == 12 and all(ln.rstrip().endswith("2pass") for ln in tp_rows),
            "\n".join(tp_rows[:2]),
        )
    check("wire: two_pass response byte-stable (double-run)",
          tp == tp2 if EMBED_FAKE else True, "")
    if not EMBED_FAKE:
        print("SKIP byte-exact two_pass double-run (real embeds)")


def _drift_scenario() -> None:
    """issue #180 CI-leg (1)+(2): drift over real stdio on a scratch
    boot project — edits between calls must be served by the NEXT read
    tool (never silently stale), a touch-without-edit must diverge
    observably from a real edit (the stat gate's mtime/size detection
    through the whole path: churn vs no churn, byte-identical results),
    a revert restores the baseline, a delete purges. The watcher stays
    off (its default) so every heal is attributed to the read-tool gate
    under test."""
    import shutil

    scratch = HERE / ".team_scratch" / "drift_stdio"
    shutil.rmtree(scratch, ignore_errors=True)
    proj = scratch / "proj"
    proj.mkdir(parents=True)
    a0 = "def drift_anchor_a():\n    return 'a'\n"
    b0 = "def drift_anchor_b():\n    return 'b'\n"
    (proj / "drift_a.py").write_text(a0, encoding="utf-8", newline="\n")
    (proj / "drift_b.py").write_text(b0, encoding="utf-8", newline="\n")
    cfg = scratch / "drift.neuronav.json"
    cfg.write_text(json.dumps({
        "root": str(proj.resolve()),
        "collection": "main",
        "state_dir": "default",
        "include_dirs": ["."],
        "extensions": [".py"],
        "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav", "node_modules"],
    }), encoding="utf-8", newline="\n")

    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env["NEURONAV_CONFIG"] = str(cfg)
    env["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(env)
    send, recv = srv.send, srv.recv

    def call(mid: int, name: str, args: dict) -> str:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        return text_of(recv(mid)["result"])

    def churn() -> list[str]:
        time.sleep(0.3)  # let the stderr drain thread land the lines
        return [ln for ln in srv.stderr_lines if "auto-rescan: files" in ln]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "drift", "version": "0"}}})
        check("drift: initialize handshake", "result" in recv(1), "")

        base = call(2, "repo_map", {"budget_tokens": 256})
        check("drift: boot project serves (2 files)",
              base.startswith(
                  f"you are here: {proj.resolve().as_posix()} — 2 files,"),
              base[:100])
        st = call(3, "search_text", {"pattern": "drift_anchor"})

        # real edit: the next read tool must serve it, never silently stale
        (proj / "drift_a.py").write_text(
            a0 + "def drift_marker_v1():\n    return 1\n",
            encoding="utf-8", newline="\n",
        )
        time.sleep(TTL_WAIT)
        call(4, "repo_map", {"budget_tokens": 256})
        check("drift: real edit healed by the next read tool (churn 0/1/1/0)",
              any("auto-rescan: files 0/1/1/0" in ln for ln in churn()),
              "\n".join(srv.stderr_lines[-4:]))
        st2 = call(5, "search_text", {"pattern": "drift_marker_v1"})
        check("drift: new marker served without a manual rescan",
              " matches in " in st2, st2[:100])
        st3 = call(6, "search_text", {"pattern": "drift_anchor_a"})
        check("drift: pre-existing content still served after the heal",
              " matches in " in st3, st3[:100])

        # touch-without-edit: same bytes, new mtime — the stat gate must
        # see the walk change yet the sha gate keep it a no-op: no churn,
        # byte-identical results (mtime/size detection through the path)
        os.utime(proj / "drift_a.py")
        time.sleep(TTL_WAIT)
        before = len(churn())
        out4 = call(7, "search_text", {"pattern": "drift_anchor"})
        after = len(churn())
        check("drift: touch-without-edit adds no churn", after == before,
              f"{before} -> {after}")
        check("drift: touch-without-edit keeps results byte-identical",
              out4 == st, f"{len(st)} vs {len(out4)} chars")

        # revert: churn names the update, the marker is gone again
        (proj / "drift_a.py").write_text(a0, encoding="utf-8", newline="\n")
        time.sleep(TTL_WAIT)
        call(8, "repo_map", {"budget_tokens": 256})
        lines = churn()
        check("drift: revert heals back (churn 0/1/1/0)",
              len(lines) == 2 and "auto-rescan: files 0/1/1/0" in lines[-1],
              lines[-1] if lines else "(no churn lines)")
        st5 = call(9, "search_text", {"pattern": "drift_marker_v1"})
        check("drift: reverted marker gone",
              st5.startswith("no matches for "), st5[:80])

        # delete: churn names the purge, the file stops being served
        (proj / "drift_b.py").unlink()
        time.sleep(TTL_WAIT)
        out = call(10, "repo_map", {"budget_tokens": 256})
        lines = churn()
        check("drift: delete heals (churn 0/0/1/1, header shrinks)",
              "auto-rescan: files 0/0/1/1" in lines[-1]
              and out.startswith(
                  f"you are here: {proj.resolve().as_posix()} — 1 files,"),
              lines[-1] if lines else "(no churn lines)")
        st6 = call(11, "search_text", {"pattern": "drift_anchor_b"})
        check("drift: deleted file no longer served",
              st6.startswith("no matches for "), st6[:80])

        s1 = call(12, "semantic_search", {"query": "drift anchor", "n": 4})
        s2 = call(13, "semantic_search", {"query": "drift anchor", "n": 4})
        check("drift: semantic_search byte-stable after churn",
              s1 == s2 and "src=" in s1, s1[:100])

        # issue #125 output shaping on a controlled graph: 12 fresh files
        # (one hub class whose 14 methods call self.hub(), 11 one-fn
        # files) pin rescan's capped changed list, symbol_graph's counts /
        # "+N more" / node-cap marker / closest-match suggestions, and
        # context's defines rows — hermetic, both legs.
        (proj / "hub_wire.py").write_text(
            "class HubBench:\n"
            + "".join(
                f"    def m{i:02d}(self):\n        return self.hub()\n"
                for i in range(14)
            )
            + "    def hub(self):\n        return 1\n",
            encoding="utf-8",
            newline="\n",
        )
        for i in range(11):
            (proj / f"junk_wire_{i:02d}.py").write_text(
                f"def junk_wire_{i:02d}():\n    return {i}\n",
                encoding="utf-8",
                newline="\n",
            )
        (proj / "drift_a.py").unlink()
        out = call(14, "rescan", {})
        check("drift/125: rescan lists changed paths, capped (10 +N more)",
              "changed (12): " in out and "+2 more" in out
              and "hub_wire.py, junk_wire_00.py" in out
              and "deleted (1): drift_a.py" in out,
              out.splitlines()[-2:])
        sg = call(15, "symbol_graph", {"symbol": "hub", "depth": 1})
        check("drift/125: symbol_graph counts + '+N more' on a 14-caller hub",
              "hub_wire.py#hub" in sg and "callers: 14 (" in sg
              and "+6 more" in sg,
              sg[:160])
        sg2 = call(16, "symbol_graph", {"symbol": "hub", "depth": 2})
        check("drift/125: symbol_graph node-cap marker names the true total",
              "truncated at 13 of 15 nodes" in sg2,
              sg2[-160:])
        miss = call(17, "symbol_graph", {"symbol": "HubBenchh", "depth": 1})
        check("drift/125: total miss suggests closest matches",
              miss.startswith("no function matching 'HubBenchh'")
              and "Closest matches: hub" in miss,
              miss[:160])
        ctx = call(18, "context", {"path": "hub_wire.py", "depth": 1})
        check("drift/125: context defines rows in definition order",
              "defines: 15 func(s) — m00, m01" in ctx,
              [ln for ln in ctx.splitlines() if ln.startswith("defines")][:2])
    finally:
        srv.kill()
        if FAILS:
            print("--- drift server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))


def _degraded_scenario() -> None:
    """issue #180 CI-leg (4): degraded semantics end-to-end. A store is
    built by a real-mode server against a live loopback stub embed
    endpoint (deterministic per-text vectors), then served by a fresh
    server whose embedding backend is a dead loopback port: every
    vector-side surface must answer success-shaped with a truthful
    degraded reason carried through the MCP response — never a raw
    error (issues #115/#116 over the wire). Port 9 on loopback refuses
    instantly; no external network is touched. The build cannot use
    FAKE embeds: a fake-stamped store opened by the real-mode phase-2
    server trips the #220 mode gate at boot, force-re-embeds into the
    dead port and dies loudly — exactly the poisoning class #220 ends.
    """
    import hashlib
    import shutil
    from http.server import BaseHTTPRequestHandler, HTTPServer

    scratch = HERE / ".team_scratch" / "degraded_stdio"
    shutil.rmtree(scratch, ignore_errors=True)
    proj = scratch / "proj"
    proj.mkdir(parents=True)
    (proj / "degraded_wire.py").write_text(
        "def degraded_wire_marker():\n    return 'w'\n",
        encoding="utf-8", newline="\n",
    )
    (proj / "degraded_net.py").write_text(
        "def degraded_net_marker():\n    return 'n'\n",
        encoding="utf-8", newline="\n",
    )

    class _EmbedStub(BaseHTTPRequestHandler):
        """Ollama-wire stub: same text -> same vector (issue #220 style),
        dim 32 to match the scratch config below."""

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            rows = []
            for t in body["input"]:
                h = hashlib.sha256(f"stub:{t}".encode()).digest()
                rows.append([(b / 255.0) * 2 - 1 for b in h])
            out = json.dumps(
                {"model": body.get("model", ""), "embeddings": rows}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    stub = HTTPServer(("127.0.0.1", 0), _EmbedStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    def _cfg_text(url: str) -> str:
        return json.dumps({
            "root": str(proj.resolve()),
            "collection": "main",
            "state_dir": "default",
            "include_dirs": ["."],
            "extensions": [".py"],
            "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav", "node_modules"],
            "embed_url": url,
            "embed_dim": 32,
        })

    cfg = scratch / "degraded.neuronav.json"
    cfg.write_text(
        _cfg_text(f"http://127.0.0.1:{stub.server_address[1]}/api/embed"),
        encoding="utf-8", newline="\n",
    )

    # phase 1: build files + fns in REAL mode against the live stub
    env_build = {k: v for k, v in os.environ.items()
                 if k not in ("NEURONAV_CONFIG", "NEURONAV_EMBED_FAKE")}
    env_build["NEURONAV_CONFIG"] = str(cfg)
    srv0 = _spawn(env_build)
    try:
        send0, recv0 = srv0.send, srv0.recv
        send0({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "degraded-build", "version": "0"}}})
        recv0(1)
        send0({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "repo_map", "arguments": {"budget_tokens": 256}}})
        out = text_of(recv0(2)["result"])
        check("degraded: store built via live stub (2 files)",
              out.startswith(
                  f"you are here: {proj.resolve().as_posix()} — 2 files,"),
              out[:100])
    finally:
        srv0.kill()

    # phase 2: same store, backend dead, still real mode — degraded answers
    cfg.write_text(_cfg_text("http://127.0.0.1:9/api/embed"),
                   encoding="utf-8", newline="\n")
    env_dead = {k: v for k, v in env_build.items() if k != "NEURONAV_EMBED_FAKE"}
    srv = _spawn(env_dead)
    send, recv = srv.send, srv.recv

    def call(mid: int, name: str, args: dict) -> str:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        return text_of(recv(mid)["result"])

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "degraded", "version": "0"}}})
        check("degraded: server boots with a dead backend",
              "result" in recv(1), "")

        out = call(2, "semantic_search", {"query": "degraded wire marker", "n": 4})
        rows = [ln for ln in out.splitlines() if "src=" in ln]
        check("degraded: semantic_search carries the truthful reason",
              "degraded: BM25F-only (embedding backend unreachable" in out,
              out[:180])
        check("degraded: BM25F side still serves hits",
              len(rows) >= 1
              and all(re.search(r"src=bm25(?:\s|$)", ln) for ln in rows),
              out[:180])

        out = call(3, "find_functions", {"query": "degraded_wire_marker", "n": 4})
        check("degraded: find_functions answers lexical fallback, never raw",
              out.startswith("degraded: ") and "lexical fallback" in out
              and "degraded_wire.py#degraded_wire_marker" in out,
              out[:180])

        out = call(4, "explore", {"query": "degraded wire marker", "n": 3})
        check("degraded: explore still slices source with a degraded marker",
              "degraded" in out.lower() and "def degraded_wire_marker" in out
              and "\t" in out,
              out[:180])
    finally:
        srv.kill()
        if FAILS:
            print("--- degraded server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))


if __name__ == "__main__":
    main()

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
