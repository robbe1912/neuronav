# Shared Playwright page harness for the viz suites (issue #86 strand R9).
# Everything tests/test_viz.py and tools/qa_readability.py duplicated, in
# one place — the suites keep their own assertions verbatim; nothing here
# may weaken or drop a check:
#   serve()     no-cache loopback server on an EPHEMERAL port (issue #132:
#               concurrent viz gates can no longer collide on a fixed port,
#               and a rerun never trips over the previous run's TIME_WAIT
#               socket). The pre-#120 `reuse` double-bind option is gone:
#               every rider binds exclusively (qa_readability dropped its
#               allow_reuse_address shadow-bind, issue #120).
#   launch()    real system Chrome via channel="chrome" (no browser download).
#   open_page() 1600x900 page, favicon stubbed, graph.html loaded and
#               settled; optional console/pageerror capture (#98 class:
#               uncaught JS errors ride the pageerror channel even when no
#               console.error call is made).
#   boot_banner() line-1 provenance stamp: which config won, which state
#               dir, and the graph.html size/mtime about to be served —
#               a wrong-bake run is visible before the first check (#89
#               class; CodeRabbit on #145).
#   CheckLog    the check(name, cond, detail) accumulator with the suite's
#               summary/exit contract: every failed check named at the end,
#               exit 1 on any.
# Import: test_viz gets this via sys.path[0] (tests/); qa_readability via
# the repo root on sys.path (`tests._page_harness`).
import http.server
import os
import socket
import socketserver
import sys
import threading
import time
from functools import partial
from pathlib import Path

VIEWPORT = {"width": 1600, "height": 900}
SETTLE_MS = 4000  # frozen layout — settle only, no animation dependence


def _active_config_label() -> str:
    """Best-effort mirror of nav's documented config resolution order:
    $NEURONAV_CONFIG wins; else <cwd>/.neuronav/config.json; else the
    checkout config.json (only when cwd IS the checkout); else defaults.
    The STATE_DIR the banner prints beside it is nav's actual resolution —
    ground truth; this label is context."""
    env = os.environ.get("NEURONAV_CONFIG")
    if env:
        return f"NEURONAV_CONFIG={env}"
    local = Path.cwd() / ".neuronav" / "config.json"
    if local.exists():
        return f"auto: {local}"
    checkout = Path(__file__).resolve().parents[1] / "config.json"
    if Path.cwd() == checkout.parent and checkout.exists():
        return f"auto: {checkout}"
    return "auto: nav defaults (no env, no config file)"


def boot_banner(directory) -> None:
    """Line-1 provenance for every gate log: config + state dir + bake
    stat. An exported override or a stale/missing bake must be visible
    BEFORE any check runs."""
    bake = Path(directory) / "graph.html"
    try:
        st = bake.stat()
        bake_s = f"graph.html {st.st_size} B, mtime {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}"
    except OSError:
        bake_s = "graph.html MISSING — regenerate the bake before gating (stale-bake trap)"
    print(f"[page-harness] config={_active_config_label()} state_dir={Path(directory).resolve()} · {bake_s}", flush=True)


def port_owner_hint(port: int) -> str:
    """Who-owns-this-port remedy for a refused bind, per OS — mirrors
    tools/serve.py (issue #51): the PowerShell cmdlet only exists on
    Windows."""
    if sys.platform == "win32":
        return f"Get-NetTCPConnection -LocalPort {port}"
    return f"lsof -i :{port}  (or: ss -ltnp)"


