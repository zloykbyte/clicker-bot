"""POST /api/webhook — сюда Telegram присылает апдейты (сообщения, нажатия кнопок)
вместо long-polling: на Vercel нет постоянного процесса, который мог бы поллить.

Нужно один раз после деплоя зарегистрировать этот адрес в Telegram — см.
scripts/vercel_setup.py и README (раздел «Деплой на Vercel»).
"""
import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

from aiogram.types import Update

from _common import botmod

log = logging.getLogger("ratcombat.api.webhook")


async def _process(data: dict) -> None:
    await botmod.init_db()
    update = Update.model_validate(data)
    await botmod.dp.feed_update(botmod.get_bot(), update)


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # Удобно для ручной проверки в браузере, что функция вообще отвечает.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "note": "Telegram webhook endpoint, use POST"}).encode())

    def do_POST(self) -> None:
        secret = os.environ.get("WEBHOOK_SECRET")
        if secret and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            self.send_response(401)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
            asyncio.run(_process(data))
        except Exception:
            # Telegram повторяет доставку при ошибке — отвечаем 200 в любом случае,
            # чтобы не получить шторм ретраев из-за единичного сбоя; ошибку просто логируем.
            log.exception("webhook update failed")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
