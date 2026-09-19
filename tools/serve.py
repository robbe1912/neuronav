"""No-cache dev server for graph.html - kills the stale-build bug class.

HEADLESS-DEV ONLY (issue #133): production opens the bake directly via
file:// — `.neuronav/graph.html` is fully self-contained and boots offline.
This server exists for browser-automation rigs that need no-cache HTTP.

Every response carries Cache-Control: no-store, so browsers always refetch.

BAKE-ONLY (issue #120): the server answers exactly one route, /graph.html,
re-read from disk on every request. The state dir also holds the chroma
store and the base/ embedding shards — nothing else is ever served and
there is no directory listing (the allowlist is structural: no path ever
reaches the filesystem but the bake itself). A Host-header allowlist
(127.0.0.1/localhost) kills DNS-rebinding pages that point an attacker
hostname at the loopback bind: the rebinding browser sends
`Host: attacker.example`, which is refused before anything is read.

Serves the ACTIVE config's state dir, so /graph.html is always the bake
that config owns (per-project state, issue #15).
Run: python tools/serve.py [--port 8791] [--config CONFIG]

The bind is EXCLUSIVE (issue #40): with allow_reuse_address (SO_REUSEADDR
semantics) a second instance on Windows "succeeds" while the FIRST listener
silently keeps the port — the browser keeps fetching a stale, possibly
other-project bake. SO_EXCLUSIVEADDRUSE makes the second bind fail loudly;
elsewhere the default EADDRINUSE already does.
"""
import argparse
import http.server
import os
import socket
import socketserver
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# DNS-rebind allowlist (issue #120): only loopback names may name this
# server. Ports are stripped first; IPv6 literals arrive bracketed.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

BAKE_ROUTE = "/graph.html"


def port_owner_hint(port: int) -> str:
    """Who-owns-this-port remedy for the bind refusal, per OS (issue #51):
    the PowerShell cmdlet only exists on Windows."""
    if sys.platform == "win32":
        return f"Get-NetTCPConnection -LocalPort {port}"
    return f"lsof -i :{port}  (or: ss -ltnp)"


class BakeServer(socketserver.ThreadingTCPServer):
    """Exclusive-bind threaded server carrying the one servable path.

    allow_reuse_address stays False (issue #40): a taken port must fail
    loudly, never silently shadow a first listener."""
    daemon_threads = True

    def __init__(self, address, handler, bake: Path):
        super().__init__(address, handler, bind_and_activate=False)
        self.bake = bake


class NoCacheHandler(http.server.BaseHTTPRequestHandler):
    """Serves the bake and nothing else (issue #120)."""

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[: host.find("]") + 1]
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _dispatch(self, head_only: bool) -> None:
        if not self._host_allowed():
            self.send_error(403, "loopback hosts only (DNS-rebind refused)")
            return
        if urllib.parse.urlsplit(self.path).path != BAKE_ROUTE:
            self.send_error(404, "bake-only server: /graph.html is the sole route")
            return
        try:
            data = self.server.bake.read_bytes()  # fresh bytes every request
        except OSError:
            self.send_error(404, "graph.html is not baked under the state dir"
                                 " - run a rescan+bake first")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self):
        self._dispatch(head_only=False)

    def do_HEAD(self):
        self._dispatch(head_only=True)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


def main() -> int:
    parser = argparse.ArgumentParser(
        description="headless-dev no-cache HTTP for the active config's "
                    "graph.html bake — /graph.html only (production opens "
                    "the file directly — see issue #133)")
    parser.add_argument("--port", type=int, default=8791,
                        help="port to bind on 127.0.0.1 (default: 8791)")
    parser.add_argument("--config", type=Path, default=None,
                        help="config profile to serve (NEURONAV_CONFIG equivalent;"
                             " set per-process only, never exported)")
    args = parser.parse_args()
    if args.config:
        # same law as `nav.py --config`: the env var must be in place BEFORE
        # nav binds its globals at import, and subprocesses/sibling modules
        # (graph.py) then agree on the profile — without the caller ever
        # exporting NEURONAV_CONFIG in a long-lived shell.
        os.environ["NEURONAV_CONFIG"] = str(args.config.resolve())
    import navconfig

    # exclusive bind (issue #40): construct dormant, arm SO_EXCLUSIVEADDRUSE,
    # then bind/activate explicitly. socketserver.TCPServer's default
    # allow_reuse_address is False, so a taken port raises EADDRINUSE; on
    # Windows only SO_EXCLUSIVEADDRUSE stops a second instance from silently
    # shadowing this listener. (Explicit calls, no server_bind override: the
    # stdlib calls it dynamically, which the self-index would flag as dead
    # code.)
    httpd = BakeServer(("127.0.0.1", args.port), NoCacheHandler,
                       bake=navconfig.STATE_DIR / "graph.html")
    try:
        if sys.platform == "win32":
            httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        httpd.server_bind()
        httpd.server_activate()
    except OSError as exc:
        httpd.server_close()
        print(
            f"refusing to start: 127.0.0.1:{args.port} is already in use "
            f"({exc.strerror or exc}). Another serve.py or viewer owns the "
            f"port — find who: {port_owner_hint(args.port)}",
            file=sys.stderr,
        )
        return 1
    with httpd:
        print(f"serving no-cache bake-only on "
              f"http://127.0.0.1:{args.port}/graph.html")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
