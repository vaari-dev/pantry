"""Serves a fake shop page for the browser tests."""

import contextlib
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


class Page(SimpleHTTPRequestHandler):
    """Every path is the fake shop's one page, `html`; its script reads the path."""

    html = ""

    def translate_path(self, path):
        return str(Path(__file__).with_name(self.html))

    def log_message(self, *_): ...


@contextlib.contextmanager
def serving(handler: type[Page]):
    if not os.environ.get("DISPLAY"):
        pytest.skip("needs a display: run under xvfb-run")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
