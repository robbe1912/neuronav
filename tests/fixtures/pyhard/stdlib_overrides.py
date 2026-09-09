# dead-scan tier fixture: handler subclass overriding stdlib hooks.
# http.server calls end_headers/log_message/do_* reflectively on the
# subclass — no static caller can exist inside the repo. The unrelated
# method (scratch_helper) is the control: it must STAY likely-dead.
import http.server


class Quiet(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)

    def scratch_helper(self):
        return 1
