"""GET /api/health — проверка, что функция и база живы."""
import json
from http.server import BaseHTTPRequestHandler

from _common import botmod


class handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        for k, v in botmod.cors_headers().items():
            self.send_header(k, v)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())
