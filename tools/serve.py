"""No-cache dev server for graph.html - kills the stale-build bug class.

Every response carries Cache-Control: no-store, so browsers always refetch.
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
import functools
import http.server
import os
import socket
import socketserver
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


def main() -> int:
    parser = argparse.ArgumentParser(
        description="no-cache viewer for the active config's graph.html bake")
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
    import nav  # noqa: E402  (state dir of the active config holds the bake)

    handler = functools.partial(NoCacheHandler, directory=str(nav.STATE_DIR))

    # exclusive bind (issue #40): construct dormant, arm SO_EXCLUSIVEADDRUSE,
    # then bind/activate explicitly. TCPServer's default allow_reuse_address
    # is False, so a taken port raises EADDRINUSE; on Windows only
    # SO_EXCLUSIVEADDRUSE stops a second instance from silently shadowing
    # this listener. (Explicit calls, no server_bind override: the stdlib
    # calls it dynamically, which the self-index would flag as dead code.)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", args.port), handler,
                                            bind_and_activate=False)
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
            f"port — find who: Get-NetTCPConnection -LocalPort {args.port}",
            file=sys.stderr,
        )
        return 1
    with httpd:
        print(f"serving no-cache on http://127.0.0.1:{args.port}")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
