"""GET /api/top?metric=coins|power|earned&limit=3|10|100 — рейтинг игроков."""
import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from _common import botmod

log = logging.getLogger("ratcombat.api.top")


async def _handle(metric: str, limit: str) -> dict:
    await botmod.init_db()
    return await botmod.top_logic(metric, limit)


class handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        for k, v in botmod.cors_headers().items():
            self.send_header(k, v)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        metric = (qs.get("metric") or ["coins"])[0]
        limit = (qs.get("limit") or ["10"])[0]
        try:
            data = asyncio.run(_handle(metric, limit))
            status = 200
        except Exception:
            log.exception("top failed")
            data, status = {"ok": False, "error": "server error"}, 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
