"""Project-mode onboarding suite (issue #27): the install stays read-only.

Covers nav._discover_config precedence, onboard.init/wire scaffolding,
and the viz add-on being optional. Hermetic: temp project trees, fake
embeds, fresh subprocesses for every discovery case (nav binds config
at import). Never touches the real stores."""

import json
import os
import subprocess
import sys
import tempfile
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
        check("wire: .mcp.json pins NEURONAV_CONFIG to the project config", entry["env"]["NEURONAV_CONFIG"] == str(cfg_path))
        oc_doc = json.loads(oc.read_text(encoding="utf-8"))
        check("wire: opencode.json merged, not replaced", "other" in oc_doc["mcp"] and oc_doc["mcp"]["neuronav"]["environment"]["NEURONAV_CONFIG"] == str(cfg_path))
        check("wire: install stays read-only", not (ROOT / "config" / "proj.json").exists())

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
            json.dumps({"root": str(p4), "include_dirs": ["nope-dir"]}), encoding="utf-8"
        )
        r = run_nav_raw(p4, "import nav; nav.rescan()", str(cfg_empty))
        err = (r.stderr or "") + (r.stdout or "")
        check("empty effective file set: rescan fails loudly", r.returncode != 0 and "0 files" in err and "nope-dir" in err, err[:120])

        # 8. nav: warm-rescan stat gate (issue #42) — unchanged files
        # skip the read+hash; the sha stays the identity
        p5 = tmp / "fifth"
        (p5 / "src").mkdir(parents=True)
        (p5 / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        (p5 / "src" / "other.py").write_text("def aux():\n    return 2\n", encoding="utf-8")
        (p5 / ".neuronav").mkdir()
        (p5 / ".neuronav" / "config.json").write_text(
            json.dumps({"root": str(p5), "include_dirs": ["."]}), encoding="utf-8"
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
if __name__ == "__main__":
    main()
    sys.exit(1 if FAILURES else 0)   # a failing run must fail the gate
