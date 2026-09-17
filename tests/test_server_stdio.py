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
# server-under-test selectable via argv (test_bootgate precedent):
# default = this checkout; an argv checkout is how pre-fix FAIL
# evidence runs against unmodified main
SERVER_DIR = (
    Path(sys.argv[1]).resolve() if len(sys.argv) > 1
    else HERE
)
sys.path.insert(0, str(SERVER_DIR))  # the checkout under test wins

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

# issue #286 test-pace knob: NEURONAV_STAT_TTL_S shortens the stat TTL
# for this suite process and every spawned server child (children
# inherit the export), shrinking the six TTL_WAIT legs. setdefault — an
# explicit export still wins. Both sides of the knob are pinned by the
# child probes below TTL_WAIT.
os.environ.setdefault("NEURONAV_STAT_TTL_S", "0.5")

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

# the stat gate's TTL cache is real (3s default, 0.5s here via the #286
# knob) — drift legs wait one window out so the next read tool re-walks
# (test_autorescan's e2e precedent)
TTL_WAIT = nav.STAT_TTL_S + 0.5

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

# pin both sides of the #286 knob in fresh children: a bare nav import
# keeps the 3.0 default; the env override is honored end-to-end
for _ttl_env, _want in ((None, "3.0"), ("0.5", "0.5")):
    _env = {k: v for k, v in os.environ.items() if k != "NEURONAV_STAT_TTL_S"}
    if _ttl_env:
        _env["NEURONAV_STAT_TTL_S"] = _ttl_env
    _pin = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import nav; print(nav.STAT_TTL_S)"],
        capture_output=True, text=True, env=_env, cwd=str(HERE),
    )
    check(
        f"ttl knob: nav.STAT_TTL_S=={_want} "
        + ("with" if _ttl_env else "without") + " NEURONAV_STAT_TTL_S",
        _pin.stdout.strip() == _want,
        _pin.stdout.strip() or _pin.stderr[-200:],
    )


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


def _spawn(env: dict[str, str], cwd: Path | None = None) -> SimpleNamespace:
    """Start one stdio server + daemon drain threads; returns
    .proc/.send/.recv/.kill/.stderr_lines.

    cwd defaults to the checkout (the classic boot shape); the #240
    fresh-folder scenario passes a foreign repo so pure defaults bind
    to IT, not the neuronav checkout.

    The server logs (startup + ollama HTTP) can outgrow the stderr pipe
    buffer and deadlock it if nobody drains — keep daemon readers
    (lambda-wrapped targets give the extractor a visible call site).
    recv enforces a REAL deadline (a blocked readline would otherwise
    ignore the timeout)."""
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(SERVER_DIR / "server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=cwd or SERVER_DIR,
        env=env,
    )
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []  # #253: stdout-purity pin source
    _stdout_q: "queue.Queue[str]" = queue.Queue()
    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    threading.Thread(target=lambda: _drain_stderr(), daemon=True).start()

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line)
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
        proc=proc, send=send, recv=recv, kill=kill,
        stderr_lines=stderr_lines, stdout_lines=stdout_lines,
    )


