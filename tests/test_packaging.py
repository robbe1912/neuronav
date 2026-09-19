# hermetic packaging suite (issue #204):
#   .venv/Scripts/python.exe -X utf8 tests/test_packaging.py
#
# The uvx contract end-to-end minus the network: build the wheel, install
# it into a scratch venv (deps inherited from the running interpreter via
# system-site-packages — only the wheel itself is installed), spawn the
# INSTALLED `neuronav-mcp` console script in a temp fixture repo with NO
# config anywhere (no NEURONAV_CONFIG, no .neuronav/config.json, cwd not
# the checkout), and drive real JSON-RPC over stdio:
#   - initialize handshake answers
#   - tools/list advertises the tool surface
#   - repo_map serves the fixture's own files
#   - the #203 pre-rescan banner names `pure defaults, root=<cwd>` on
#     stderr BEFORE any rescan output
# plus wheel-content contracts: the flat py-modules, the extractors/bake
# packages, the vendored three.js bytes byte-identical (bake law), and
# the console-script entry point.
#
# Builder selection is loud, never a silent skip: `python -m build` when
# importable (CI installs it), else `python -m pip wheel` (pip ships with
# every interpreter). Only BOTH missing skips — printed, exit 0. A
# builder that runs and fails is a FAIL.

import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import shutil
import threading
import time
import venv
import sysconfig
import zipfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]



from harness import FAILURES as FAILS, check

VENDOR_FILES = (
    "vendor/three-0.160.0/three.module.js",
    "vendor/three-0.160.0/controls/OrbitControls.js",
    "vendor/three-0.160.0/lines/LineSegments2.js",
    "vendor/three-0.160.0/lines/LineSegmentsGeometry.js",
    "vendor/three-0.160.0/lines/LineMaterial.js",
)
PY_MODULES = (
    "nav.py", "navconfig.py", "navstore.py", "navindex.py", "graph.py", "server.py", "servercore.py",
    "server_search.py", "server_structure.py", "server_clusters.py", "viz.py", "layout.py",
    "clusters.py", "explore.py", "recall.py", "onboard.py", "memories.py", "predicates.py",
)

# the repo's dev-only and machine-local trees must NOT ride the wheel
ABSENT_PREFIXES = ("tests/", "tools/", "config/", "bench/", "docs/", "templates/")
TOOL_NAMES = (
    "explore", "repo_map", "semantic_search", "find_functions", "search_text",
    "symbol_graph", "dead_code", "duplicates", "clusters", "crosstalk",
    "context", "visualize", "rescan", "memory",
)

def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}



