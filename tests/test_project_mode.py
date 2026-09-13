"""Project-mode onboarding suite (issue #27): the install stays read-only.

Covers nav._discover_config precedence, onboard.init/wire scaffolding,
and the viz add-on being optional. Hermetic: temp project trees, fake
embeds, fresh subprocesses for every discovery case (nav binds config
at import). Never touches the real stores."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def run_nav(cwd: Path, code: str, env_extra: dict | None = None) -> str:
    """Fresh interpreter with cwd set; NEURONAV_CONFIG scrubbed by default
    so discovery is exercised exactly as a user would hit it."""
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env["NEURONAV_EMBED_FAKE"] = "1"
    if env_extra:
        env.update(env_extra)
    code = f"import sys; sys.path.insert(0, r'{ROOT}'); " + code
    return subprocess.run(
        [PY, "-X", "utf8", "-c", code], cwd=cwd, env=env,
        capture_output=True, text=True, check=True,
    ).stdout


def make_project(tmp: Path) -> Path:
    proj = tmp / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    # scratch conventions (repo-root .tmp, agent .team_scratch): excluded
    # only via .neuroignore — nothing is hardcoded
    (proj / ".tmp").mkdir()
    (proj / ".tmp" / "scratch.py").write_text("def leaked():\n    return 2\n", encoding="utf-8")
    (proj / ".team_scratch").mkdir()
    (proj / ".team_scratch" / "census.py").write_text("def leaked2():\n    return 3\n", encoding="utf-8")
    (proj / ".gitignore").write_text("build/\n", encoding="utf-8")
    return proj


def _wait_listening(proc: subprocess.Popen, port: int, timeout_s: float = 25.0) -> bool:
    """True once 127.0.0.1:port accepts; False if proc died or timed out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    from extractors import EXTENSIONS

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proj = make_project(tmp)

        # 1. no config anywhere -> pure cwd defaults (the npx shape)
        out = run_nav(proj, "import nav; print(nav.ROOT); print(sorted(nav.EXTS)); print(nav.INCLUDE_DIRS); print(nav.STATE_DIR)")
        lines = out.strip().splitlines()
        check("no-config: root is cwd", lines[0] == str(proj), lines[0])
        check("no-config: walks everything, state under project", lines[2] == "('.',)" and lines[3] == str(proj / ".neuronav"))
        out2 = run_nav(proj, "import nav; print([str(p) for p in nav.iter_files() if '.tmp' in str(p)])")
        check("no-config: nothing hardcoded — .tmp walked until .neuroignore says otherwise", out2.strip() != "[]", out2)

        check("no-config: extensions = registered suffixes", lines[1] == str(sorted(EXTENSIONS)), lines[1])
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "init"], cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True, check=True)
        cfg_path = proj / ".neuronav" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        check("init: project-local config with walk defaults", cfg["root"] == str(proj) and cfg["include_dirs"] == ["."])
        check("init: scaffold opts into the project store (issue #91)",
              cfg.get("state_dir") == "default", str(cfg.get("state_dir")))
        out3 = run_nav(proj, "import nav; print([str(p) for p in nav.iter_files() if '.tmp' in str(p) or '.team_scratch' in str(p)])")
        check("init: scaffolded .neuroignore prunes .tmp and .team_scratch", out3.strip() == "[]", out3)
        ig = (proj / ".neuronav" / ".neuroignore").read_text(encoding="utf-8")
        check("init: .neuroignore scaffolded once, lists both conventions", ig.count(".tmp") == 1 and ig.count(".team_scratch") == 1, repr(ig))
        igf = proj / ".neuronav" / ".neuroignore"
        saved = igf.read_text(encoding="utf-8")
        igf.write_text("# user trimmed it\n", encoding="utf-8")
        out4 = run_nav(proj, "import nav; print([str(p) for p in nav.iter_files() if '.tmp' in str(p)])")
        check(".neuroignore: user-adjustable — dropping the line re-includes .tmp", out4.strip() != "[]", out4)
        igf.write_text(saved, encoding="utf-8")
        gi = (proj / ".gitignore").read_text(encoding="utf-8")
        check("init: gitignore gains exactly one .neuronav/ line", gi.count(".neuronav/") == 1, repr(gi))
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "init"], cwd=proj,
                       capture_output=True, text=True, check=True)
        gi2 = (proj / ".gitignore").read_text(encoding="utf-8")
        check("init: idempotent", gi2 == gi)

        # 2b. init does NOT clobber a customized config on re-run (issue #121)
        cfg_path.write_text(json.dumps({**cfg, "exclude_dirs": ["build/"], "embed_url": "http://example.invalid/embeddings"}), encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "init"], cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True, check=True)
        preserved = json.loads(cfg_path.read_text(encoding="utf-8"))
        check("init re-run: user config byte-preserved, customizations kept",
              preserved["exclude_dirs"] == ["build/"] and preserved["embed_url"] == "http://example.invalid/embeddings",
              str(preserved.get("exclude_dirs")))
        check("init re-run: notes the existing config instead of rewriting",
              "config exists" in r.stdout and "exclude_dirs" not in r.stdout, r.stdout.strip()[:100])
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")  # restore for later checks

        # 3. discovery: project-local beats the install's checkout config
        out = run_nav(proj, "import nav; print(nav.ROOT)")
        check("discovery: project-local config wins from project cwd", out.strip() == str(proj))
        # env still beats everything (self-index profile resolves to the repo)
        out = run_nav(proj, "import nav; print(nav.ROOT)",
                      {"NEURONAV_CONFIG": str(ROOT / "config" / "neuronav.json")})
        check("discovery: env var beats project-local", out.strip() == str(ROOT))
        # checkout config applies only when cwd IS the checkout
        out = run_nav(ROOT, "import nav; print(nav.COLLECTION); print(nav.ROOT != nav.__file__)")
        lines = out.strip().splitlines()
        legacy = (ROOT / "config.json").is_file()
        check("discovery: checkout-cwd keeps machine default", (not legacy) or lines[1] == "True", out.strip())

        # 4. wire: MCP entries in the project, none in the install
        oc = proj / "opencode.json"
        oc.write_text(json.dumps({"mcp": {"other": {}}}), encoding="utf-8")
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                       capture_output=True, text=True, check=True)
        mcp = json.loads((proj / ".mcp.json").read_text(encoding="utf-8"))
        entry = mcp["mcpServers"]["neuronav"]
        venv_py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # stdio entries run the install's own venv python when present,
        # otherwise the interpreter that ran onboard.py
        want_command = str(venv_py) if venv_py.is_file() else sys.executable
        check("wire: .mcp.json starts the repo venv python -X utf8 server.py",
              entry["command"] == want_command and
              entry["args"] == ["-X", "utf8", str(ROOT / "server.py")] and
              entry["env"]["NEURONAV_CONFIG"] == str(cfg_path),
              str(entry))
        oc_doc = json.loads(oc.read_text(encoding="utf-8"))
        check("wire: opencode.json merged, not replaced", "other" in oc_doc["mcp"] and oc_doc["mcp"]["neuronav"]["environment"]["NEURONAV_CONFIG"] == str(cfg_path))
        check("wire: install stays read-only", not (ROOT / "config" / "proj.json").exists())

        # 4b. wire is BOM-tolerant + loud on malformed MCP jsons, and not
        # truncated when a merge dies part-way (issue #121)
        mcp_path = proj / ".mcp.json"
        mcp_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"other": {}}}).encode("utf-8"))
        oc.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcp": {"other": {}}}).encode("utf-8"))
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                       capture_output=True, text=True, check=True)
        mcp2 = json.loads(mcp_path.read_text(encoding="utf-8"))
        check("wire: BOM'd .mcp.json parsed and merged, other entry kept",
              sorted(mcp2["mcpServers"]) == sorted(["other", "neuronav"]), str(mcp2["mcpServers"].keys()))
        oc2 = json.loads(oc.read_text(encoding="utf-8"))
        check("wire: BOM'd opencode.json parsed and merged",
              sorted(oc2["mcp"]) == sorted(["other", "neuronav"]), str(oc2["mcp"].keys()))
        check("wire: BOM'd files rewritten BOM-free",
              mcp_path.read_bytes()[:3] != b"\xef\xbb\xbf" and oc.read_bytes()[:3] != b"\xef\xbb\xbf")
        good = json.dumps({"mcp": {"other": {}}})
        # malformed .mcp.json body -> loud JSONDecodeError, nothing written
        mcp_path.write_text("not json {", encoding="utf-8")
        oc.write_text(good, encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True)
        err = (r.stderr or "") + (r.stdout or "")
        check("wire: malformed .mcp.json fails loud, names the file + remedy",
              r.returncode != 0 and "fix it or delete it" in err, err[:90])
        check("wire: loud failure leaves .mcp.json unwritten",
              mcp_path.read_text(encoding="utf-8") == "not json {")
        # non-object .mcp.json root -> loud, names the merge key
        mcp_path.write_text('["nope"]', encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True)
        err = (r.stderr or "") + (r.stdout or "")
        check("wire: non-object .mcp.json root loud with the merge key",
              r.returncode != 0 and "must be a JSON object" in err and "list" in err, err[:90])
        check("wire: non-object root leaves the file unwritten",
              mcp_path.read_text(encoding="utf-8") == '["nope"]')
        # object root whose merge container is not an object -> loud too
        mcp_path.write_text('{"mcpServers": []}', encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True)
        err = (r.stderr or "") + (r.stdout or "")
        check("wire: non-object mcpServers container loud with the key",
              r.returncode != 0 and "mcpServers" in err and "must be a JSON object" in err, err[:90])
        check("wire: non-object container leaves the file unwritten",
              mcp_path.read_text(encoding="utf-8") == '{"mcpServers": []}')
        # malformed opencode.json -> loud too, and .mcp.json stays merged
        mcp_path.write_text(json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8")
        oc.write_text("not json {", encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True)
        err = (r.stderr or "") + (r.stdout or "")
        check("wire: malformed opencode.json fails loud too",
              r.returncode != 0 and "opencode.json" in err and "fix it or delete it" in err, err[:90])
        check("wire: .mcp.json not truncated by the failed opencode merge",
              sorted(json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]) == sorted(["other", "neuronav"]))
        # tombstone: no write-temp debris survives any of the above paths
        # (the .tmp scratch dir is a fixture, not debris — match the
        # mkstemp prefix exactly)
        check("wire: no temp-file debris left behind",
              not list(proj.glob(".mcp.json.*.tmp")) and not list(proj.glob("opencode.json.*.tmp")), "")

        # 4c. wire starts from scratch when .mcp.json is absent (no tombstone)
        oc.write_text(good, encoding="utf-8")
        mcp_path.unlink()
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                       capture_output=True, text=True, check=True)
        check("wire: recreates a deleted .mcp.json cleanly", mcp_path.is_file())

        # 4d. wire --omp: emits the omp harness mcpServers fragment (issue #130)
        omp_env = {"NEURONAV_OMP_MCP": str(tmp / "omp-mcp.json")}
        home_mcp = Path.home() / ".omp" / "agent" / "mcp.json"
        home_before = home_mcp.read_bytes() if home_mcp.exists() else None
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire", "--omp"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"},
                            "NEURONAV_EMBED_FAKE": "1", **omp_env},
                       capture_output=True, text=True, check=True)
        omp_doc = json.loads((tmp / "omp-mcp.json").read_text(encoding="utf-8"))
        omp_entry = omp_doc["mcpServers"]["neuronav-proj"]
        check("omp: server name defaults to neuronav-<project>", "neuronav-proj" in omp_doc["mcpServers"], str(sorted(omp_doc["mcpServers"])))
        check("omp: fragment pins NEURONAV_CONFIG to the project config", omp_entry["env"]["NEURONAV_CONFIG"] == str(cfg_path))
        check("omp: fragment command = repo venv python, args -X utf8 server.py",
              omp_entry["command"] == entry["command"] and omp_entry["args"] == ["-X", "utf8", str(ROOT / "server.py")],
              str(omp_entry))
        check("omp: install stays read-only (home mcp.json untouched)",
              (home_mcp.read_bytes() if home_mcp.exists() else None) == home_before)

        # 4e. --omp-name overrides the server name; merge preserves other servers
        omp_doc["mcpServers"]["pre-existing"] = {"command": "x"}
        (tmp / "omp-mcp.json").write_text(json.dumps(omp_doc), encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire", "--omp", "--omp-name", "my-neuronav"],
                           cwd=proj,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"},
                                "NEURONAV_EMBED_FAKE": "1", **omp_env},
                           capture_output=True, text=True, check=True)
        omp_doc2 = json.loads((tmp / "omp-mcp.json").read_text(encoding="utf-8"))
        check("omp: --omp-name overrides the server name", "my-neuronav" in omp_doc2["mcpServers"], str(sorted(omp_doc2["mcpServers"])))
        check("omp: unrelated servers preserved on merge", "pre-existing" in omp_doc2["mcpServers"])
        check("omp: rerun idempotent", json.loads((tmp / "omp-mcp.json").read_text(encoding="utf-8")) == omp_doc2)

        # 4f. two projects: default names never collide in the single omp file
        p_second = tmp / "omp-two"
        (p_second / "src").mkdir(parents=True)
        (p_second / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire", "--omp", "--project", str(p_second)],
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"},
                            "NEURONAV_EMBED_FAKE": "1", **omp_env},
                       capture_output=True, text=True, check=True)
        omp_doc3 = json.loads((tmp / "omp-mcp.json").read_text(encoding="utf-8"))
        check("omp: two projects = two named servers",
              "my-neuronav" in omp_doc3["mcpServers"] and "neuronav-omp-two" in omp_doc3["mcpServers"],
              str(sorted(omp_doc3["mcpServers"])))

        # 4g. global-wire: ONE universal entry (no config pin, per-call dir
        # routing, issue #131) in all four harness user configs; merge-only,
        # idempotent, per-project pins preserved
        gw_env = {
            "NEURONAV_OMP_MCP": str(tmp / "omp-mcp.json"),
            "NEURONAV_OPENCODE_MCP": str(tmp / "opencode-user.json"),
            "NEURONAV_KILO_MCP": str(tmp / "kilo-mcp.json"),
            "NEURONAV_ZCODE_MCP": str(tmp / "zcode-config.json"),
        }
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "global-wire"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"},
                            "NEURONAV_EMBED_FAKE": "1", **gw_env},
                       capture_output=True, text=True, check=True)
        gw_omp = json.loads((tmp / "omp-mcp.json").read_text(encoding="utf-8"))
        gw_oc = json.loads((tmp / "opencode-user.json").read_text(encoding="utf-8"))
        gw_ki = json.loads((tmp / "kilo-mcp.json").read_text(encoding="utf-8"))
        gw_zc = json.loads((tmp / "zcode-config.json").read_text(encoding="utf-8"))
        u_omp = gw_omp["mcpServers"]["neuronav"]
        check("global-wire: omp universal entry carries no config pin",
              "env" not in u_omp and u_omp["args"] == ["-X", "utf8", str(ROOT / "server.py")], str(u_omp))
        check("global-wire: per-project omp pins survive beside the universal entry",
              "my-neuronav" in gw_omp["mcpServers"] and "neuronav-omp-two" in gw_omp["mcpServers"])
        oc_u = gw_oc["mcp"]["neuronav"]
        check("global-wire: opencode local shape (command list, no environment)",
              oc_u["type"] == "local" and oc_u["command"][0] == u_omp["command"]
              and "environment" not in oc_u, str(oc_u))
        ki_u = gw_ki["mcpServers"]["neuronav"]
        check("global-wire: kilocode Cline shape (stdio, no autoApprove widening)",
              ki_u["type"] == "stdio" and ki_u["command"] == u_omp["command"]
              and "autoApprove" not in ki_u, str(ki_u))
        zc_u = gw_zc["mcp"]["servers"]["neuronav"]
        check("global-wire: zcode mcp.servers local shape",
              zc_u["type"] == "local" and zc_u["command"] == u_omp["command"], str(zc_u))
        before_oc = (tmp / "opencode-user.json").read_text(encoding="utf-8")
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "global-wire"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"},
                            "NEURONAV_EMBED_FAKE": "1", **gw_env},
                       capture_output=True, text=True, check=True)
        check("global-wire: rerun idempotent",
              (tmp / "opencode-user.json").read_text(encoding="utf-8") == before_oc
              and json.loads((tmp / "kilo-mcp.json").read_text(encoding="utf-8")) == gw_ki)
        gw_oc["mcp"]["other-server"] = {"type": "remote", "url": "x"}
        (tmp / "opencode-user.json").write_text(json.dumps(gw_oc), encoding="utf-8")
        subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "global-wire"], cwd=proj,
                       env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"},
                            "NEURONAV_EMBED_FAKE": "1", **gw_env},
                       capture_output=True, text=True, check=True)
        check("global-wire: unrelated harness servers preserved",
              "other-server" in json.loads((tmp / "opencode-user.json").read_text(encoding="utf-8"))["mcp"])
        check("global-wire: install stays read-only (no home config writes)",
              (home_mcp.read_bytes() if home_mcp.exists() else None) == home_before)

        # 5. one command end-to-end (fake embeds): index + bake in the project
        p2 = make_project(tmp / "second")
        r = subprocess.run([PY, "-X", "utf8", str(ROOT / "onboard.py"), "wire", "--index"], cwd=p2,
                           env={**{k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}, "NEURONAV_EMBED_FAKE": "1"},
                           capture_output=True, text=True, check=True)
        bake = p2 / ".neuronav" / "graph.html"
        store = p2 / ".neuronav" / "chroma"
        check("wire --index: store + bake land in the project", bake.is_file() and store.is_dir() and not (ROOT / ".neuronav" / "chroma" / "main").exists())

        # 6. viz is an add-on: core imports never pull it, server degrades
        out = run_nav(proj, "import sys; import nav, recall, graph, clusters; print('viz' in sys.modules)")
        check("core modules never import viz", out.strip() == "False")
        sys.path.insert(0, str(ROOT))
        import server  # noqa: E402

        server._auto_rescan = lambda: None  # gate off; we test the degrade path
        sys.modules["viz"] = None  # import viz now raises ImportError
        msg = server.visualize()
        check("visualize degrades loudly without the add-on", "viz add-on not installed" in msg, msg[:60])

        # 6b. issue #133: a real bake answers with an openable file:// URI —
        # production opens <project>/.neuronav/graph.html directly, no server
        import types
        nav_state = Path(run_nav(proj, "import nav; print(nav.STATE_DIR)",
                                 {"NEURONAV_CONFIG": str(cfg_path)}).strip())
        fake_viz = types.ModuleType("viz")
        fake_viz.ensure_bake = lambda: nav_state / "graph.html"
        sys.modules["viz"] = fake_viz
        try:
            msg2 = server.visualize()
        finally:
            del sys.modules["viz"]
        check("visualize: happy path appends the openable file:// URI",
              msg2.startswith("3D graph written to")
              and str(nav_state / "graph.html") in msg2
              and "file://" in msg2,
              msg2[:120])

        # 6c. issue #133: onboard prints the per-OS open command for the
        # baked file — win32 `start`, darwin `open`, POSIX `xdg-open`
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("onboard_uv", str(ROOT / "onboard.py"))
        _onboard = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_onboard)
        plat2 = sys.platform
        try:
            sys.platform = "win32"
            win_open = _onboard.open_viewer(proj)
            sys.platform = "darwin"
            mac_open = _onboard.open_viewer(proj)
            sys.platform = "linux"
            lin_open = _onboard.open_viewer(proj)
        finally:
            sys.platform = plat2
        bake = proj / ".neuronav" / "graph.html"
        check("open_viewer: win32 prints `start <bake>`",
              win_open == f"start {bake}", win_open)
        check("open_viewer: darwin prints `open <bake>`",
              mac_open == f"open {bake}", mac_open)
        check("open_viewer: POSIX prints `xdg-open <bake>`",
              lin_open == f"xdg-open {bake}", lin_open)
        # a space-y project path needs quoting; win32 additionally needs
        # `start "" "path"` (first quoted arg is the cmd window title)
        spacey = tmp / "my project"
        spacey.mkdir()
        (spacey / ".neuronav").mkdir()
        (spacey / ".neuronav" / "graph.html").write_text("x", encoding="utf-8")
        try:
            sys.platform = "win32"
            win_space = _onboard.open_viewer(spacey)
            sys.platform = "linux"
            lin_space = _onboard.open_viewer(spacey)
        finally:
            sys.platform = plat2
        check("open_viewer: win32 quotes and adds the empty title arg",
              win_space == f'start "" "{spacey / ".neuronav" / "graph.html"}"', win_space)
        check("open_viewer: POSIX quotes a space-y path",
              lin_space == f'xdg-open "{spacey / ".neuronav" / "graph.html"}"', lin_space)

        # 7. nav: explicit-config guards (issue #41) — a missing env
        # config is a hard exit, an empty effective file set a loud
        # rescan error; neither may degrade silently
        def run_nav_raw(cwd: Path, code: str, cfg: str | None = None) -> subprocess.CompletedProcess:
            env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
            env["NEURONAV_EMBED_FAKE"] = "1"
            if cfg is not None:
                env["NEURONAV_CONFIG"] = cfg
            code = f"import sys; sys.path.insert(0, r'{ROOT}'); " + code
            return subprocess.run(
                [PY, "-X", "utf8", "-c", code], cwd=cwd, env=env,
                capture_output=True, text=True,
            )

        p3 = make_project(tmp / "third")
        r = run_nav_raw(p3, "import nav", str(p3 / ".neuronav" / "typo.json"))
        err = (r.stderr or "") + (r.stdout or "")
        check("missing env config: loud nonzero exit", r.returncode != 0, f"rc={r.returncode} {err[:60]}")
        check("missing env config: names the path + remedy", "typo.json" in err and "unset" in err, err[:120])
        # no config anywhere stays legal (pure defaults; section 1 pins it)
        # — but a config whose walk matches nothing must fail the rescan
        p4 = make_project(tmp / "fourth")
        cfg_empty = p4 / ".neuronav" / "no-match.json"
        p4.joinpath(".neuronav").mkdir()
        cfg_empty.write_text(
            json.dumps({"root": str(p4), "include_dirs": ["nope-dir"], "state_dir": "default"}), encoding="utf-8"
        )
        r = run_nav_raw(p4, "import nav; nav.rescan()", str(cfg_empty))
        err = (r.stderr or "") + (r.stdout or "")
        check("empty effective file set: rescan fails loudly", r.returncode != 0 and "0 files" in err and "nope-dir" in err, err[:120])

        # 7b. state_dir loud-abort (issue #91): a config file with no
        # explicit state_dir used to silently default to <root>/.neuronav —
        # a store INSIDE the scanned root, the exact door that wiped a live
        # one. The law is provenance-agnostic (env, project-local, or the
        # legacy checkout-local config all abort the same); "default" is the
        # explicit opt-in (onboard writes it); an explicit path just works;
        # the no-config pure-defaults leg (section 1) stays silent.
        p6 = make_project(tmp / "sixth")
        p6.joinpath(".neuronav").mkdir()
        cfg_bare = p6 / ".neuronav" / "stateless.json"
        cfg_bare.write_text(
            json.dumps({"root": str(p6), "include_dirs": ["."]}), encoding="utf-8"
        )
        r = run_nav_raw(p6, "import nav", str(cfg_bare))
        err = (r.stderr or "") + (r.stdout or "")
        check("state_dir-less config: import aborts nonzero", r.returncode != 0, f"rc={r.returncode}")
        check("state_dir-less config: names the config, the key and both fixes",
              "stateless.json" in err and "\"state_dir\"" in err and "\"default\"" in err, err[:160])
        check("state_dir-less config: aborts before creating the default store",
              not (p6 / ".neuronav" / "chroma").exists())
        # the same law fires for the legacy checkout-local config leg
        # (discovered only when cwd IS the checkout) — restored after
        legacy_cfg = ROOT / "config.json"
        try:
            legacy_cfg.write_text(
                json.dumps({"root": str(p6), "include_dirs": ["."]}), encoding="utf-8"
            )
            r = run_nav_raw(ROOT, "import nav")
            err = (r.stderr or "") + (r.stdout or "")
            check("state_dir-less checkout-local config: aborts too",
                  r.returncode != 0 and "\"state_dir\"" in err, err[:100])
        finally:
            legacy_cfg.unlink(missing_ok=True)
        # the opt-in resolves to the same <root>/.neuronav as ever
        cfg_def = p6 / ".neuronav" / "opted-in.json"
        cfg_def.write_text(
            json.dumps({"root": str(p6), "include_dirs": ["."], "state_dir": "default"}), encoding="utf-8"
        )
        out = run_nav(p6, "import nav; print(nav.STATE_DIR)", {"NEURONAV_CONFIG": str(cfg_def)})
        check("opt-in \"default\": resolves to <root>/.neuronav",
              out.strip() == str(p6 / ".neuronav"), out.strip())
        # an explicit scratch state_dir is honored — no abort
        cfg_scr = p6 / ".neuronav" / "scratch.json"
        cfg_scr.write_text(
            json.dumps({"root": str(p6), "include_dirs": ["."], "state_dir": str(tmp / "sixth-state")}), encoding="utf-8"
        )
        out = run_nav(p6, "import nav; print(nav.STATE_DIR)", {"NEURONAV_CONFIG": str(cfg_scr)})
        check("explicit scratch state_dir: honored, no abort",
              out.strip() == str(tmp / "sixth-state"), out.strip())

        # 8. nav: warm-rescan stat gate (issue #42) — unchanged files
        # skip the read+hash; the sha stays the identity
        p5 = tmp / "fifth"
        (p5 / "src").mkdir(parents=True)
        (p5 / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        (p5 / "src" / "other.py").write_text("def aux():\n    return 2\n", encoding="utf-8")
        (p5 / ".neuronav").mkdir()
        (p5 / ".neuronav" / "config.json").write_text(
            json.dumps({"root": str(p5), "include_dirs": ["."], "state_dir": "default"}), encoding="utf-8"
        )
        code8 = (
            "import json, os\n"
            "import nav\n"
            "cold = nav.rescan()\n"
            "col = nav._collection()\n"
            "ids1 = sorted(col.get()['ids'])\n"
            "warm = nav.rescan()\n"
            "ids2 = sorted(col.get()['ids'])\n"
            "p = nav.ROOT / 'src' / 'app.py'\n"
            "p.write_text(p.read_text(encoding='utf-8') + '\\ndef extra():\\n    return 3\\n', encoding='utf-8')\n"
            "touched = nav.rescan()\n"
            "q = nav.ROOT / 'src' / 'other.py'\n"
            "st = q.stat()\n"
            "os.utime(q, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))\n"
            "resaved = nav.rescan()\n"
            "print(json.dumps({'cold': cold, 'warm': warm, 'ids_stable': ids1 == ids2,\n"
            "                  'touched': touched, 'resaved': resaved}))\n"
        )
        r = run_nav_raw(p5, code8, str(p5 / ".neuronav" / "config.json"))
        if r.returncode != 0:
            check("stat gate: scenario subprocess ran", False, (r.stderr or "")[:120])
        else:
            out = json.loads(r.stdout.strip().splitlines()[-1])
            w = {k: out["warm"][k] for k in ("added", "updated", "unchanged", "deleted")}
            check("stat gate: warm rescan is 0/0/2/0 (a/u/u/d)", w == {"added": 0, "updated": 0, "unchanged": 2, "deleted": 0}, str(w))
            check("stat gate: warm rescan leaves count + ids identical", out["ids_stable"] and out["cold"]["added"] == 2)
            check("stat gate: touched file re-hashes + updates", out["touched"]["updated"] == 1 and "src/app.py" in out["touched"]["changed"], str(out["touched"]["updated"]))
            check("stat gate: re-stat'd identical file embeds nothing", out["resaved"]["updated"] == 0 and out["resaved"]["unchanged"] == 2, str(out["resaved"]["updated"]))
        # 9. serve.py refuses a taken port loudly (issue #40): a second
        # instance must exit nonzero and say why — never silently shadow
        # the first listener (Windows SO_REUSEADDR double-bind).
        serve_py = str(ROOT / "tools" / "serve.py")
        scrub = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
        port, first = None, None
        for cand in range(9081, 9091):  # team ports only; 8791/8792 are owner tooling
            first = subprocess.Popen(
                [PY, "-X", "utf8", serve_py, "--port", str(cand)],
                cwd=proj, env=scrub, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if _wait_listening(first, cand):
                port = cand
                break
            first.wait()
        check("serve: first instance listening on a team port", port is not None,
              "9081-9090 all taken")
        if port is not None:
            try:
                dup = subprocess.run(
                    [PY, "-X", "utf8", serve_py, "--port", str(port)],
                    cwd=proj, env=scrub, capture_output=True, text=True, timeout=90)
                why = (dup.stdout + dup.stderr).strip()
                check("serve: second bind on a taken port exits nonzero",
                      dup.returncode != 0, f"rc={dup.returncode}")
                want_hint = ("Get-NetTCPConnection" if sys.platform == "win32"
                             else "lsof")   # per-OS remedy (issue #51)
                check("serve: refusal prints the port + owner hint",
                      str(port) in why and want_hint in why, why[:70])
            except subprocess.TimeoutExpired:
                check("serve: second bind on a taken port exits nonzero", False,
                      "second instance kept serving (timeout)")
                check("serve: refusal prints the port + owner hint", False, "no refusal printed")
            finally:
                first.kill()
                first.wait()

        # 9b. the remedy line is per-OS (issue #51): the PowerShell cmdlet
        # only exists on Windows — POSIX prints lsof/ss instead
        import importlib.util
        spec = importlib.util.spec_from_file_location("serve_hint", serve_py)
        serve_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(serve_mod)
        plat = sys.platform
        try:
            sys.platform = "win32"
            win_hint = serve_mod.port_owner_hint(9123)
            sys.platform = "linux"
            posix_hint = serve_mod.port_owner_hint(9123)
        finally:
            sys.platform = plat
        check("serve: win32 hint is the cmdlet with the port",
              win_hint == "Get-NetTCPConnection -LocalPort 9123", win_hint)
        check("serve: POSIX hint is lsof/ss, never the cmdlet",
              "lsof" in posix_hint and "9123" in posix_hint
              and "Get-NetTCPConnection" not in posix_hint, posix_hint)
if __name__ == "__main__":
    main()
    sys.exit(1 if FAILURES else 0)   # a failing run must fail the gate