TOOL_NAMES = (
    "explore", "repo_map", "semantic_search", "find_functions", "search_text",
    "symbol_graph", "impact", "dead_code", "duplicates", "clusters",
    "crosstalk",
    "arch_check", "context", "visualize", "rescan", "memory",
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
        # issue #237: initialize must carry the agent instructions
        # (orientation workflow: repo_map -> explore -> lookups -> memory)
        instr = init.get("result", {}).get("instructions", "")
        check(
            "initialize carries agent instructions (issue #237)",
            bool(instr) and all(k in instr for k in
                                ("repo_map", "explore", "semantic_search", "memory")),
            repr(instr[:120]),
        )

        # issue #300: the workflow must name every advertised tool —
        # it had drifted to 11 of 16 (search_text, context, clusters,
        # crosstalk, arch_check missing from the agent's manual)
        check(
            "instructions name every advertised tool (issue #300)",
            all(t in instr for t in TOOL_NAMES),
            f"missing: {sorted(t for t in TOOL_NAMES if t not in instr)}",
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
        # issue #70: arch_check joins the surface — same deliberate pin
        # (advertisement + readOnlyHint; the call legs run further down,
        # once the serving index has answered something real)
        check(
            "tools/list advertises arch_check",
            "arch_check" in names,
            f"tools={names}",
        )
        ac_ann = next(
            (t.get("annotations") for t in tools if t["name"] == "arch_check"), None
        )
        check(
            "arch_check: readOnlyHint set",
            bool(ac_ann and ac_ann.get("readOnlyHint") is True),
            json.dumps(ac_ann),
        )
        # issue #131: universal mount — every tool gains the optional dir
        # param (empty = boot config's repo); the suite pins the surface,
        # so it pins the new parameter on all 16 tools
        check(
            "tools/list advertises exactly the 16 tools",
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
        # issue #266: the default cap is part of the tool's honesty
        # contract — n=40 starved the review tier silently; the clamp
        # max (100) is the new default
        dc_schema = next(
            (t.get("inputSchema") or {} for t in tools if t["name"] == "dead_code"),
            {},
        )
        check(
            "dead_code: inputSchema advertises default n=100 (issue #266)",
            dc_schema.get("properties", {}).get("n", {}).get("default") == 100,
            str(dc_schema.get("properties", {}).get("n")),
        )
        # ---- issue #253: MCP spec/best-practice compliance pins ----
        # The audit (.team_scratch/mcp_audit.md, untracked) judged
        # most of the surface compliant; these pins hold the
        # load-bearing parts against SDK drift: negotiated
        # revision, capabilities honesty, ping, per-tool description
        # floor, inputSchema shape, annotation hints, and the error
        # contract (bad input -> isError CallToolResult naming the
        # field, server stays up for the next call).
        ires = init.get("result", {})
        check(
            "negotiated protocolVersion echoes 2024-11-05",
            ires.get("protocolVersion") == "2024-11-05",
            str(ires.get("protocolVersion")),
        )
        caps = ires.get("capabilities") or {}
        check(
            "capabilities: tools declared, listChanged false (static set)",
            caps.get("tools", {}).get("listChanged") is False,
            json.dumps(caps),
        )
        check(
            "capabilities: no logging/progress/completions over-claim",
            not ({"logging", "progress", "completions"} & set(caps)),
            json.dumps(sorted(caps)),
        )
        send({"jsonrpc": "2.0", "id": 900, "method": "ping"})
        check("ping answered with an empty result",
              recv(900).get("result") == {}, "")
        for t in tools:
            desc = t.get("description") or ""
            check(
                f"{t['name']}: description floor (names the job)",
                len(desc) >= 120 and "\n" in desc
                and bool(desc.splitlines()[0].strip()),
                f"len={len(desc)} head={desc[:60]!r}",
            )
        for t in tools:
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            check(
                f"{t['name']}: inputSchema well-formed",
                schema.get("type") == "object" and bool(props)
                and set(required) <= set(props)
                and all("type" in p or "anyOf" in p
                       for p in props.values()),
                json.dumps(schema)[:200],
            )
        for t in tools:
            if t["name"] in ("memory", "rescan"):
                continue
            ann = t.get("annotations") or {}
            check(
                f"{t['name']}: readOnlyHint set",
                ann.get("readOnlyHint") is True,
                json.dumps(ann),
            )
        mem_ann = next(t.get("annotations") or {}
                       for t in tools if t["name"] == "memory")
        check(
            "memory: destructiveHint true, not read-only (issue #253)",
            mem_ann.get("destructiveHint") is True
            and not mem_ann.get("readOnlyHint"),
            json.dumps(mem_ann),
        )
        res_ann = next(t.get("annotations") or {}
                       for t in tools if t["name"] == "rescan")
        check(
            "rescan: additive+idempotent, not read-only (issue #253)",
            res_ann.get("destructiveHint") is False
            and res_ann.get("idempotentHint") is True
            and not res_ann.get("readOnlyHint"),
            json.dumps(res_ann),
        )

        def _bad_value(prop: dict):
            kind = prop.get("type")
            if kind == "string":
                return 4242
            if kind in ("integer", "number"):
                return "not-a-number"
            if kind == "boolean":
                return "not-a-bool"
            for branch in prop.get("anyOf", []):
                if branch.get("type") in ("integer", "number"):
                    return "not-a-number"
                if branch.get("type") == "boolean":
                    return "not-a-bool"
            return 4242

        pid = 910
        for t in sorted(tools, key=lambda x: x["name"]):
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            if required:
                arguments, expect = {}, required[0]
            else:
                pname = next(
                    (p for p in sorted(props) if p != "dir"
                     and ("type" in props[p] or "anyOf" in props[p])),
                    "dir",
                )
                arguments, expect = {pname: _bad_value(props[pname])}, pname
            pid += 1
            send({"jsonrpc": "2.0", "id": pid, "method": "tools/call",
                  "params": {"name": t["name"], "arguments": arguments}})
            bad = recv(pid)
            res = bad.get("result") or {}
            err_text = " ".join(
                b.get("text", "") for b in res.get("content", [])
                if b.get("type") == "text"
            )
            check(
                f"{t['name']}: bad input -> isError naming '{expect}'",
                res.get("isError") is True and expect in err_text,
                json.dumps(bad)[:200],
            )
        pid += 1
        send({"jsonrpc": "2.0", "id": pid, "method": "tools/call",
              "params": {"name": "no_such_tool", "arguments": {}}})
        unk = recv(pid)
        check(
            "unknown tool -> isError result (shipped SDK contract)",
            (unk.get("result") or {}).get("isError") is True,
            json.dumps(unk)[:200],
        )
        pid += 1
        send({"jsonrpc": "2.0", "id": pid, "method": "tools/call",
              "params": {"name": "memory",
                         "arguments": {"verb": "frobnicate"}}})
        mem_bad = recv(pid)
        mres = mem_bad.get("result") or {}
        mem_txt = " ".join(
            b.get("text", "") for b in mres.get("content", [])
            if b.get("type") == "text"
        )
        check(
            "memory: bad verb -> isError naming the verb law",
            mres.get("isError") is True and "verb" in mem_txt.lower(),
            mem_txt[:200],
        )
        # stdout purity: every byte the server ever wrote to stdout
        # must be a JSON-RPC frame (stdio transport law)
        time.sleep(0.3)
        nonframes = []
        for ln in list(srv.stdout_lines):
            try:
                msg = json.loads(ln)
            except ValueError:
                nonframes.append(ln)
                continue
            if not isinstance(msg, dict) or "jsonrpc" not in msg:
                nonframes.append(ln)
        check(
            "stdout carries only JSON-RPC frames (issue #253)",
            not nonframes,
            f"non-frames: {nonframes[:3]!r}",
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

        # issue #70: arch_check call legs, riding the serving project's
        # own partition. (1) no rules file in the state dir -> the
        # how-to-write-one answer, never an error. (2) forbid rules
        # planted over a REAL cross-wired pair — taken from the
        # crosstalk tool's own answer, so the leg is config-agnostic —
        # must be caught; the rules file lands in the gitignored state
        # dir, which is excluded from walks, so the stat gate never
        # rescans on it. (3) a typo'd cluster name is loud. Removed
        # after, restoring the no-rules answer.
        send(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "arch_check", "arguments": {}},
            }
        )
        ac = text_of(recv(21)["result"])
        check(
            "arch_check: absent rules file answers how-to, not error",
            "no arch rules configured" in ac and "arch-rules.json" in ac
            and '"forbid"' in ac,
            ac[:120],
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {"name": "crosstalk", "arguments": {}},
            }
        )
        ct = text_of(recv(22)["result"])
        mpair = re.search(r"^  (.+?) <-> (.+?): (\d+) edges", ct, re.M)
        mpath = re.search(r"write (\S*arch-rules\.json)", ac)
        if not (mpair and mpath):
            print("SKIP arch_check planted-violation leg (no cross-wired pair)")
        else:
            la, lb, pair_n = mpair.group(1), mpair.group(2), int(mpair.group(3))
            rules_file = Path(mpath.group(1))
            rules_file.write_text(
                json.dumps(
                    {
                        "rules": [
                            {"id": "planted-ab", "kind": "forbid", "from": la, "to": lb},
                            {"id": "planted-ba", "kind": "forbid", "from": lb, "to": la},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            try:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 23,
                        "method": "tools/call",
                        "params": {"name": "arch_check", "arguments": {}},
                    }
                )
                av = text_of(recv(23)["result"])
                mv = re.search(r"arch check: 2 rule\(s\), (\d+) violation", av)
                wires = [int(n) for n in re.findall(r"(\d+) wires", av)]
                check(
                    "arch_check: planted violation over a real pair is caught",
                    mv and int(mv.group(1)) >= 1 and wires and sum(wires) <= pair_n
                    and ("planted-ab" in av or "planted-ba" in av),
                    av[:160],
                )
                rules_file.write_text(
                    json.dumps(
                        {"rules": [
                            {"id": "typo", "kind": "forbid",
                             "from": "No-Such-Subsystem", "to": lb},
                        ]}
                    ),
                    encoding="utf-8",
                )
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 24,
                        "method": "tools/call",
                        "params": {"name": "arch_check", "arguments": {}},
                    }
                )
                at = text_of(recv(24)["result"])
                check(
                    "arch_check: typo'd rule is loud over stdio",
                    "rule config error" in at and "No-Such-Subsystem" in at
                    and "known clusters" in at,
                    at[:160],
                )
            finally:
                rules_file.unlink()
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 25,
                    "method": "tools/call",
                    "params": {"name": "arch_check", "arguments": {}},
                }
            )
            ac2 = text_of(recv(25)["result"])
            check(
                "arch_check: cleanup restores the no-rules answer",
                "no arch rules configured" in ac2,
                ac2[:120],
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

        # issue #280: impact rides the wire on the self-index's
        # most-called fn (config-agnostic) — full totals + per-hop
        # histogram under the #125 line law, byte-stable across calls
        send({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
              "params": {"name": "impact",
                         "arguments": {"symbol": HUBFN}}})
        imp = text_of(recv(12)["result"])
        check("impact: full totals + depth histogram over the wire (issue #280)",
              bool(re.match(r"impact of .+ \(.*\): callers — what breaks$",
                            imp.splitlines()[0]))
              and bool(re.search(r"total: \d+ within [1-8] hops", imp))
              and bool(re.search(r"depth 1: \d+ \(", imp)),
              imp.splitlines()[:3])
        send({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
              "params": {"name": "impact",
                         "arguments": {"symbol": HUBFN}}})
        check("impact: byte-stable across calls",
              text_of(recv(13)["result"]) == imp,
              len(imp))

        ex_schema = next(
            (t.get("inputSchema") or {} for t in tools if t["name"] == "explore"),
            {},
        )
        check("explore: orientation advertised optional boolean (issue #125)",
              ex_schema.get("properties", {}).get("orientation", {}).get("type")
              == "boolean"
              and "orientation" not in ex_schema.get("required", []),
              json.dumps(ex_schema)[:200])
        check("explore: query not required — anchor alone pages (issue #276)",
              "query" not in ex_schema.get("required", []),
              json.dumps(ex_schema.get("required", [])))
        send({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
              "params": {"name": "explore",
                         "arguments": {"query": "cluster labeling", "n": 3,
                                       "orientation": False}}})
        ex = text_of(recv(11)["result"])
        check("explore: orientation=False skips the preamble (issue #125)",
              "== repo map ==" not in ex and "== clusters ==" not in ex,
              ex[:160])
        m = re.search(r'anchor="([^"]+)"', ex)
        if m:
            send({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                  "params": {"name": "explore",
                             "arguments": {"anchor": m.group(1)}}})
            cont = text_of(recv(14)["result"])
            check("explore: anchor-only call pages without query (issue #276)",
                  bool(re.search(r"^\d+\t", cont, re.M)) or cont.startswith("**")
                  or "anchor" in cont[:60],
                  cont[:160])
        else:
            print(f"SKIP: explore slice had no continuation anchor on this "
                  f"index shape (issue #97) — anchor-only leg not exercisable")
        send({"jsonrpc": "2.0", "id": 15, "method": "tools/call",
              "params": {"name": "explore", "arguments": {}}})
        empty = text_of(recv(15)["result"])
        check("explore: neither query nor anchor -> guidance not error",
              "one of the two is required" in empty,
              empty[:160])

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

        # issue #240: the fresh-folder onboarding repro — TS-only repo,
        # pure defaults, guidance + recovery + embedder probe
        _fresh_folder_scenario()

        # issue #67: Serena-style project memories — the mutating tool #2
        _memory_scenario()

        # issues #266 + #268: truncation/skip honesty on dead_code and
        # duplicates output
        _truncation_scenario()

        # issue #300: #125 honesty sweep — clusters listing marker +
        # clamp footers naming the applied bound
        _sweep300_scenario()

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


def _truncation_scenario() -> None:
    """issues #266 + #268: honest truncation and skip counts on the
    dead_code/duplicates tool output. A scratch corpus with known tier
    totals (7 likely + 2 review dead funcs) pins: default n serves every
    row with no footer, a small n ends in a footer naming the per-tier
    shown/total counts (at HEAD the likely-first ordering starved the
    review tier silently), and a routed second corpus pins duplicates —
    the identical thin-delegate pair (null guard + one forwarding call)
    drops with an honest skip footer while the genuine duplicated-logic
    pair stays listed."""
    import shutil

    scratch = HERE / ".team_scratch" / "honesty266"
    shutil.rmtree(scratch, ignore_errors=True)

    def mkproj(name: str, files: dict[str, str]) -> Path:
        p = scratch / name
        p.mkdir(parents=True)
        for fn, body in files.items():
            (p / fn).write_text(body, encoding="utf-8", newline="\n")
        return p

    dead_corp = mkproj("dead", {
        "big1.py": "".join(
            f"def d{i}():\n    return {100 + i}\n\n" for i in range(7)
        ),
        "big2.py": (
            'def main():\n    t = Hub()\n    t = getattr(t, "sig")\n    return t\n\n'
            "def r00():\n    return 200\n"
        ),
    })
    wrap = (
        "def _wrap(v):\n    if v is None:\n        return None\n"
        "    return _shared(v)\n"
    )
    twin = "def twin():\n    alpha = 10\n    beta = 20\n    return alpha + beta\n"
    dup_corp = mkproj("dups", {
        "w1.py": wrap, "w2.py": wrap, "t1.py": twin, "t2.py": twin,
    })
    wrap_only = mkproj("wraps", {"w1.py": wrap, "w2.py": wrap})

    cfg = scratch / "honesty.neuronav.json"
    cfg.write_text(json.dumps({
        "root": str(dead_corp.resolve()),
        "collection": "honesty",
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

    def call(mid: int, name: str, args: dict) -> dict:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        return recv(mid)["result"]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "honesty", "version": "0"}}})
        check("honesty: initialize handshake", "result" in recv(1), "")

        # default n: every candidate listed (9 total), no footer
        out = text_of(call(2, "dead_code", {}))
        rows = [ln for ln in out.splitlines() if ln.startswith("[")]
        check("honesty: dead_code default serves full corpus header",
              out.splitlines()[0]
              == "dead-code candidates: 9 total  (likely: 7, review: 2)",
              out.splitlines()[:1])
        check("honesty: dead_code default lists every row incl. review",
              len(rows) == 9 and sum(1 for r in rows if "[review]" in r) == 2,
              f"rows={len(rows)}")
        check("honesty: no footer when nothing is truncated",
              "truncated" not in out, out.splitlines()[-1:])

        # small n: likely-first ordering starves review — the footer
        # must name both tiers' shown/total counts (issue #266)
        out5 = text_of(call(3, "dead_code", {"n": 5}))
        rows5 = [ln for ln in out5.splitlines() if ln.startswith("[")]
        check("honesty: dead_code n=5 lists 5 rows, all likely",
              len(rows5) == 5 and all("[likely]" in r for r in rows5),
              f"rows={len(rows5)}")
        check("honesty: dead_code n=5 footer names tier counts (issue #266)",
              out5.splitlines()[-1]
              == "… truncated at 5 rows: showing 5 of 7 likely + 0 of 2 review"
                 " — pass n= for the rest",
              out5.splitlines()[-1:])

        # routed dups corpus: first contact onboards, then the report
        on = text_of(call(4, "duplicates", {"dir": str(dup_corp)}))
        check("honesty: dups corpus onboards on first contact",
              on.startswith(f"onboarded {dup_corp.resolve().as_posix()} — index built:"),
              on[:80])
        outd = text_of(call(5, "duplicates", {"dir": str(dup_corp)}))
        check("honesty: duplicates lists the genuine pair only (issue #268)",
              outd.splitlines()[0] == "1 duplicate group(s):"
              and "t1.py#twin" in outd and "t2.py#twin" in outd
              and "_wrap" not in outd,
              outd)
        check("honesty: duplicates footer counts skipped delegates (issue #268)",
              outd.splitlines()[-1]
              == "1 pure-delegate group(s) skipped — thin delegates, not"
                 " duplicated logic",
              outd.splitlines()[-1:])

        # all-delegate corpus: the clean bill must still count the skips
        call(6, "duplicates", {"dir": str(wrap_only)})
        outw = text_of(call(7, "duplicates", {"dir": str(wrap_only)}))
        check("honesty: no-dups line names the skipped delegates (issue #268)",
              outw == "no exact duplicates found (2 files with functions"
                      " scanned; 1 pure-delegate group(s) skipped — thin"
                      " delegates, not duplicated logic)",
              outw)
    finally:
        srv.kill()
        if FAILS:
            print("--- honesty server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))


def _sweep300_scenario() -> None:
    """issue #300: #125 honesty sweep on the server views. A 40-chain
    import corpus (deterministic ~38 communities under FAKE embeds —
    the self-index resolution sweep's shape) forces the clusters
    listing past its 30 cap: the answer must carry the +N more marker,
    not stop silently. Same law on the clamps: repo_map's budget and
    semantic_search's n announce the bound they applied instead of
    quietly serving 8192/25."""
    import shutil

    scratch = HERE / ".team_scratch" / "sweep300"
    shutil.rmtree(scratch, ignore_errors=True)
    corp = scratch / "chains"
    for g in range(40):
        d = corp / f"grp{g:02d}"
        d.mkdir(parents=True)
        (d / "core.py").write_text(
            f"def grp{g:02d}_core_fn(mag_{g}):\n    return mag_{g} * {g}\n",
            encoding="utf-8", newline="\n",
        )
        (d / "mid.py").write_text(
            f"from grp{g:02d}.core import grp{g:02d}_core_fn\n"
            f"def grp{g:02d}_mid_fn(x_{g}):\n"
            f"    return grp{g:02d}_core_fn(x_{g}) + 1\n",
            encoding="utf-8", newline="\n",
        )
        (d / "leaf.py").write_text(
            f"from grp{g:02d}.mid import grp{g:02d}_mid_fn\n"
            f"def grp{g:02d}_leaf_fn(y_{g}):\n"
            f"    return grp{g:02d}_mid_fn(y_{g}) * 2\n",
            encoding="utf-8", newline="\n",
        )
    cfg = scratch / "sweep300.neuronav.json"
    cfg.write_text(json.dumps({
        "root": str(corp.resolve()),
        "collection": "sweep300",
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

    def call(mid: int, name: str, args: dict) -> dict:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        return recv(mid)["result"]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "sweep300", "version": "0"}}})
        check("sweep300: initialize handshake", "result" in recv(1), "")

        out = text_of(call(2, "clusters", {}))
        header = out.splitlines()[0] if out else ""
        check("sweep300: clusters header carries the true total",
              re.fullmatch(r"\d+ cluster\(s\):", header) is not None,
              header)
        n_total = int(header.split()[0]) if header[:1].isdigit() else 0
        check("sweep300: fixture exceeds the listing cap",
              n_total > 30, f"{n_total} clusters")
        check(
            "sweep300: past-cap clusters answer with a +N more marker (issue #300)",
            re.search(
                rf"… \+{n_total - 30} more cluster\(s\) — listing capped at 30",
                out,
            ) is not None,
            out.splitlines()[-1:],
        )

        # clamp footers name the applied bound (#125) instead of silence
        rm = text_of(call(3, "repo_map", {"budget_tokens": 999999}))
        check("sweep300: repo_map budget clamp footer names the bound",
              "(budget clamped to 8192 — legal range 256..8192)" in rm,
              rm.splitlines()[-1:])
        ss = text_of(call(4, "semantic_search",
                          {"query": "core fn", "n": 99}))
        check("sweep300: semantic_search n clamp footer names the bound",
              "(n clamped to 25 — legal range 1..25)" in ss,
              ss.splitlines()[-1:])
        rm2 = text_of(call(5, "repo_map", {"budget_tokens": 512}))
        check("sweep300: in-range budget serves no footer",
              "budget clamped" not in rm2, rm2.splitlines()[-1:])
    finally:
        srv.kill()
        if FAILS:
            print("--- sweep300 server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))


def _fresh_folder_scenario() -> None:
    """issue #240 acceptance, post-#245 shape: the owner's live repro
    (opencode-mobile — TS-only, no .neuronav, pure defaults, v0.1.5 died
    pre-handshake). A TS-only repo now boots STRUCTURAL (the ts
    extractor landed, #245), so that half pins the structural boot:
    initialize answers, tools/list works, repo_map serves the real map.
    The degraded-boot guidance UX lives on for raw-text-only repos
    (.md — JS left that club in #277, Rust in #284): an md-only sibling pins the
    contract verbatim — read tools answer first-call guidance naming
    the scanned extensions + the paste-ready config for the suffixes
    actually on disk, the explicit rescan TOOL stays loud (#41 law),
    and the guidance's exact fix (onboard.py init --preset ts)
    recovers the session in place. Then the embedder-probe contract
    (no FAKE): dead endpoint + empty store aborts pre-handshake naming
    the ollama pull fix; a warm store serves degraded from the index."""
    import shutil
    import socket

    scratch = HERE / ".team_scratch" / "fresh240"
    shutil.rmtree(scratch, ignore_errors=True)
    repo = scratch / "opencodeish"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.ts").write_text(
        "export function greet(): string {\n  return 'hi';\n}\n",
        encoding="utf-8", newline="\n")
    (repo / "src" / "util.tsx").write_text(
        "export const answer = 42;\n", encoding="utf-8", newline="\n")
    (repo / "package.json").write_text(
        '{\n  "name": "opencodeish"\n}\n', encoding="utf-8", newline="\n")

    base = {k: v for k, v in os.environ.items()
            if k not in ("NEURONAV_CONFIG", "NEURONAV_EMBED_FAKE")}

    def call(mid: int, name: str, args: dict) -> dict:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        return recv(mid)["result"]

    # --- ts repo: pure-defaults boot is structural now (#245) ---
    env = dict(base)
    env["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(env, cwd=repo)
    send, recv = srv.send, srv.recv
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fresh", "version": "0"}}})
        init = recv(1)
        check("fresh240: TS-only pure-defaults boot answers initialize "
              "(server stays up — the v0.1.5 fatal)",
              "result" in init and srv.proc.poll() is None, "")
        check("fresh240: instructions name the presets (issue #240)",
              "onboard.py init --preset" in init["result"]["instructions"],
              init["result"]["instructions"][-120:])
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in recv(2)["result"]["tools"]]
        check("fresh240: tools/list works on the structural ts boot",
              "repo_map" in names and "rescan" in names, "")

        g = text_of(call(3, "repo_map", {"budget_tokens": 256}))
        check("fresh240: ts boot serves the structural map, not guidance",
              g.startswith(f"you are here: {repo.resolve().as_posix()}")
              and "main.ts" in g and "EMPTY INDEX" not in g, g[:160])
        g2 = text_of(call(4, "semantic_search", {"query": "greet"}))
        check("fresh240: ts boot semantic_search serves, not guidance",
              "main.ts" in g2 and "EMPTY INDEX" not in g2, g2[:160])

        # the structural boot indexes on its own: explicit rescan is a
        # normal success, no degraded mode to protect (#41 applies to
        # the js-only leg below)
        r = call(5, "rescan", {})
        check("fresh240: ts boot rescan succeeds (structural, #245)",
              not bool(r.get("isError")) and "files" in text_of(r),
              text_of(r)[:200])
    finally:
        srv.kill()
        if FAILS:
            print("--- fresh240 ts server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))

    # --- js-only sibling: structural now too (issue #277 — the js
    #     extractor landed; the registry default walk covers .js) ---
    jsrepo = scratch / "jsonly"
    (jsrepo / "src").mkdir(parents=True)
    (jsrepo / "src" / "main.js").write_text(
        "export function jgreet() {\n  return 'yo';\n}\n",
        encoding="utf-8", newline="\n")
    (jsrepo / "package.json").write_text(
        '{\n  "name": "jsonly"\n}\n', encoding="utf-8", newline="\n")

    jsenv = dict(base)
    jsenv["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(jsenv, cwd=jsrepo)
    send, recv = srv.send, srv.recv
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fresh", "version": "0"}}})
        init = recv(1)
        check("fresh240: js-only pure-defaults boot answers initialize "
              "(server stays up — the v0.1.5 fatal)",
              "result" in init and srv.proc.poll() is None, "")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in recv(2)["result"]["tools"]]
        check("fresh240: tools/list works on the structural js boot",
              "repo_map" in names and "rescan" in names, "")

        g = text_of(call(3, "repo_map", {"budget_tokens": 256}))
        check("fresh240: js boot serves the structural map, not guidance "
              "(issue #277)",
              g.startswith(f"you are here: {jsrepo.resolve().as_posix()}")
              and "main.js" in g and "EMPTY INDEX" not in g, g[:160])
        g2 = text_of(call(4, "semantic_search", {"query": "jgreet"}))
        check("fresh240: js boot semantic_search serves, not guidance",
              "main.js" in g2 and "EMPTY INDEX" not in g2, g2[:160])

        r = call(5, "rescan", {})
        check("fresh240: js boot rescan succeeds (structural, #277)",
              not bool(r.get("isError")) and "files" in text_of(r),
              text_of(r)[:200])
    finally:
        srv.kill()
        if FAILS:
            print("--- fresh240 js server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))

    # --- rust-only sibling: structural too (issue #284 — the rust
    #     extractor is wired into the registry default walk for .rs) ---
    rsrepo = scratch / "rustonly"
    (rsrepo / "src").mkdir(parents=True)
    (rsrepo / "src" / "lib.rs").write_text(
        "pub fn rgreet() -> &'static str {\n  \"ahoy\"\n}\n\n"
        "pub fn rcaller() -> &'static str {\n  rgreet()\n}\n",
        encoding="utf-8", newline="\n")

    rsenv = dict(base)
    rsenv["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(rsenv, cwd=rsrepo)
    send, recv = srv.send, srv.recv
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fresh", "version": "0"}}})
        init = recv(1)
        check("fresh240: rust-only pure-defaults boot answers initialize "
              "(server stays up — the v0.1.5 fatal)",
              "result" in init and srv.proc.poll() is None, "")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in recv(2)["result"]["tools"]]
        check("fresh240: tools/list works on the structural rust boot",
              "repo_map" in names and "rescan" in names, "")

        g = text_of(call(3, "repo_map", {"budget_tokens": 256}))
        check("fresh240: rust boot serves the structural map, not guidance "
              "(issue #284)",
              g.startswith(f"you are here: {rsrepo.resolve().as_posix()}")
              and "lib.rs" in g and "EMPTY INDEX" not in g, g[:160])
        g2 = text_of(call(4, "semantic_search", {"query": "rgreet"}))
        check("fresh240: rust boot semantic_search serves, not guidance",
              "lib.rs" in g2 and "EMPTY INDEX" not in g2, g2[:160])

        r = call(5, "rescan", {})
        check("fresh240: rust boot rescan succeeds (structural, #284)",
              not bool(r.get("isError")) and "files" in text_of(r),
              text_of(r)[:200])
    finally:
        srv.kill()
        if FAILS:
            print("--- fresh240 rust server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))

    # --- md-only sibling: the raw-text degraded-boot UX (issue #240,
    #     preserved verbatim for the languages without an extractor —
    #     .md never registers; JS left this club in #277, Rust in #284) ---
    mdrepo = scratch / "mdonly"
    (mdrepo / "src").mkdir(parents=True)
    (mdrepo / "src" / "main.md").write_text(
        "# notes\n\nyo\n", encoding="utf-8", newline="\n")
    (mdrepo / "package.json").write_text(
        '{\n  "name": "mdonly"\n}\n', encoding="utf-8", newline="\n")

    mdenv = dict(base)
    mdenv["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(mdenv, cwd=mdrepo)
    send, recv = srv.send, srv.recv
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fresh", "version": "0"}}})
        init = recv(1)
        check("fresh240: md-only pure-defaults boot answers initialize "
              "(server stays up — the v0.1.5 fatal)",
              "result" in init and srv.proc.poll() is None, "")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in recv(2)["result"]["tools"]]
        check("fresh240: tools/list works while degraded",
              "repo_map" in names and "rescan" in names, "")

        g = text_of(call(3, "repo_map", {"budget_tokens": 256}))
        check("fresh240: repo_map answers guidance naming .md",
              "EMPTY INDEX" in g and ".md" in g, g[:160])
        check("fresh240: guidance carries the paste-ready config "
              "(state_dir opt-in, issue #91)",
              '"state_dir": "default"' in g and "config.json" in g, g)
        check("fresh240: guidance names the preset one-liner "
              "(the ts preset covers .md — ts hints first)",
              "--preset ts" in g, g)
        check("fresh240: guidance names the raw-text degradation",
              "raw text" in g and "find_functions" in g, g)
        check("fresh240: guidance names extensions actually scanned",
              "extensions scanned:" in g, g[:300])
        g2 = text_of(call(4, "semantic_search", {"query": "yo"}))
        check("fresh240: semantic_search answers the same guidance",
              "EMPTY INDEX" in g2 and ".md" in g2, g2[:120])

        # issue #41 law: degraded boot must not neuter the explicit
        # rescan tool — it errors loudly (names the 0-file walk), never
        # echoes guidance
        r = call(5, "rescan", {})
        check("fresh240: degraded rescan is a loud error (#41 unchanged)",
              bool(r.get("isError")) and "0 files" in text_of(r)
              and "EMPTY INDEX" not in text_of(r), text_of(r)[:200])

        # the guidance's exact fix, applied in-session via the CLI
        subprocess.run(
            [sys.executable, "-X", "utf8", str(HERE / "onboard.py"),
             "init", "--project", str(mdrepo), "--preset", "ts"],
            cwd=mdrepo, env=mdenv, capture_output=True, text=True, check=True)
        cfg = json.loads((mdrepo / ".neuronav" / "config.json")
                         .read_text(encoding="utf-8"))
        check("fresh240: preset ts scaffold pins extensions + state_dir",
              cfg["extensions"] == [".ts", ".tsx", ".mts", ".cts",
                                    ".js", ".jsx", ".mjs", ".cjs",
                                    ".json", ".md"]
              and cfg["state_dir"] == "default" and cfg["root"] == ".",
              json.dumps(cfg))   # #298: scaffold pins portable root
        rec = text_of(call(6, "rescan", {}))
        check("fresh240: in-session recovery on the next rescan",
              "rebound the boot" in rec and "files 2/" in rec, rec[:160])
        time.sleep(0.3)  # stderr drain settle
        err = "".join(srv.stderr_lines)
        check("fresh240: boot banner names the raw-text degradation",
              "no structural extractor for" in err and ".md" in err,
              "\n".join(srv.stderr_lines[-6:]))
        g3 = text_of(call(7, "semantic_search", {"query": "yo"}))
        check("fresh240: post-recovery search finds the md file (raw)",
              "main.md" in g3, g3[:200])
        g4 = text_of(call(8, "repo_map", {"budget_tokens": 256}))
        check("fresh240: post-recovery repo_map serves with the "
              "structural degradation visible",
              g4.startswith(f"you are here: {mdrepo.resolve().as_posix()}")
              and "0 files" in g4, g4[:120])
    finally:
        srv.kill()
        if FAILS:
            print("--- fresh240 md server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))

    # ---- embedder probe (issue #240 pin 4): no FAKE in these legs ----
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    warm = scratch / "warm"
    warm.mkdir(parents=True)
    (warm / "a.py").write_text("def aa():\n    return 1\n",
                               encoding="utf-8", newline="\n")
    (warm / "b.py").write_text("def bb():\n    return 2\n",
                               encoding="utf-8", newline="\n")
    probe_cfg = scratch / "deadport.json"
    probe_cfg.write_text(json.dumps({
        "root": str(warm.resolve()), "collection": "main",
        "state_dir": "default", "include_dirs": ["."],
        "extensions": [".py"],
        "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav",
                         "node_modules"],
        "embed_url": f"http://127.0.0.1:{dead_port}/api/embed",
    }), encoding="utf-8", newline="\n")

    # empty store + dead endpoint: abort pre-handshake, loud, with the
    # pull fix — a raw Popen, because _spawn's recv would just time out
    # on a process that never answers
    env_abort = dict(base)
    env_abort["NEURONAV_CONFIG"] = str(probe_cfg)
    p = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(HERE / "server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        cwd=str(scratch), env=env_abort)
    _, abort_err = p.communicate(input="", timeout=120)
    check("fresh240: probe fail on an empty store aborts non-zero "
          "pre-handshake", p.returncode != 0, f"rc={p.returncode}")
    check("fresh240: abort names model + endpoint + the pull fix",
          "qwen3-embedding:0.6b" in abort_err and str(dead_port) in abort_err
          and "ollama pull qwen3-embedding:0.6b" in abort_err,
          abort_err[-400:])

    # warm the store under FAKE, then serve the same dead endpoint with
    # the probe failing: initialize still answers, stderr names it
    env_warm = dict(env_abort)
    env_warm["NEURONAV_EMBED_FAKE"] = "1"
    srv2 = _spawn(env_warm, cwd=scratch)
    try:
        srv2.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fresh", "version": "0"}}})
        check("fresh240: warm store builds under FAKE",
              "result" in srv2.recv(1), "")
        srv2.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        srv2.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "rescan", "arguments": {}}})
        srv2.recv(2)
    finally:
        srv2.kill()

    srv3 = _spawn(env_abort, cwd=scratch)
    try:
        srv3.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05",
                              "capabilities": {},
                              "clientInfo": {"name": "fresh",
                                             "version": "0"}}})
        r = srv3.recv(1)
        check("fresh240: probe fail on a warm store serves degraded",
              "result" in r, "")
        srv3.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        srv3.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "repo_map",
                              "arguments": {"budget_tokens": 256}}})
        out = text_of(srv3.recv(2)["result"])
        check("fresh240: warm degraded repo_map answers from the index",
              out.startswith(f"you are here: {warm.resolve().as_posix()}"),
              out[:120])
        time.sleep(0.3)
        err3 = "".join(srv3.stderr_lines)
        check("fresh240: stderr names the probe failure + degraded serve",
              "embed probe FAILED" in err3
              and "Serving the warm index degraded" in err3, err3[-400:])
    finally:
        srv3.kill()
        if FAILS:
            print("--- fresh240 probe server stderr (tail) ---")
            print("\n".join(srv3.stderr_lines[-15:]))


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

        # issue #280: impact over the wire on the controlled hub — full
        # transitive caller total, honest per-hop histogram with "+N
        # more", the direction guard answering guidance (not an error),
        # and a miss suggesting closest matches
        imp = call(19, "impact", {"symbol": "hub", "max_depth": 2})
        check("drift/280: impact answers full transitive caller totals",
              imp.splitlines()[0] == "impact of hub (hub_wire.py#hub): "
              "callers — what breaks"
              and "total: 14 within 2 hops" in imp
              and "    depth 1: 14 (" in imp and "+6 more" in imp,
              imp[:200])
        bad = call(20, "impact", {"symbol": "hub", "direction": "sideways"})
        check("drift/280: bad direction answers guidance, not an error",
              bad.startswith("direction must be 'callers' or 'callees'"),
              bad[:120])
        imiss = call(21, "impact", {"symbol": "HubBenchh"})
        check("drift/280: impact miss suggests closest matches",
              imiss.startswith("no function matching 'HubBenchh'")
              and "Closest matches: hub" in imiss,
              imiss[:160])
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
def _memory_scenario() -> None:
    """issue #67: the memory tool contract over stdio — hermetic scratch
    tree, fake embeds, the boot project plus routed ones, so verb surface,
    file bytes, routing isolation and loud failures are all pinned."""
    import shutil

    scratch = HERE / ".team_scratch" / "memory_stdio"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    boot, pa, pb, fresh = (scratch / n for n in ("boot", "alpha", "beta", "fresh"))
    for d in (boot, pa, pb, fresh):
        d.mkdir()
        (d / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    cfg = scratch / "boot.neuronav.json"
    cfg.write_text(json.dumps({
        "root": str(boot),
        "collection": "main",
        "state_dir": "default",
        "include_dirs": ["."],
        "extensions": [".py"],
        "exclude_dirs": [".git", "__pycache__", ".venv", ".neuronav"],
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env["NEURONAV_CONFIG"] = str(cfg)
    env["NEURONAV_EMBED_FAKE"] = "1"
    srv = _spawn(env)
    send, recv = srv.send, srv.recv

    def call(mid: int, verb: str, args: dict) -> dict:
        send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
              "params": {"name": "memory", "arguments": {"verb": verb, **args}}})
        return recv(mid)["result"]

    bdir = boot / ".neuronav" / "memories"
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "mem", "version": "0"}}})
        check("memory: initialize handshake", "result" in recv(1), "")

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(2)["result"]["tools"]
        mem = next(t for t in tools if t["name"] == "memory")
        ann = mem.get("annotations") or {}
        check("memory: no readOnlyHint (mutating, like rescan)",
              not ann.get("readOnlyHint"), str(ann))
        schema = mem["inputSchema"]
        check("memory: schema — verb required; name/body/dir optional",
              set(schema["properties"]) == {"verb", "name", "body", "dir"}
              and schema.get("required") == ["verb"],
              f"props={sorted(schema['properties'])} req={schema.get('required')}")

        out = text_of(call(3, "list", {}))
        check("memory: empty list says so explicitly",
              out.startswith("no memories yet"), out)

        body = "<!-- login flow quirks -->\nrefresh needs the retry header\nnaïve — café\n"
        out = text_of(call(4, "set", {"name": "auth", "body": body}))
        check("memory: set confirms with the exact path",
              "saved 'auth'" in out
              and (bdir / "auth.md").as_posix() in out, out)
        raw = (bdir / "auth.md").read_bytes()
        check("memory: file bytes — # name H1 + body, LF, no BOM",
              raw == ("# auth\n" + body).encode() and b"\r" not in raw,
              repr(raw[:48]))

        out = text_of(call(5, "get", {"name": "auth"}))
        check("memory: set->get round-trip byte-identical", out == body, repr(out[:60]))

        l1 = text_of(call(6, "list", {}))
        l2 = text_of(call(7, "list", {}))
        check("memory: list deterministic (repeat call = identical)", l1 == l2, l1)
        check("memory: list shows name + one-line summary from the comment",
              "auth: login flow quirks" in l1, l1)

        # the dir convention is discoverable: hand-written files list too
        (bdir / "README.md").write_text(
            "# neuronav memories\nscaffold doc, not a memory\n",
            encoding="utf-8", newline="\n")
        (bdir / "hand.md").write_text(
            "# hand\n<!-- hand-written note -->\nwritten by a human\n",
            encoding="utf-8", newline="\n")
        l3 = text_of(call(8, "list", {}))
        check("memory: hand-written file listed, README skipped",
              "hand: hand-written note" in l3 and "auth: login flow quirks" in l3
              and "README" not in l3, l3)
        out = text_of(call(9, "get", {"name": "hand"}))
        check("memory: get returns everything after the H1, verbatim",
              out == "<!-- hand-written note -->\nwritten by a human\n", repr(out))

        (bdir / "bad.md").write_bytes(b"\xff\xfe not utf8\n")
        r = call(10, "list", {})
        check("memory: corrupt file is a loud list error naming the file",
              bool(r.get("isError")) and "bad.md" in text_of(r), text_of(r)[:120])
        r = call(11, "get", {"name": "bad"})
        check("memory: corrupt file is a loud get error naming the file",
              bool(r.get("isError")) and "bad.md" in text_of(r), text_of(r)[:120])
        (bdir / "bad.md").unlink()

        r = call(12, "get", {"name": "nope"})
        check("memory: get missing is loud", bool(r.get("isError"))
              and "nope" in text_of(r), text_of(r)[:120])
        r = call(13, "delete", {"name": "nope"})
        check("memory: delete missing refuses loud", bool(r.get("isError"))
              and "nope" in text_of(r), text_of(r)[:120])

        bads = ["../evil", "a/b", "..", ".hidden", "CON", "trailing.", "README"]
        for i, bad in enumerate(bads):
            r = call(14 + i, "set", {"name": bad, "body": "x"})
            check(f"memory: unsafe name refused: {bad!r}",
                  bool(r.get("isError")), text_of(r)[:100])
        check("memory: refused names never escaped the memories dir",
              not (scratch / "evil.md").exists() and not (scratch / "a").exists(),
              str(scratch))

        r = call(21, "set", {"name": "empty", "body": ""})
        check("memory: empty body refused", bool(r.get("isError")),
              text_of(r)[:100])
        r = call(22, "purge", {"name": "auth"})
        check("memory: unknown verb refused, valid verbs named",
              bool(r.get("isError")) and "list" in text_of(r)
              and "delete" in text_of(r), text_of(r)[:120])

        out = text_of(call(23, "set", {"name": "alpha-note", "body": "alpha only\n",
                                       "dir": str(pa)}))
        check("memory: routed set writes the routed project's dir",
              (pa / ".neuronav" / "memories" / "alpha-note.md").is_file()
              and not (pb / ".neuronav" / "memories" / "alpha-note.md").exists(),
              out[:120])
        out = text_of(call(24, "list", {"dir": str(pb)}))
        check("memory: routed list stays project-isolated",
              "alpha-note" not in out, out[:120])
        out = text_of(call(25, "list", {"dir": str(pa)}))
        check("memory: routed list shows the routed project's memory",
              "alpha-note: alpha only" in out, out[:120])
        out = text_of(call(26, "list", {}))
        check("memory: boot store untouched by routed writes",
              "alpha-note" not in out and "auth: login flow quirks" in out, out)

        out = text_of(call(27, "set", {"name": "first",
                                       "body": "written mid-onboarding\n",
                                       "dir": str(fresh)}))
        check("memory: fresh-dir set onboards AND keeps the write",
              "onboarded" in out and "saved 'first'" in out
              and (fresh / ".neuronav" / "memories" / "first.md").is_file()
              and (fresh / ".neuronav" / "memories" / "README.md").is_file(),
              out[:200])

        out = text_of(call(28, "delete", {"name": "auth"}))
        check("memory: delete confirms", "deleted 'auth'" in out, out)
        out = text_of(call(29, "list", {}))
        check("memory: gone after delete", "auth" not in out, out)
    finally:
        srv.kill()
        if FAILS:
            print("--- memory server stderr (tail) ---")
            print("\n".join(srv.stderr_lines[-15:]))




if __name__ == "__main__":
    main()

print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
