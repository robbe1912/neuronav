# Shared Playwright page harness for the viz suites (issue #86 strand R9).
# Everything tests/test_viz.py and tools/qa_readability.py duplicated, in
# one place — the suites keep their own assertions verbatim; nothing here
# may weaken or drop a check:
#   serve()     no-cache loopback server on an EPHEMERAL port (issue #132:
#               concurrent viz gates can no longer collide on a fixed port,
#               and a rerun never trips over the previous run's TIME_WAIT
#               socket). The historic port-bind difference between the two
#               suites survives as the `reuse` option.
#   launch()    real system Chrome via channel="chrome" (no browser download).
#   open_page() 1600x900 page, favicon stubbed, graph.html loaded and
#               settled; optional console/pageerror capture (#98 class:
#               uncaught JS errors ride the pageerror channel even when no
#               console.error call is made).
#   probe_dbg() window.__dbg boot probe — False means a broken bake.
#   CheckLog    the check(name, cond, detail) accumulator with the suite's
#               summary/exit contract: every failed check named at the end,
#               exit 1 on any.
# Import: test_viz gets this via sys.path[0] (tests/); qa_readability via
# the repo root on sys.path (`tests._page_harness`).
import http.server
import socketserver
import sys
import threading
from functools import partial

VIEWPORT = {"width": 1600, "height": 900}
SETTLE_MS = 4000  # frozen layout — settle only, no animation dependence


class _ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def serve(directory, reuse: bool = False):
    """No-cache HTTP server for `directory` on an ephemeral loopback port.

    Returns (httpd, port). `reuse` preserves the historic bind difference:
    qa_readability bound with allow_reuse_address, test_viz without.
    Teardown is the caller's: httpd.shutdown() then httpd.server_close().
    """
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    cls = _ReuseTCPServer if reuse else socketserver.TCPServer
    httpd = cls(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def launch(pw):
    """Real system Chrome — channel="chrome" (no browser download)."""
    return pw.chromium.launch(channel="chrome", headless=True)


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
    page.wait_for_timeout(SETTLE_MS)
    return page


def probe_dbg(page) -> bool:
    """window.__dbg boot probe — False means a broken bake."""
    return bool(page.evaluate("() => !!window.__dbg"))


class CheckLog:
    """check(name, cond, detail) accumulator + summary/exit contract."""

    def __init__(self):
        self.failures = []

    def check(self, name, cond, detail=""):
        tag = "PASS" if cond else "FAIL"
        print(f"{tag} {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            self.failures.append(name)

    def finish(self, tail: str = "") -> None:
        print(f"\n{tail} · {len(self.failures)} failure(s)")
        if self.failures:
            print("FAILED:", ", ".join(self.failures))
            sys.exit(1)
        print("ALL TESTS PASS")
