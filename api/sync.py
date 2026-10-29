"""POST /api/sync — Mini App шлёт сюда состояние игрока (монеты, сила тапа, заработано,
клики); в ответ может прийти {"grant": {...}} — накопленная админом выдача/списание."""
import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler

from _common import botmod

log = logging.getLogger("ratcombat.api.sync")


async def _handle(body: dict) -> tuple[dict, int]:
    await botmod.init_db()
    return await botmod.sync_logic(body)


class handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        for k, v in botmod.cors_headers().items():
            self.send_header(k, v)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        try:
            data, status = asyncio.run(_handle(body))
        except Exception:
            log.exception("sync failed")
            data, status = {"ok": False, "error": "server error"}, 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
