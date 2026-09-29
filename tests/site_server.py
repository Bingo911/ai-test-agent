"""Local HTTP fixture site for the executor and acceptance tests.

`file:` navigation is forbidden by the egress policy (§14.2), so the tests need a real origin on
loopback. `/api/status` exists so the network evidence ring has something to record.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent / "site"


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api/status"):
            payload = json.dumps({"state": "服务正常"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, *_args) -> None:
        """Keep the pytest output readable."""


class SiteServer:
    def __init__(self, directory: Path = SITE_DIR) -> None:
        handler = partial(_Handler, directory=str(directory))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> SiteServer:
        self.thread.start()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