def serve(directory, port: int = 0):
    """No-cache HTTP server for `directory` on a loopback port.

    `port` defaults to 0 — the ephemeral-port law (#145/#132): gates
    self-select a free port and may run concurrently. An explicit port
    exists for diagnosis and the bind-refusal contract (#123): a taken
    port must fail LOUDLY, never as a raw traceback — SO_EXCLUSIVEADDRUSE
    on Windows (the serve.py issue #40 pattern) also stops a second
    listener from silently shadowing the first. The pre-#120 `reuse`
    option (qa_readability's allow_reuse_address shadow-bind) is gone:
    an orphaned prior run must fail the next bind loudly, never
    silently keep serving a stale bake.
    Teardown is the caller's: httpd.shutdown() then httpd.server_close().
    """
    boot_banner(directory)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    # exclusive-bind dance (issue #40): construct dormant, arm the socket
    # option, then bind/activate explicitly, so a taken port raises
    # EADDRINUSE instead of shadowing.
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler,
                                   bind_and_activate=False)
    try:
        if sys.platform == "win32":
            httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        httpd.server_bind()
        httpd.server_activate()
    except OSError as exc:
        httpd.server_close()
        if port:
            remedy = f"Find who owns it: {port_owner_hint(port)}"
        else:
            remedy = ("no ephemeral loopback port could be bound — is "
                      "another gate run exhausting the socket table?")
        print(f"refusing to serve: 127.0.0.1:{port} bind failed "
              f"({exc.strerror or exc}). {remedy}", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def require_fresh_bake(state_dir) -> None:
    """Issue #89 stale-bake refusal. The harness is read-only: it serves
    and measures the ACTIVE config's bake, never the template. A bake
    older than viz.py would green-light yesterday's product — refuse
    loudly and name the remedy. Never an auto-bake: baking writes to the
    store, and the store the gate serves must be exactly what the runner
    baked (the NEURONAV_CONFIG scratch-config law, tests/AGENTS.md)."""
    bake = Path(state_dir) / "graph.html"
    template = Path(__file__).resolve().parents[1] / "viz.py"
    try:
        bake_mtime = bake.stat().st_mtime
    except OSError:
        sys.exit(
            f"page-harness: no graph.html in {Path(state_dir).resolve()} — bake first:\n"
            f"  NEURONAV_CONFIG=<scratch-store config> python -X utf8 viz.py\n"
            f"(config law: point NEURONAV_CONFIG at a scratch store per process;\n"
            f" CI bakes via tests/vizcorpus_build.py — never bake a live store)")
    if bake_mtime < template.stat().st_mtime:
        sys.exit(
            f"page-harness: STALE BAKE — {bake}\n"
            f"  graph.html mtime {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(bake_mtime))}"
            f" predates viz.py mtime"
            f" {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(template.stat().st_mtime))}.\n"
            f"  This gate would measure an old bake of the product. Bake now:\n"
            f"  NEURONAV_CONFIG={os.environ.get('NEURONAV_CONFIG', '<unset>')}"
            f" python -X utf8 viz.py\n"
            f"  (CI: re-run tests/vizcorpus_build.py into the corpus dest.)\n"
            f"  The harness stays read-only (#89) — it will not auto-bake.")


def launch(pw):
    """Real system Chrome — channel="chrome" (no browser download).
    Falls back to the Playwright-bundled chromium only when no system
    Chrome exists (CI runners): same Blink engine, same checks. The
    banner notes which one answered so gate logs stay honest."""
    try:
        return pw.chromium.launch(channel="chrome", headless=True)
    except Exception:
        print("[page-harness] system Chrome not found — "
              "falling back to bundled chromium", flush=True)
        return pw.chromium.launch(headless=True)


def open_page(browser, port: int, errors: list | None = None):
    """New VIEWPORT page on the favicon-stubbed loopback origin, graph.html
    loaded and settled. Pass `errors` to collect console errors AND uncaught
    page errors for the session-wide #98 watchdog."""
    page = browser.new_page(viewport=VIEWPORT)
    page.route("**/favicon.ico", lambda r: r.fulfill(status=200, body=""))
    if errors is not None:
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{port}/graph.html", wait_until="load")
    # [#123] readiness over a blanket sleep: quiesce on __dbg.settled when
    # the bake exposes the getter; a bake from before it keeps the
    # historic fixed settle (qa_readability may serve older bakes).
    if page.evaluate("() => window.__dbg && typeof window.__dbg.settled === 'boolean'"):
        quiesce(page)
    else:
        page.wait_for_timeout(SETTLE_MS)
    return page


def quiesce(page, timeout: int = 8000):
    """[#123] readiness/quiescence wait: camera tween done, compaction
    done, node alpha + hover-scale arrays at target (viz's __dbg.settled —
    same snap thresholds the tick loop eases with). Replaces blanket
    sleeps after camera/focus actions; raises on timeout because a
    never-settling page is a failure, never a skip."""
    page.wait_for_function(
        "() => window.__dbg && window.__dbg.settled === true",
        timeout=timeout)


def probe_dbg(page) -> bool:
    """window.__dbg boot probe — False means a broken bake."""
    return bool(page.evaluate("() => !!window.__dbg"))


class CheckLog:
    """check(name, cond, detail) accumulator + summary/exit contract.

    [#123] executed-check floor: `floor` (set by the suite once the
    data-shape is known) is the minimum number of checks that MUST
    execute. A render regression that destroys a section's precondition
    turns its checks into skips; without the floor the suite goes green
    while testing nothing. finish() fails the run on an executed shortfall
    — loud named SKIPs stay reserved for genuine data-gates."""

    def __init__(self):
        self.failures = []
        self.executed = 0
        self.floor = 0

    def check(self, name, cond, detail=""):
        self.executed += 1
        tag = "PASS" if cond else "FAIL"
        print(f"{tag} {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            self.failures.append(name)

    def finish(self, tail: str = "") -> None:
        print(f"\n{tail} · executed {self.executed} checks "
              f"(floor {self.floor}) · {len(self.failures)} failure(s)")
        if self.floor and self.executed < self.floor:
            print(f"FLOOR BREACH: executed {self.executed} checks, floor is "
                  f"{self.floor} — a section's precondition vanished (see the "
                  f"SKIP lines above). The suite does not go green by testing "
                  f"less (#123); the floor is pinned to the frozen CI corpus "
                  f"(tests/AGENTS.md).")
            sys.exit(1)
        if self.failures:
            print("FAILED:", ", ".join(self.failures))
            sys.exit(1)
        print("ALL TESTS PASS")
