# Hermetic smoke for the QA gate + the bake-only serve.py (issues #120, #124-3).
#
# tools/qa_readability.py gates every visualizer change yet had ZERO coverage
# (issue #124 item 3): it could rot or crash and the gate would silently stop
# gating. This suite closes that hole hermetically — own corpus, own store,
# own bake, FAKE embeds, machine-temp scratch only, never a live store:
#
# Leg A (stdlib, always runs) — serve.py bake-only surface (issue #120):
#   /graph.html answers with the on-disk bytes under no-cache; /chroma/...,
#   /base/... and every other route 404 (structural allowlist — no path but
#   the bake ever reaches the filesystem); non-loopback Host headers 403
#   (DNS-rebind kill); an absent Host header is refused too. The port
#   refusal contract lives in test_project_mode (issue #40/#51 pins).
#
# Leg B (needs playwright + Chrome; LOUD failure/skip, never silent) — the
#   declutter battery over a tiny hermetic bake: --declutter exits 0 and the
#   baseline report shape is pinned (identity block + every subject x angle
#   x GATE_KEYS cell); --after against it exits 0; a baseline whose identity
#   was tampered with is REFUSED nonzero naming BOTH identities; a
#   schema-mismatched and an identity-less (pre-#120) baseline are refused,
#   never silently degraded; bake_data_sha ignores the volatile meta stamps
#   but moves on real data changes; probe() exits 2 on exhausted retries;
#   an orphan-held port makes the harness serve() bind (the QA server's
#   bind, issue #120) fail loudly with the port-owner hint.
#
# Run: .venv/Scripts/python.exe -X utf8 tests/test_qa_smoke.py
import contextlib
import http.client
import importlib.util
import inspect
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PY = sys.executable
FAILURES = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --- shared helpers ----------------------------------------------------------

