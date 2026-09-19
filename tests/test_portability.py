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
#       console prints hit paths instead of raising UnicodeEncodeError;
#   (4) trailing argv on a no-argument subcommand is rejected loudly
#       (issue #332): `nav.py rescan --config X` must never fall to the
#       pure-defaults walk that writes <cwd>/.neuronav.

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

import navconfig, navindex, navstore, nav



from harness import check, finish

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
    navindex.rescan()
    navindex.export_base()


def main() -> None:
    # ---- (1a) BOM'd config.json loads + BOM'd .neuroignore still prunes ---
    check("bom config: import binds the configured root", navconfig.ROOT == TMP, str(navconfig.ROOT))
    check("bom config: state_dir resolved", navconfig.STATE_DIR == TMP / "state", str(navconfig.STATE_DIR))
    check("bom neuroignore: first-line name intact, no \\ufeff prefix", "scr1" in navconfig.EXCLUDE_DIRS, str(navconfig.EXCLUDE_DIRS))
    check("bom neuroignore: \\ufeff-prefixed twin absent", "\\ufeffscr1" not in navconfig.EXCLUDE_DIRS, str(navconfig.EXCLUDE_DIRS))

    build_store()
    # the walk must actually reflect the BOM'd .neuroignore: scr1/skip.py is
    # excluded, the non-ASCII file is indexed under its real name
    ids = {r for r in navstore._collection().get()["ids"]}
    check("bom neuroignore: excluded dir never indexed", "src/scr1/skip.py" not in ids, str(sorted(ids)))
    check("bom config: non-ASCII file indexed intact", "src/café_helper.py" in ids, str(sorted(ids)))

    # fresh-process proofs: nav binds the BOM'd config from scratch, not
    # merely from this suite's in-process import
    r = run_child(
        [PY, "-X", "utf8", "-c", "import nav, navconfig, navstore, navindex, navconfig, navstore, navindex; print(navconfig.ROOT); print(navconfig.STATE_DIR); print('scr1' in navconfig.EXCLUDE_DIRS)"],
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
    m = navconfig.BASE_DIR / navindex.MANIFEST_NAME
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
    navconfig.use_config(cfg2)
    shutil.copytree(TMP / "state" / "base", navconfig.BASE_DIR)
    imp = navindex.import_base()
    check("bom manifest: import reads BOM'd manifest", imp.get("imported") == 2, str(imp))
    check("bom manifest: imported store populated", navstore.count() == 2, str(navstore.count()))
    r = run_child([PY, "-X", "utf8", "-c", "import nav, navconfig, navstore, navindex, navconfig, navstore, navindex; print(navstore.count())"], TMP, env_extra={"NEURONAV_CONFIG": str(cfg2)})
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
    navconfig.use_config(CFG)  # back to the store the CLI search will read
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

    # ---- #298: CLI query isolation + config edges --------------------------------
    # The --config form used to join sys.argv[2:] — which still carries the
    # config path and the word "search" — INTO the query, so every --config
    # search was quietly contaminated (bm25 hits on "json"/"search").
    _d298 = Path(tempfile.mkdtemp(prefix="n298port_"))
    (_d298 / "src").mkdir()
    (_d298 / "src" / "aaa.py").write_text(
        "def json_search_thing():\n    return 'json search config'\n", encoding="utf-8")
    _cfg298 = _d298 / "cfg.json"
    _cfg298.write_text(json.dumps({
        "root": str(_d298), "collection": "n298port", "state_dir": "default",
        "include_dirs": ["src"], "extensions": [".py"], "exclude_dirs": [],
    }), encoding="utf-8")
    _env298 = {"NEURONAV_CONFIG": str(_cfg298)}
    run_child([sys.executable, "-X", "utf8", "-c",
               "import sys; sys.path.insert(0, '.'); import nav, navconfig, navstore, navindex, navconfig, navstore, navindex; navindex.rescan()"],
              cwd=_d298, env_extra=_env298)
    _bad = run_child([sys.executable, "-X", "utf8", str(REPO / "nav.py"),
                      "--config", str(_cfg298), "search", "zzqxnopenterm"], cwd=_d298)
    _good = run_child([sys.executable, "-X", "utf8", str(REPO / "nav.py"),
                       "search", "zzqxnopenterm"], cwd=_d298, env_extra=_env298)
    _bad_hits = [l for l in _bad.stdout.splitlines() if "src=" in l]
    _good_hits = [l for l in _good.stdout.splitlines() if "src=" in l]
    check("#298 --config search query is not contaminated by the config path",
          _bad_hits == _good_hits and _bad_hits
          and all("src=vec" in l for l in _bad_hits),
          f"--config: {_bad_hits[:1]} env: {_good_hits[:1]}")

    _r298 = run_child([sys.executable, "-X", "utf8", "-c", "import nav"],
                      cwd=_d298, env_extra={"NEURONAV_CONFIG": ""})
    check("#298 present-but-empty NEURONAV_CONFIG aborts (#41 contract)",
          _r298.returncode != 0 and "empty" in _r298.stderr, _r298.stderr.strip()[:100])

    _pa, _pb = _d298 / "projA", _d298 / "projB"
    (_pa / "src").mkdir(parents=True)
    (_pa / "src" / "one.py").write_text("def portable():\n    return 1\n", encoding="utf-8")
    run_child([sys.executable, "-X", "utf8", str(REPO / "onboard.py"), "init"], cwd=_pa)
    _root298 = json.loads((_pa / ".neuronav" / "config.json").read_text(encoding="utf-8"))["root"]
    check("#298 scaffold pins root='.' (portable, #27 contract)", _root298 == ".", repr(_root298))
    shutil.move(str(_pa), str(_pb))
    _moved = run_child([sys.executable, "-X", "utf8", "-c",
                        "import sys; sys.path.insert(0, '.'); import nav, navconfig, navstore, navindex, navconfig, navstore, navindex; "
                        "navconfig.use_config(__import__('pathlib').Path(sys.argv[1])); "
                        "print(navconfig.ROOT)",
                        str(_pb / ".neuronav" / "config.json")], cwd=_d298)
    check("#298 moved config resolves root at its new home",
          _moved.returncode == 0 and _moved.stdout.strip() == str(_pb),
          _moved.stdout.strip() or _moved.stderr.strip()[:100])

    import chromadb  # noqa: E402  (drop legs: swallow NotFoundError only)
    class _StubClient:
        def __init__(self, exc):
            self._exc = exc
        def delete_collection(self, name):
            raise self._exc
    _real_client298 = navstore.client
    try:
        navstore.client = lambda: _StubClient(chromadb.errors.NotFoundError())
        import io as _io298, contextlib as _cx298
        _buf298 = _io298.StringIO()
        with _cx298.redirect_stdout(_buf298):
            nav._cli(["drop"])
        check("#298 drop: chroma NotFoundError still reads as 'not present'",
              _buf298.getvalue().count("not present") == 2
              and "dropped" not in _buf298.getvalue(), _buf298.getvalue()[:80])
        navstore.client = lambda: _StubClient(PermissionError("file is locked by another process"))
        try:
            nav._cli(["drop"])
            check("#298 drop: a locked/IO failure aborts loudly, names the collection", False, "no raise")
        except RuntimeError as e:
            check("#298 drop: a locked/IO failure aborts loudly, names the collection",
                  "drop:" in str(e) and navconfig.COLLECTION in str(e) and "locked" in str(e), str(e)[:120])
    finally:
        navstore.client = _real_client298
    shutil.rmtree(_d298, ignore_errors=True)

    # ---- (4) trailing-argv rejection (issue #332): `rescan --config X`
    # used to silently run the PURE-DEFAULTS rescan — walking the cwd
    # and writing <cwd>/.neuronav (the #91 wipe-door class reachable by
    # an argument-order slip). It must die loudly naming the token and
    # the global-prefix form, with no store created by the failed call.
    _d332 = TMP / "plain332"
    _d332.mkdir()
    (_d332 / "victim.py").write_text("x = 1\n", encoding="utf-8")
    _r332 = run_child(
        [PY, "-X", "utf8", str(REPO / "nav.py"),
         "rescan", "--config", str(CFG)],
        cwd=_d332,
    )
    check("#332 trailing --config: rc!=0, not a silent defaults run",
          _r332.returncode != 0, f"rc={_r332.returncode}")
    check("#332 trailing --config: names token + global-prefix form",
          "--config" in _r332.stderr
          and "global prefix" in _r332.stderr
          and "nav.py --config <path> rescan" in _r332.stderr,
          _r332.stderr.strip()[:120])
    check("#332 trailing --config: no <cwd>/.neuronav store created",
          not (_d332 / ".neuronav").exists(), str(_d332 / ".neuronav"))
    _ok332 = run_child(
        [PY, "-X", "utf8", str(REPO / "nav.py"),
         "--config", str(CFG), "count"],
        cwd=_d332,
    )
    check("#332 correct global-prefix form still runs",
          _ok332.returncode == 0, _ok332.stderr.strip()[:100])

    finish()


if __name__ == "__main__":
    main()