"""No-cache dev server for graph.html - kills the stale-build bug class.

Every response carries Cache-Control: no-store, so browsers always refetch.
Run: python tools/serve.py  (binds 127.0.0.1:8791, serves repo root)
"""
import http.server
import socketserver

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
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), NoCacheHandler) as httpd:
        print(f"serving no-cache on http://127.0.0.1:{PORT}")
        httpd.serve_forever()
