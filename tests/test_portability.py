# Portability suite (issue #119) — Run in its own process:
#   .venv/Scripts/python.exe -X utf8 tests/test_portability.py
#
# Hermetic: generated temp target tree + config under the suite's own
# mkdtemp dir (never the real index), NEURONAV_EMBED_FAKE=1. Pins the
# three encoding/portability holes that only bite the BOM-producing
# platform (Windows):
#   (1) a BOM'd config.json must load (utf-8-sig, not utf-8 — a
#       PowerShell-5 `Set-Content -Encoding utf8` config used to crash
#       json.loads on \ufeff), and a BOM'd .neuroignore must still
#       exclude its FIRST line instead of silently corrupting the name;
#   (2) git subprocess output decodes as UTF-8 (bake.gitinfo head/churn)
#       even when the locale codec is ASCII/cp1252 — otherwise the churn
#       channel silently disabled itself on the first non-ASCII path;
#   (3) the nav CLI reconfigures stdout/stderr to UTF-8 at entry, so a
#       direct `python nav.py search` piped through an ASCII/cp1252
#       console prints hit paths instead of raising UnicodeEncodeError.

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable

TMP = Path(tempfile.mkdtemp(prefix="neuronav_portab_"))
(TMP / "src" / "scr1").mkdir(parents=True)
(TMP / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
# non-ASCII name: the whole point of the issue is that these stay intact
(TMP / "src" / "café_helper.py").write_text("def café_helper():\n    return 1\n", encoding="utf-8")
(TMP / "src" / "scr1" / "skip.py").write_text("def skipped():\n    return 9\n", encoding="utf-8")

CFG = TMP / "config.json"
CFG.write_text(
    json.dumps(
        {
            "root": str(TMP),
            "include_dirs": ["src"],
            "extensions": [".py"],
            "state_dir": str(TMP / "state"),
        }
    )
    + "\n",
    encoding="utf-8",
)
# write BOTH as a PowerShell-5 tool would: BOM + utf-8 content
cfg_bom = CFG.read_bytes()
CFG.write_bytes(b"\xef\xbb\xbf" + cfg_bom)
IG = TMP / ".neuroignore"
IG.write_bytes(b"\xef\xbb\xbfscr1\n")

os.environ["NEURONAV_CONFIG"] = str(CFG)
os.environ.setdefault("NEURONAV_EMBED_FAKE", "1")
sys.path.insert(0, str(REPO))

import nav  # noqa: E402  (binds the BOM'd config above)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def run_child(
    args: list[str],
    cwd: Path,
    *,
    env_extra: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Fresh interpreter named-argument-free helper; NEURONAV_CONFIG is
    scrubbed by default so each child binds exactly what it is given."""
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env["NEURONAV_EMBED_FAKE"] = "1"
    env["PYTHONPATH"] = str(REPO)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=text,
    )


def build_store() -> None:
    nav.rescan()
    nav.export_base()


def main() -> None:
    # ---- (1a) BOM'd config.json loads + BOM'd .neuroignore still prunes ---
    check("bom config: import binds the configured root", nav.ROOT == TMP, str(nav.ROOT))
    check("bom config: state_dir resolved", nav.STATE_DIR == TMP / "state", str(nav.STATE_DIR))
    check("bom neuroignore: first-line name intact, no \\ufeff prefix", "scr1" in nav.EXCLUDE_DIRS, str(nav.EXCLUDE_DIRS))
    check("bom neuroignore: \\ufeff-prefixed twin absent", "\\ufeffscr1" not in nav.EXCLUDE_DIRS, str(nav.EXCLUDE_DIRS))

    build_store()
    # the walk must actually reflect the BOM'd .neuroignore: scr1/skip.py is
    # excluded, the non-ASCII file is indexed under its real name
    ids = {r for r in nav._collection().get()["ids"]}
    check("bom neuroignore: excluded dir never indexed", "src/scr1/skip.py" not in ids, str(sorted(ids)))
    check("bom config: non-ASCII file indexed intact", "src/café_helper.py" in ids, str(sorted(ids)))

    # fresh-process proofs: nav binds the BOM'd config from scratch, not
    # merely from this suite's in-process import
    r = run_child(
        [PY, "-X", "utf8", "-c", "import nav; print(nav.ROOT); print(nav.STATE_DIR); print('scr1' in nav.EXCLUDE_DIRS)"],
        TMP,
        env_extra={"NEURONAV_CONFIG": str(CFG)},
    )
    lines = (r.stdout or "").strip().splitlines()
    check("bom config: fresh child binds root", not r.returncode and len(lines) == 3 and lines[0] == str(TMP), (r.stdout or "")[:120])
    check("bom config: fresh child state_dir", len(lines) == 3 and lines[1] == str(TMP / "state"), (r.stdout or "")[:120])
    check("bom neuroignore: fresh child excludes", len(lines) == 3 and lines[2] == "True", (r.stdout or "")[:120])

    # ---- (1b) base-export manifest BOM-tolerant import ----------------------
    # export wrote a clean manifest; BOM it (PowerShell 5 rewrites) and seed
    # a SECOND store from the shards — import_base must read the BOM'd
    # manifest, not crash on \ufeff
    m = nav.BASE_DIR / nav.MANIFEST_NAME
    m.write_bytes(b"\xef\xbb\xbf" + m.read_bytes())
    cfg2 = TMP / "config2.json"
    cfg2.write_text(
        json.dumps(
            {
                "root": str(TMP),
                "include_dirs": ["src"],
                "extensions": [".py"],
                "state_dir": str(TMP / "state2"),
                "collection": "portcopy",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    nav.use_config(cfg2)
    shutil.copytree(TMP / "state" / "base", nav.BASE_DIR)
    imp = nav.import_base()
    check("bom manifest: import reads BOM'd manifest", imp.get("imported") == 2, str(imp))
    check("bom manifest: imported store populated", nav.count() == 2, str(nav.count()))
    r = run_child([PY, "-X", "utf8", "-c", "import nav; print(nav.count())"], TMP, env_extra={"NEURONAV_CONFIG": str(cfg2)})
    check("bom manifest: fresh child imports too", r.returncode == 0 and r.stdout.strip() == "2", (r.stdout or r.stderr or "")[:120])

    # ---- (1c) server MCP accepts a BOM'd foreign project config ------------
    import server  # noqa: E402  (lazy off the hot path; imports nav singletons)

    fproj = TMP / "foreign"
    (fproj / ".neuronav").mkdir(parents=True)
    fcfg = fproj / ".neuronav" / "config.json"
    fcfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"state_dir": "default", "root": str(fproj)}).encode("utf-8"))
    got = server._validate_foreign_config(fcfg, fproj)
    check("server: BOM'd foreign config validates", got.get("state_dir") == "default", str(got))

    # ---- (3) console encoding: ascii console + non-ASCII hit path ----------
    nav.use_config(CFG)  # back to the store the CLI search will read
    r = run_child(
        [PY, str(REPO / "nav.py"), "search", "helper"],
        TMP,
        env_extra={
            "NEURONAV_CONFIG": str(CFG),
            "PYTHONIOENCODING": "ascii",
            "PYTHONUTF8": "0",
        },
        text=False,
    )
    out = r.stdout or b""
    check("console: nav CLI exits 0 on ascii console", r.returncode == 0, (r.stderr or b"")[:120])
    check(
        "console: non-ASCII hit path prints as UTF-8, not UnicodeEncodeError",
        b"caf\xc3\xa9_helper.py" in out,
        out[:120],
    )
    # prove the guard itself is what saves it: raw print under the same
    # ascii console raises while the reconfigured CLI emits bytes
    r = run_child([PY, "-c", "print('caf\u00e9_helper.py')"], TMP, env_extra={"PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}, text=False)
    check("console: raw ascii-console print really fails", r.returncode != 0, (r.stdout or r.stderr or b"")[:60])

    # ---- (2) git subprocess: utf-8 decode under an ascii locale ------------
    git = shutil.which("git")
    if git is None:
        check("git: git unavailable — suite may not prove decode", False, "git not on PATH in this env")
    else:
        g = TMP / "gitrepo"
        g.mkdir()
        for cmd in (
            ["init", "-b", "main"],
            ["config", "user.name", "portability-suite"],
            ["config", "user.email", "suite@example.invalid"],
            ["config", "core.quotepath", "false"],
        ):
            subprocess.run([git, *cmd], cwd=g, check=True, capture_output=True, text=True)
        (g / "plain.py").write_text("def plain():\n    return 0\n", encoding="utf-8")
        (g / "café.py").write_text("def cafe():\n    return 1\n", encoding="utf-8")
        subprocess.run([git, "add", "."], cwd=g, check=True, capture_output=True, text=True)
        subprocess.run([git, "commit", "-m", "init"], cwd=g, check=True, capture_output=True, text=True)
        child = (
            "import sys; sys.path.insert(0, %r); "
            "import bake.gitinfo as gi; "
            "print(gi.churn(['plain.py', 'caf\\u00e9.py'], %r)); "
            "print(len(gi.head(%r) or ''))" % (str(REPO), str(g), str(g))
        )
        # forced-ASCII locale: pre-fix the text=True decode used the locale
        # codec — a UnicodeDecodeError that the broad except swallowed into
        # a silent "churn unavailable"; post-fix it decodes UTF-8 regardless
        r = run_child([PY, "-c", child], g, env_extra={"LC_ALL": "C", "PYTHONUTF8": "0"})
        out = (r.stdout or "").strip().splitlines()
        check(
            "git churn: non-ASCII path decodes under ascii locale",
            r.returncode == 0 and len(out) == 2 and out[0] == "[1.0, 1.0]",
            " ".join((r.stdout or "")[:120].split()),
        )
        check("git head: ascii-locale child still reads all hashes", len(out) == 2 and out[1] == "7", (r.stdout or "")[:60])
        # and the channels have not drifted the byte-identities
        r = run_child([PY, "-c", child], g, env_extra={"NEURONAV_CONFIG": ""})
        check("git churn: default environment identity", r.returncode == 0 and (r.stdout or "").strip().splitlines()[0] == "[1.0, 1.0]", (r.stdout or "")[:80])

    print(f"\n{len(FAILURES)} failure(s)")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()