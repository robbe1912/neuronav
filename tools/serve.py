"""No-cache dev server for graph.html - kills the stale-build bug class.

Every response carries Cache-Control: no-store, so browsers always refetch.
Serves the ACTIVE config's state dir, so /graph.html is always the bake
that config owns (per-project state, issue #15).
Run: python tools/serve.py  (binds 127.0.0.1:8791)
"""
import functools
import http.server
import socketserver
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nav  # noqa: E402  (state dir of the active config holds the bake)

PORT = 8791


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


if __name__ == "__main__":
    handler = functools.partial(NoCacheHandler, directory=str(nav.STATE_DIR))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"serving no-cache on http://127.0.0.1:{PORT}")
        httpd.serve_forever()