def _fetch(port, path, host=None):
    """Raw GET. host: None = auto (127.0.0.1:port), '' = omit, else override."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest("GET", path, skip_host=host is not None)
        if host:
            c.putheader("Host", host)
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read(), r.headers
    finally:
        c.close()


def _wait_listening(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _child_env(cfg, extra=None):
    """Subprocess env: scratch config per process, never a shell export."""
    env = {k: v for k, v in os.environ.items() if k != "NEURONAV_CONFIG"}
    env.update(NEURONAV_CONFIG=str(cfg), NEURONAV_EMBED_FAKE="1",
               PYTHONIOENCODING="utf-8")
    if extra:
        env.update(extra)
    return env


# --- leg A: serve.py is bake-only + loopback-host-guarded (issue #120) -------

def leg_a_serve():
    print("== leg A: serve.py bake-only surface (issue #120) ==")
    with tempfile.TemporaryDirectory(prefix="nn_qasmoke_a_") as td:
        tmp = Path(td)
        state = tmp / "state"
        state.mkdir()
        bake = b"<!doctype html><html><body>smoke bake</body></html>\n"
        (state / "graph.html").write_bytes(bake)
        # decoy shards: the state dir's real residents must NEVER be reachable
        (state / "chroma").mkdir()
        (state / "chroma" / "store.bin").write_bytes(b"chroma-secret")
        (state / "base").mkdir()
        (state / "base" / "shard-0.bin").write_bytes(b"shard-secret")
        cfg = tmp / "config.json"
        cfg.write_text(json.dumps({
            "root": str(tmp), "collection": "qasmoke", "include_dirs": ["."],
            "extensions": [".gd"], "exclude_dirs": [".git"],
            "state_dir": str(state)}), encoding="utf-8", newline="\n")

        with socket.socket() as s:  # pick a free port, then hand it to serve.py
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen(
            [PY, "-X", "utf8", str(ROOT / "tools" / "serve.py"),
             "--port", str(port)],
            cwd=str(ROOT), env=_child_env(cfg),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if not _wait_listening(port):
                err = proc.stderr.read().decode("utf-8", "replace") if proc.poll() is not None else ""
                check("serve: instance boots on a free loopback port", False, err[:120])
                return
            check("serve: instance boots on a free loopback port", True)

            st, body, hdrs = _fetch(port, "/graph.html")
            check("serve: /graph.html answers 200", st == 200, f"status {st}")
            check("serve: served bytes are the on-disk bake", body == bake)
            check("serve: responses carry no-store",
                  "no-store" in (hdrs.get("Cache-Control") or ""),
                  hdrs.get("Cache-Control") or "(none)")

            st, _, _ = _fetch(port, "/graph.html?cachebust=1")
            check("serve: query strings stay on the bake route", st == 200,
                  f"status {st}")
            for path in ("/chroma/store.bin", "/base/shard-0.bin", "/",
                         "/index.html", "/graph.html/../chroma/store.bin"):
                st, body2, _ = _fetch(port, path)
                check(f"serve: {path} -> 404 (bake-only allowlist)",
                      st == 404 and b"secret" not in body2, f"status {st}")

            st, _, _ = _fetch(port, "/graph.html", host=f"localhost:{port}")
            check("serve: Host localhost is allowed", st == 200, f"status {st}")
            st, _, _ = _fetch(port, "/graph.html", host=f"rebind.example.com:{port}")
            check("serve: DNS-rebind Host is refused 403", st == 403,
                  f"status {st}")
            st, _, _ = _fetch(port, "/chroma/store.bin",
                              host=f"rebind.example.com:{port}")
            check("serve: rebind Host refused on other routes too",
                  st in (403, 404), f"status {st}")
            st, _, _ = _fetch(port, "/graph.html", host="")
            check("serve: absent Host header refused", st == 403,
                  f"status {st}")
        finally:
            proc.terminate()
            proc.wait(timeout=15)

        # no-bake refusal: a loud, well-formed 404 - never a dropped
        # connection (GK F1: non-ASCII reason phrases crash latin-1 encode)
        nobake = tmp / "nobake"
        nobake.mkdir()
        cfg2 = tmp / "config-nobake.json"
        cfg2.write_text(json.dumps({
            "root": str(tmp), "collection": "qasmoke", "include_dirs": ["."],
            "extensions": [".gd"], "exclude_dirs": [".git"],
            "state_dir": str(nobake)}), encoding="utf-8", newline="\n")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port2 = s.getsockname()[1]
        proc2 = subprocess.Popen(
            [PY, "-X", "utf8", str(ROOT / "tools" / "serve.py"),
             "--port", str(port2)],
            cwd=str(ROOT), env=_child_env(cfg2),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if not _wait_listening(port2):
                check("serve: no-bake instance boots", False)
                return
            try:
                st, body, _ = _fetch(port2, "/graph.html")
            except Exception as exc:  # dropped connection = the F1 crash
                check("serve: missing bake -> loud 404, connection lives",
                      False, f"connection dropped: {type(exc).__name__}")
            else:
                check("serve: missing bake -> loud 404, connection lives",
                      st == 404 and b"not baked" in body
                      and b"rescan+bake" in body,
                      f"status {st}")
        finally:
            proc2.terminate()
            proc2.wait(timeout=15)


# --- leg B: the declutter battery over a hermetic bake (#120 + #124-3) -------

CORPUS = {
    "hub.gd": 'class_name QaHub\n'
              'extends Node\n'
              '# smoke hub: fans ticks to the satellites, collects reports.\n'
              '\n'
              'const SatAScene = preload("res://sat_a.gd")\n'
              'const SatBScene = preload("res://sat_b.gd")\n'
              '\n'
              'func boot() -> void:\n'
              '\tvar a: QaSatA = SatAScene.new()\n'
              '\tvar b: QaSatB = SatBScene.new()\n'
              '\ta.start()\n'
              '\tb.start()\n'
              '\n'
              'func tick(_delta: float) -> void:\n'
              '\tvar a: QaSatA = SatAScene.new()\n'
              '\ta.step()\n'
              '\n'
              'func collect(_tag: String) -> int:\n'
              '\treturn 0\n',
    "sat_a.gd": 'class_name QaSatA\n'
                'extends Node\n'
                '# smoke satellite A: steps work, reports to the hub.\n'
                '\n'
                'const HubScene = preload("res://hub.gd")\n'
                '\n'
                'func start() -> void:\n'
                '\tpass\n'
                '\n'
                'func step() -> void:\n'
                '\tvar h: QaHub = HubScene.new()\n'
                '\th.collect("a")\n',
    "sat_b.gd": 'class_name QaSatB\n'
                'extends Node\n'
                '# smoke satellite B: steps work, reports to the hub.\n'
                '\n'
                'const HubScene = preload("res://hub.gd")\n'
                '\n'
                'func start() -> void:\n'
                '\tpass\n'
                '\n'
                'func step() -> void:\n'
                '\tvar h: QaHub = HubScene.new()\n'
                '\th.collect("b")\n',
    "main.gd": 'class_name QaMain\n'
               'extends Node\n'
               '# smoke entry: boots the hub and pumps ticks.\n'
               '\n'
               'const HubScene = preload("res://hub.gd")\n'
               '\n'
               'func _ready() -> void:\n'
               '\tvar h: QaHub = HubScene.new()\n'
               '\th.boot()\n'
               '\n'
               'func _process(_delta: float) -> void:\n'
               '\tvar h: QaHub = HubScene.new()\n'
               '\th.tick(0.0)\n',
    "extra.gd": 'class_name QaExtra\n'
                'extends Node\n'
                '# smoke leaf: one-shot calibration against the hub.\n'
                '\n'
                'const HubScene = preload("res://hub.gd")\n'
                '\n'
                'func calibrate() -> int:\n'
                '\tvar h: QaHub = HubScene.new()\n'
                '\treturn h.collect("cal")\n',
}


def _build_corpus(td):
    """5-file GDScript corpus (fixed bytes) + git history + config + FAKE
    store + bake — the vizcorpus pattern, miniature. Returns (cfg, state)."""
    proj = td / "proj"
    proj.mkdir()
    for name, src in CORPUS.items():
        (proj / name).write_text(src, encoding="utf-8", newline="\n")

    def git(*args):
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "smoke@invalid",
               "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "smoke@invalid",
               "GIT_AUTHOR_DATE": "2026-01-01T00:00:00",
               "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"}
        subprocess.run(["git", *args], cwd=proj, env=env, check=True,
                       capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "chore: smoke corpus")

    cfg = td / "config.json"
    cfg.write_text(json.dumps({
        "root": str(proj), "collection": "qasmoke", "include_dirs": ["."],
        "extensions": [".gd"], "exclude_dirs": [".git"],
        "state_dir": "default"}), encoding="utf-8", newline="\n")

    code = ("import nav, viz\n"
            "nav.rescan()\n"
            "viz.generate()\n"
            "print('files', nav.count())\n")
    r = subprocess.run([PY, "-X", "utf8", "-c", code], cwd=str(ROOT),
                       env=_child_env(cfg), capture_output=True, text=True,
                       timeout=300)
    check("qa: hermetic corpus + store + bake built", r.returncode == 0,
          (r.stderr or "")[-160:])
    return cfg, proj / ".neuronav"


def _run_qa(cfg, qadir, *flags, timeout=900):
    return subprocess.run(
        [PY, "-X", "utf8", str(ROOT / "tools" / "qa_readability.py"), *flags],
        cwd=str(ROOT), env=_child_env(cfg, {"NEURONAV_QA_DIR": str(qadir)}),
        capture_output=True, text=True, timeout=timeout)


def leg_b_battery():
    print("== leg B: declutter battery over a hermetic bake (#120, #124-3) ==")
    if os.environ.get("NEURONAV_QA_SMOKE_NO_BROWSER") == "1":
        print("SKIP leg B: NEURONAV_QA_SMOKE_NO_BROWSER=1 (no-browser matrix "
              "job) — the viz job runs the full battery leg")
        return
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        check("qa: battery leg needs playwright (browser gate)",
              False, f"playwright not installed ({exc}) — run on a "
                     "chrome-capable host (CI: the viz job runs this suite)")
        return
    with tempfile.TemporaryDirectory(prefix="nn_qasmoke_b_") as td:
        tmp = Path(td)
        cfg, state = _build_corpus(tmp)
        if not (state / "graph.html").is_file():
            return
        qadir = tmp / "qa"
        qadir.mkdir()

        # import the gate module in-process (rot check: it must import at
        # all) against the scratch profile — set BEFORE the nav import inside
        os.environ["NEURONAV_CONFIG"] = str(cfg)
        spec = importlib.util.spec_from_file_location(
            "qa_readability_smoke", ROOT / "tools" / "qa_readability.py")
        qa = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(qa)
        except Exception as exc:
            check("qa: gate module imports (can-rot hole, #124-3)", False,
                  repr(exc)[:160])
            return
        check("qa: gate module imports (can-rot hole, #124-3)", True)

        # --- bake_data_sha: stable across volatile stamps, moves on data ---
        html = (state / "graph.html").read_bytes()
        m = re.search(rb"const DATA = (\{.*\});", html)
        data = json.loads(m.group(1)) if m else {}
        check("qa: smoke bake has 5 nodes / >=6 edges",
              len(data.get("nodes", [])) == 5 and len(data.get("links", [])) >= 6,
              f"nodes={len(data.get('nodes', []))} links={len(data.get('links', []))}")
        sha = qa.bake_data_sha(html)
        check("qa: bake sha is a 16-hex digest",
              bool(re.fullmatch(r"[0-9a-f]{16}", sha)), sha)
        rebaked = re.sub(rb'"generated_at":"[^"]*"',
                         b'"generated_at":"2999-01-01T00:00:00"', html)
        rebaked = re.sub(rb'"git":"[^"]*"', b'"git":"fffff"', rebaked)
        check("qa: bake sha ignores volatile meta stamps",
              qa.bake_data_sha(rebaked) == sha)
        check("qa: bake sha moves on real data change",
              qa.bake_data_sha(html.replace(b"extra.gd", b"extra2.gd", 1)) != sha)
        try:
            qa.bake_data_sha(b"<html>no data payload</html>")
            check("qa: bake sha refuses non-bake bytes", False, "no exit")
        except SystemExit as e:
            check("qa: bake sha refuses non-bake bytes", e.code == 2,
                  f"code={e.code}")

        # --- served-bake identity helper over the live harness server ------
        from tests._page_harness import serve as harness_serve
        httpd, hport = harness_serve(str(state))
        try:
            ident = qa.served_bake_identity(hport)
            check("qa: served identity hashes the served bake",
                  ident["bake_sha"] == sha and ident["schema"] == qa.BATTERY_SCHEMA)
        finally:
            httpd.shutdown()
            httpd.server_close()

        # --- probe() exhaustion exits 2, never a flapping census -----------
        class FlapPage:
            n = 0

            def evaluate(self, _js, *_a):
                type(self).n += 1
                return {"n": type(self).n}  # never identical twice

            def wait_for_timeout(self, _ms):
                pass

        try:
            qa.probe(FlapPage())
            check("qa: probe exhaustion exits 2", False, "probe returned")
        except SystemExit as e:
            check("qa: probe exhaustion exits 2", e.code == 2, f"code={e.code}")

        # --- orphan-held port: the QA bind must fail loudly + hint --------
        sig = inspect.signature(harness_serve)
        if "port" not in sig.parameters:
            check("qa: orphan-held port refused loudly (harness bind)",
                  False, "this checkout's _page_harness.serve() predates the "
                         "exclusive-bind port kwarg (harness #123/#89 PR); "
                         "rebase on it — never pass silently")
        else:
            holder = socket.socket()
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            held = holder.getsockname()[1]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stderr(buf):
                    harness_serve(str(state), port=held)
                check("qa: orphan-held port bind exits 1", False,
                      "bind succeeded against a held port")
            except SystemExit as e:
                check("qa: orphan-held port bind exits 1", e.code == 1,
                      f"code={e.code}")
                err = buf.getvalue()
                hint = ("Get-NetTCPConnection" if sys.platform == "win32"
                        else "lsof")
                check("qa: refusal is loud: port + owner hint",
                      "refusing to serve" in err
                      and f"127.0.0.1:{held}" in err and hint in err,
                      err[:140])
            finally:
                holder.close()

        # --- the battery itself: --declutter exits 0, shape pinned ---------
        r = _run_qa(cfg, qadir, "--declutter")
        check("qa: --declutter over the hermetic bake exits 0",
              r.returncode == 0, (r.stdout + r.stderr)[-200:])
        tail = (r.stdout + r.stderr).strip().splitlines()
        print("    [declutter tail] " + " | ".join(tail[-2:]))
        base_path = qadir / "declutter_base.json"
        check("qa: --declutter wrote the baseline", base_path.is_file(),
              str(base_path))
        if r.returncode != 0 or not base_path.is_file():
            return
        doc = json.loads(base_path.read_text(encoding="utf-8"))
        ident = doc.get("identity")
        check("qa: baseline embeds an identity block", isinstance(ident, dict),
              repr(ident)[:80])
        if isinstance(ident, dict):
            check("qa: identity schema matches the battery",
                  ident.get("schema") == qa.BATTERY_SCHEMA, str(ident.get("schema")))
            check("qa: identity stamps the capture git sha",
                  bool(ident.get("git_sha")), repr(ident.get("git_sha")))
            check("qa: identity bake sha is 16-hex",
                  bool(re.fullmatch(r"[0-9a-f]{16}", ident.get("bake_sha") or "")),
                  repr(ident.get("bake_sha")))
            check("qa: identity hub roster == view subjects",
                  ident.get("hub_subjects") == sorted(doc["views"]),
                  f"{ident.get('hub_subjects')} vs {sorted(doc['views'])}")

        angles = {a for a, _ in qa.ANGLES}
        subjects = sorted(doc["views"])
        check("qa: battery covered global + all 5 corpus hubs",
              subjects[:1] == ["global"] and len(subjects) == 6, str(subjects))
        shape_ok, missing = True, []
        for subj in subjects:
            view = doc["views"][subj]
            if subj != "global":
                if not (view.get("_hub") or {}).get("p", "").endswith(".gd"):
                    shape_ok, missing = False, f"{subj}/_hub"
                    break
                view = {k: v for k, v in view.items() if not k.startswith("_")}
            if set(view) != angles:
                shape_ok, missing = False, f"{subj} angles {sorted(set(view))}"
                break
            for ang, mrec in view.items():
                for key, _label, _tol, _bar in qa.GATE_KEYS:
                    if qa.get_metric(mrec, key) is None:
                        shape_ok, missing = False, f"{subj}/{ang}/{key}"
                        break
                if not shape_ok:
                    break
            if not shape_ok:
                break
        check("qa: every subject x angle x GATE_KEYS cell is present",
              shape_ok, f"first missing: {missing}")

        # --- --after against the SAME bake: identity passes, gate exits 0 --
        r = _run_qa(cfg, qadir, "--after", "--base", str(base_path))
        check("qa: --after on the matching baseline exits 0",
              r.returncode == 0, (r.stdout + r.stderr)[-300:])
        check("qa: --after wrote the after report",
              (qadir / "declutter_after.json").is_file())

        # --- stale/tampered baselines are REFUSED nonzero, naming both ----
        real_sha = (ident or {}).get("bake_sha") or ""

        tampered = json.loads(base_path.read_text(encoding="utf-8"))
        tampered["identity"]["bake_sha"] = "deadbeefdeadbeef"
        tampered_path = qadir / "tampered_base.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        r = _run_qa(cfg, qadir, "--after", "--base", str(tampered_path),
                    timeout=180)
        out = r.stdout + r.stderr
        check("qa: tampered-identity baseline refused nonzero",
              r.returncode == 2, f"rc={r.returncode}")
        check("qa: refusal names BOTH identities",
              "REFUSED" in out and "deadbeefdeadbeef" in out and real_sha in out,
              out[:200])

        schema_bad = json.loads(base_path.read_text(encoding="utf-8"))
        schema_bad["identity"]["schema"] = qa.BATTERY_SCHEMA + 999
        schema_path = qadir / "schema_base.json"
        schema_path.write_text(json.dumps(schema_bad), encoding="utf-8")
        r = _run_qa(cfg, qadir, "--after", "--base", str(schema_path),
                    timeout=180)
        check("qa: schema-mismatched baseline refused nonzero",
              r.returncode == 2 and "REFUSED" in r.stdout + r.stderr,
              f"rc={r.returncode}")

        legacy = {k: v for k, v in doc.items() if k != "identity"}
        legacy_path = qadir / "legacy_base.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        r = _run_qa(cfg, qadir, "--after", "--base", str(legacy_path),
                    timeout=180)
        out = r.stdout + r.stderr
        check("qa: identity-less (pre-#120) baseline refused, not degraded",
              r.returncode == 2 and "REFUSED" in out and "null" in out,
              f"rc={r.returncode} " + out[:160])


if __name__ == "__main__":
    leg_a_serve()
    leg_b_battery()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL TESTS PASS")