def _build_wheel(out: Path) -> bool:
    """Build the wheel into `out`; True on success (a FAIL is recorded
    on builder failure)."""
    if importlib.util.find_spec("build") is not None:
        cmd = [sys.executable, "-X", "utf8", "-m", "build", "--wheel",
               "--outdir", str(out), str(HERE)]
        which = "python -m build --wheel"
    else:
        cmd = [sys.executable, "-X", "utf8", "-m", "pip", "wheel", "--no-deps",
               "-w", str(out), str(HERE)]
        which = "python -m pip wheel --no-deps"
    print(f"builder: {which}")
    r = subprocess.run(cmd, cwd=HERE, env=_clean_env(),
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        check("wheel build succeeds", False,
              f"{which} rc={r.returncode}\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
        return False
    # setuptools writes build/ + <name>.egg-info into the SOURCE tree —
    # leaving them pollutes every later walk (test_selfindex's
    # likely-dead pin trips on the copied .py files); sweep them
    for dirt in (HERE / "build", *HERE.glob("*.egg-info")):
        shutil.rmtree(dirt, ignore_errors=True)
    return True


def _check_wheel_content(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        for m in PY_MODULES:
            check(f"wheel ships flat module {m}", m in names)
        for p in ("extractors/__init__.py", "bake/__init__.py"):
            check(f"wheel ships package {p}", p in names)
        stray = sorted({n.split("/", 1)[0] + "/" for n in names
                        if n.split("/", 1)[0] + "/" in ABSENT_PREFIXES})
        check("wheel carries no dev/machine trees (tests/tools/config/...)",
              not stray, str(stray))
        for v in VENDOR_FILES:
            check(f"wheel ships vendored three file at {v}", v in names)
        for v in VENDOR_FILES:
            shipped = zf.read(v)
            committed = (HERE / v).read_bytes()
            check(f"vendored bytes byte-identical: {v.rsplit('/', 1)[-1]}",
                  shipped == committed,
                  f"{len(shipped)} vs {len(committed)} bytes")
        eps = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        ep_text = zf.read(eps[0]).decode("utf-8") if eps else ""
        mds = [n for n in names if n.endswith(".dist-info/METADATA")]
        md_text = zf.read(mds[0]).decode("utf-8") if mds else ""
    check("console script neuronav-mcp = server:main declared",
          "neuronav-mcp = server:main" in ep_text, ep_text.strip())
    check("wheel METADATA pins tree-sitter-typescript==0.23.2 (issue #245)",
          "Requires-Dist: tree-sitter-typescript==0.23.2" in md_text,
          "pin missing from wheel METADATA")
    check("wheel METADATA pins tree-sitter-rust==0.24.2 (issue #244)",
          "Requires-Dist: tree-sitter-rust==0.24.2" in md_text,
          "pin missing from wheel METADATA")


def _make_fixture(tmp: Path) -> Path:
    """A tiny no-config repo: pure-defaults boot must walk exactly it."""
    fx = tmp / "fixture"
    (fx / "src").mkdir(parents=True)
    (fx / "src" / "app.py").write_text(
        "from src.util import area\n\n\ndef main():\n"
        "    return area(2)\n", encoding="utf-8")
    (fx / "src" / "util.py").write_text(
        "PI = 3.14\n\n\ndef area(r):\n    return PI * r * r\n", encoding="utf-8")
    (fx / "lib").mkdir()
    (fx / "lib" / "widget.gd").write_text(
        "extends Node\nclass_name Widget\n\nfunc ping() -> void:\n    pass\n",
        encoding="utf-8")
    return fx


def _spawn_console(script: Path, cwd: Path) -> SimpleNamespace:
    """Start the installed console script + daemon drain threads;
    .send/.recv/.kill/.stderr_lines (the test_server_stdio shape —
    stderr must be drained or the boot logs can wedge the pipe)."""
    env = _clean_env() | {"NEURONAV_EMBED_FAKE": "1"}
    proc = subprocess.Popen(
        [str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=cwd, env=env,
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

    def recv(want_id, timeout: float = 180.0) -> dict:
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
        proc.wait(timeout=10)
        time.sleep(0.5)

    return SimpleNamespace(proc=proc, send=send, recv=recv, kill=kill,
                           stderr_lines=stderr_lines)


def main() -> None:
    if importlib.util.find_spec("build") is None and importlib.util.find_spec("pip") is None:
        # loud skip, never silent: CI installs `build` so this leg runs there
        print("SKIP tests/test_packaging.py — no wheel builder available "
              "(neither `build` nor `pip` importable); CI installs `build` "
              "to run this suite")
        print(f"{len(FAILS)} failure(s)")
        sys.exit(0)

    tmp = Path(tempfile.mkdtemp(prefix="neuronav-pkg-"))
    wheels = tmp / "wheels"
    wheels.mkdir()
    if not _build_wheel(wheels):
        print(f"{len(FAILS)} failure(s)")
        sys.exit(1)
    built = sorted(wheels.glob("neuronav-*.whl"))
    check("exactly one neuronav wheel built", len(built) == 1,
          str([w.name for w in built]))
    if not built:
        print(f"{len(FAILS)} failure(s)")
        sys.exit(1)
    wheel = built[0]
    print(f"wheel: {wheel.name}")
    _check_wheel_content(wheel)

    # scratch venv: deps come from the running env via an addsitedir .pth
    # (venv-of-venv gap — system_site_packages would resolve to the real
    # base install, and a bare PYTHONPATH skips the deps' own .pth setup,
    # which pywin32 needs on win32). Wheel dep resolution itself is
    # exercised by real uvx installs, not here; install needs no network.
    vdir = tmp / "venv"
    venv.create(str(vdir), with_pip=True)
    sp = vdir / ("Lib/site-packages" if os.name == "nt" else
                 f"lib/python{sys.version_info.major}.{sys.version_info.minor}"
                 "/site-packages")
    (sp / "_neuronav_deps.pth").write_text(
        f"import site; site.addsitedir(r'{sysconfig.get_paths()['purelib']}')\n",
        encoding="utf-8")
    vpy = vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    r = subprocess.run([str(vpy), "-m", "pip", "install", "--no-deps",
                        "--quiet", str(wheel)],
                       capture_output=True, text=True, timeout=300)
    check("wheel installs into the scratch venv", r.returncode == 0,
          (r.stderr or r.stdout)[-500:])
    script = vdir / ("Scripts/neuronav-mcp.exe" if os.name == "nt"
                     else "bin/neuronav-mcp")
    check("neuronav-mcp console script installed", script.is_file(), str(script))
    if not script.is_file():
        print(f"{len(FAILS)} failure(s)")
        sys.exit(1)

    fx = _make_fixture(tmp).resolve()
    srv = _spawn_console(script, fx)
    try:
        srv.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {},
                             "clientInfo": {"name": "pkg-test", "version": "0"}}})
        init = srv.recv(1)
        name = init.get("result", {}).get("serverInfo", {}).get("name", "")
        check("installed server answers initialize", name == "neuronav", str(init)[:200])
        srv.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        srv.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = srv.recv(2)
        got = {t.get("name", "") for t in listed.get("result", {}).get("tools", [])}
        check("tools/list advertises the full surface",
              set(TOOL_NAMES) <= got, str(sorted(got)))
        srv.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "repo_map",
                             "arguments": {"budget_tokens": 800}}})
        rm = srv.recv(3)
        text = json.dumps(rm.get("result", {}))
        check("repo_map serves the fixture repo (app.py + util.py)",
              "app.py" in text and "util.py" in text, text[:200])
        # #203: the banner lands BEFORE the startup-stats line (pre-rescan
        # visibility) and names the pure-defaults root = the fixture cwd
        deadline = time.time() + 30
        while len(srv.stderr_lines) < 2 and time.time() < deadline:
            time.sleep(0.2)
        lines = [ln.rstrip("\n") for ln in srv.stderr_lines]
        banner = [ln for ln in lines if ln.startswith("neuronav: pure defaults")]
        check("pre-rescan stderr banner names pure defaults + the cwd root",
              any(ln == f"neuronav: pure defaults, root={fx}" for ln in banner),
              str(lines[:4]))
        stats_idx = [i for i, ln in enumerate(lines)
                     if ln.startswith("neuronav: startup files")]
        if banner and stats_idx:
            check("banner precedes the startup-stats line",
                  lines.index(banner[0]) < stats_idx[0], str(lines[:4]))
    finally:
        srv.kill()

    # byte pin: summary without leading blank line — kept local
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


main()
