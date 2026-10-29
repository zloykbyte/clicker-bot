import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    Update,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Postgres: обязателен при хостинге на Vercel (там нет локальных файлов между запросами).
# Vercel Postgres сам кладёт строку подключения в POSTGRES_URL — DATABASE_URL для локального/своего Postgres.
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("POSTGRES_PRISMA_URL")
    or ""
)
# Адрес Mini App (страница из папки docs, опубликованная через GitHub Pages или Vercel).
# Должен быть https. Можно переопределить в .env: WEBAPP_URL=...
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://zloykbyte.github.io/clicker-bot/")
# Публичный https-адрес API этого бота (туннель, свой хостинг или домен на Vercel), его получает
# Mini App для рейтингов и учёта посещений. Без него игра работает, но рейтинг пуст.
API_PUBLIC_URL = (os.getenv("API_PUBLIC_URL") or "").rstrip("/")
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("PORT") or os.getenv("API_PORT") or 8080)
# Секрет для проверки вебхука Telegram (режим Vercel). Необязателен, но желателен.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or ""

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or 0)
except ValueError:
    ADMIN_ID = 0

# Часовой пояс проекта: по нему считаются «сегодня/вчера» и посещения.
TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(TZ_NAME)
except Exception:  # нет tzdata (Windows) — запасной вариант UTC+3
    TZ = timezone(timedelta(hours=3))
    TZ_NAME = "UTC+03:00"

ACTIVE_DAYS_NEEDED = 3   # столько разных дней
ACTIVE_WINDOW = 7        # за последние столько дней

router = Router()
dp = Dispatcher()
dp.include_router(router)
log = logging.getLogger("ratcombat")

_bot_singleton: Bot | None = None


def get_bot() -> Bot:
    """Общий экземпляр Bot — используется и локальным polling, и вебхуком на Vercel
    (там функция может переиспользоваться между вызовами, пока инстанс «тёплый»)."""
    global _bot_singleton
    if _bot_singleton is None:
        if not BOT_TOKEN:
            raise RuntimeError("Не задан BOT_TOKEN.")
        _bot_singleton = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _bot_singleton


# ---------- Время ----------

def now_local() -> datetime:
    return datetime.now(TZ)


def today_local():
    return now_local().date()


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso).astimezone(TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def fmt_num(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


# ---------- База данных (Postgres через asyncpg) ----------
# Старая таблица `users` (бывший классик-кликер) из старой SQLite-версии бота здесь
# никогда не создавалась и не читается — при переезде на Postgres она просто не существует.
#
# В serverless (Vercel) файлы между вызовами не сохраняются, поэтому вместо локального
# SQLite-файла используется внешний Postgres (Vercel Postgres/Neon/Supabase и т.п. — подходит
# любой, достаточно строки подключения в DATABASE_URL/POSTGRES_URL).

_pool: asyncpg.Pool | None = None
_schema_ready = False


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "Не задан DATABASE_URL (или POSTGRES_URL) — нужна Postgres-база. "
                "См. .env.example."
            )
        # statement_cache_size=0 — обязательно, если подключение идёт через PgBouncer
        # в режиме transaction pooling (так работает большинство serverless-Postgres,
        # включая Vercel Postgres/Neon): иначе asyncpg ловит ошибки с prepared statements.
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=0, max_size=5, statement_cache_size=0)
    return _pool


async def init_db() -> None:
    """Создаёт схему, если её ещё нет. Безопасно вызывать многократно (в т.ч. на каждый cold start)."""
    global _schema_ready
    if _schema_ready:
        return
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                tg_id     BIGINT PRIMARY KEY,
                name      TEXT   NOT NULL DEFAULT '',
                username  TEXT   NOT NULL DEFAULT '',
                coins     BIGINT NOT NULL DEFAULT 0,
                tap_power BIGINT NOT NULL DEFAULT 1,
                earned    BIGINT NOT NULL DEFAULT 0,
                taps      BIGINT NOT NULL DEFAULT 0,
                first_day TEXT   NOT NULL,
                last_seen TEXT   NOT NULL,
                adj_coins     BIGINT NOT NULL DEFAULT 0,
                adj_tap_power BIGINT NOT NULL DEFAULT 0,
                adj_energy    BIGINT NOT NULL DEFAULT 0
            )
            """
        )
        # На случай апгрейда с более ранней схемы без adj_*-колонок.
        await con.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS adj_coins BIGINT NOT NULL DEFAULT 0")
        await con.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS adj_tap_power BIGINT NOT NULL DEFAULT 0")
        await con.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS adj_energy BIGINT NOT NULL DEFAULT 0")
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS visits (
                tg_id BIGINT NOT NULL,
                day   TEXT   NOT NULL,
                PRIMARY KEY (tg_id, day)
            )
            """
        )
        await con.execute("CREATE INDEX IF NOT EXISTS idx_visits_day ON visits(day)")
    _schema_ready = True


async def record_sync(tg_id: int, name: str, username: str,
                      coins: int, tap_power: int, earned: int, taps: int) -> None:
    """Сохраняет состояние игрока и засчитывает посещение (один день — одно)."""
    day = today_local().isoformat()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                """
                INSERT INTO players (tg_id, name, username, coins, tap_power, earned, taps, first_day, last_seen)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (tg_id) DO UPDATE SET
                    name = EXCLUDED.name, username = EXCLUDED.username,
                    coins = EXCLUDED.coins, tap_power = EXCLUDED.tap_power,
                    earned = EXCLUDED.earned, taps = EXCLUDED.taps,
                    last_seen = EXCLUDED.last_seen
                """,
                tg_id, name, username, coins, tap_power, earned, taps, day, ts,
            )
            await con.execute(
                "INSERT INTO visits (tg_id, day) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                tg_id, day,
            )


TOP_COLUMNS = {"coins": "coins", "power": "tap_power", "earned": "earned"}
# Выдача/списание ресурсов из админки: публичное имя поля -> колонка с накопленной дельтой.
GRANT_FIELDS = {"coins": "adj_coins", "power": "adj_tap_power", "energy": "adj_energy"}
GRANT_LABELS = {"coins": "💰 Монеты", "power": "👆 Сила тапа", "energy": "⚡ Энергия"}


async def get_top(metric: str, limit: int) -> list[dict]:
    col = TOP_COLUMNS[metric]  # только из белого списка
    pool = await get_pool()
    async with pool.acquire() as con:
        # Администратор не участвует в рейтингах.
        if ADMIN_ID:
            rows = await con.fetch(
                f"SELECT tg_id, name, username, {col} AS value FROM players "
                f"WHERE tg_id != $1 ORDER BY {col} DESC, tg_id ASC LIMIT $2",
                ADMIN_ID, limit,
            )
        else:
            rows = await con.fetch(
                f"SELECT tg_id, name, username, {col} AS value FROM players "
                f"ORDER BY {col} DESC, tg_id ASC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]


async def adjust_player(tg_id: int, field: str, delta: int) -> bool:
    """Копит выдачу/списание ресурса для игрока; применится на его стороне при синхронизации."""
    col = GRANT_FIELDS[field]
    pool = await get_pool()
    async with pool.acquire() as con:
        exists = await con.fetchval("SELECT 1 FROM players WHERE tg_id = $1", tg_id)
        if not exists:
            return False
        await con.execute(f"UPDATE players SET {col} = {col} + $1 WHERE tg_id = $2", delta, tg_id)
        return True


async def take_grant(tg_id: int) -> dict:
    """Забирает и обнуляет накопленные выдачи игрока — вызывается при его синхронизации."""
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT adj_coins, adj_tap_power, adj_energy FROM players WHERE tg_id = $1",
            tg_id,
        )
        if not row:
            return {}
        grant = dict(row)
        if any(grant.values()):
            await con.execute(
                "UPDATE players SET adj_coins = 0, adj_tap_power = 0, adj_energy = 0 WHERE tg_id = $1",
                tg_id,
            )
            return grant
        return {}


async def get_player(tg_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM players WHERE tg_id = $1", tg_id)
        if not row:
            return None
        p = dict(row)
        p["visit_days"] = await con.fetchval("SELECT COUNT(*) FROM visits WHERE tg_id = $1", tg_id)
        return p


async def get_stats() -> dict:
    today = today_local()
    d = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    pool = await get_pool()
    async with pool.acquire() as con:
        total = await con.fetchval("SELECT COUNT(*) FROM players")
        new = {
            "today": await con.fetchval("SELECT COUNT(*) FROM players WHERE first_day = $1", d(0)),
            "yesterday": await con.fetchval("SELECT COUNT(*) FROM players WHERE first_day = $1", d(1)),
            "week": await con.fetchval("SELECT COUNT(*) FROM players WHERE first_day >= $1", d(6)),
            "month": await con.fetchval("SELECT COUNT(*) FROM players WHERE first_day >= $1", d(29)),
        }
        seen = {
            "today": await con.fetchval("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day = $1", d(0)),
            "yesterday": await con.fetchval("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day = $1", d(1)),
            "week": await con.fetchval("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day >= $1", d(6)),
            "month": await con.fetchval("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day >= $1", d(29)),
        }
        active = await con.fetchval(
            "SELECT COUNT(*) FROM (SELECT tg_id FROM visits WHERE day >= $1 "
            "GROUP BY tg_id HAVING COUNT(DISTINCT day) >= $2) sub",
            d(ACTIVE_WINDOW - 1), ACTIVE_DAYS_NEEDED,
        )
    return {"total": total, "new": new, "seen": seen,
            "active": active, "inactive": max(0, total - active)}


async def get_activity() -> dict:
    today = today_local()
    days = [(today - timedelta(days=n)) for n in range(ACTIVE_WINDOW - 1, -1, -1)]
    pool = await get_pool()
    async with pool.acquire() as con:
        per_day = []
        for day in days:
            cnt = await con.fetchval(
                "SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day = $1", day.isoformat()
            )
            per_day.append((day, cnt))
        rows = await con.fetch(
            "SELECT c, COUNT(*) AS n FROM (SELECT COUNT(DISTINCT day) AS c FROM visits "
            "WHERE day >= $1 GROUP BY tg_id) sub GROUP BY c",
            days[0].isoformat(),
        )
        dist = {r["c"]: r["n"] for r in rows}
    return {"per_day": per_day, "dist": dist}


# ---------- Проверка данных Mini App ----------

def validate_init_data(init_data: str, max_age: int = 2 * 86400) -> dict | None:
    """Проверяет подпись Telegram (initData) и возвращает данные пользователя."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash", "")
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if isinstance(user.get("id"), int) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def clean_int(value, hi: int) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(f) or f < 0:
        return 0
    return int(min(f, hi))


# ---------- HTTP API для Mini App ----------
# Основная логика здесь не зависит от веб-фреймворка: её вызывают и aiohttp-обёртки ниже
# (для локального запуска/своего хостинга), и serverless-функции в api/*.py (для Vercel).

_last_sync: dict[int, float] = {}


def cors_headers() -> dict:
    origin = urlparse(WEBAPP_URL)
    allowed = f"{origin.scheme}://{origin.netloc}"
    return {
        "Access-Control-Allow-Origin": allowed,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Cache-Control": "no-store",
        "Vary": "Origin",
    }


async def sync_logic(body: dict) -> tuple[dict, int]:
    user = validate_init_data(str(body.get("initData", "")))
    if not user:
        return {"ok": False, "error": "bad auth"}, 401
    uid = user["id"]
    now = time.monotonic()
    if now - _last_sync.get(uid, 0) < 3:
        return {"ok": True, "throttled": True}, 200
    _last_sync[uid] = now
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)[:64]
    username = str(user.get("username") or "")[:64]
    await record_sync(
        uid, name or username or str(uid), username,
        clean_int(body.get("coins"), 10**18),
        max(1, clean_int(body.get("tapPower"), 10**12)),
        clean_int(body.get("earned"), 10**18),
        clean_int(body.get("taps"), 10**12),
    )
    resp: dict = {"ok": True}
    grant = await take_grant(uid)
    if grant:
        resp["grant"] = {
            "coins": grant.get("adj_coins", 0),
            "tapPower": grant.get("adj_tap_power", 0),
            "energy": grant.get("adj_energy", 0),
        }
    return resp, 200


async def top_logic(metric: str, limit_raw: str) -> dict:
    if metric not in TOP_COLUMNS:
        metric = "coins"
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 10
    limit = limit if limit in (3, 10, 100) else 10
    rows = await get_top(metric, limit)  # всегда свежие данные из базы
    items = [
        {"rank": i + 1, "name": r["name"] or r["username"] or "Игрок", "value": r["value"]}
        for i, r in enumerate(rows)
    ]
    return {"metric": metric, "limit": limit, "items": items,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def build_api():
    """aiohttp-приложение для локального запуска/своего хостинга (не нужно на Vercel)."""
    from aiohttp import web

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors_headers())
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        resp.headers.update(cors_headers())
        return resp

    async def api_health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def api_sync(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        data, status = await sync_logic(body)
        return web.json_response(data, status=status)

    async def api_top(request: web.Request) -> web.Response:
        data = await top_logic(request.query.get("metric", "coins"), request.query.get("limit", "10"))
        return web.json_response(data)

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/health", api_health)
    app.router.add_post("/api/sync", api_sync)
    app.router.add_get("/api/top", api_top)
    return app


# ---------- Mini App ссылки ----------

def webapp_url(tab: str | None = None) -> str:
    params = {}
    if API_PUBLIC_URL:
        params["api"] = API_PUBLIC_URL
    if tab:
        params["tab"] = tab
    if not params:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + urlencode(params)


def webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🐀 Играть", web_app=WebAppInfo(url=webapp_url()))]
        ]
    )


# ---------- Обработчики пользователей ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = html.escape(message.from_user.first_name or "друг")
    text = (
        f"🐀 <b>Привет, {name}!</b>\n\n"
        "Добро пожаловать в <b>Rat Combat</b>!\n\n"
        "👆 Тапай по крысе и зарабатывай <b>RatCoin</b>: каждый тап даёт +1\n"
        "⚡ Энергия восстанавливается сама\n"
        "🧀 Прокачивай норку и получай доход каждый час\n"
        "🏆 Расти в лигах и зови друзей\n\n"
        "Жми «Играть» 👇"
    )
    await message.answer(text, reply_markup=webapp_keyboard())


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Открыть рейтинг",
                                  web_app=WebAppInfo(url=webapp_url("top")))]
        ]
    )
    await message.answer(
        "🏆 <b>Рейтинг Rat Combat</b>\n\nТоп по монетам, силе клика и общему заработку: "
        "топ-3, топ-10 и топ-100.",
        reply_markup=kb,
    )


# ---------- Админка (обычные сообщения Telegram) ----------

_last_search: dict[int, int] = {}
_pending_grant: dict[int, tuple[int, str]] = {}  # admin_id -> (tg_id, field)


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


def admin_kb(view: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
             InlineKeyboardButton(text="🏆 Топы", callback_data="adm:tops")],
            [InlineKeyboardButton(text="👥 Игроки", callback_data="adm:players"),
             InlineKeyboardButton(text="📈 Активность", callback_data="adm:activity")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm:{view}"),
             InlineKeyboardButton(text="✖ Закрыть", callback_data="adm:close")],
        ]
    )


def player_label(p: dict) -> str:
    name = html.escape(p.get("name") or p.get("username") or "Игрок")
    return f"{name} <code>{p['tg_id']}</code>"


def player_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Монеты", callback_data=f"adm:grant:coins:{tg_id}"),
             InlineKeyboardButton(text="👆 Сила тапа", callback_data=f"adm:grant:power:{tg_id}"),
             InlineKeyboardButton(text="⚡ Энергия", callback_data=f"adm:grant:energy:{tg_id}")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:players"),
             InlineKeyboardButton(text="👥 Другой ID", callback_data="adm:search")],
            [InlineKeyboardButton(text="✖ Закрыть", callback_data="adm:close")],
        ]
    )


async def render_menu() -> str:
    return (
        "🛠 <b>Админка Rat Combat</b>\n\n"
        "Выберите раздел кнопками ниже."
    )


async def render_stats() -> str:
    s = await get_stats()
    n, v = s["new"], s["seen"]
    return (
        "📊 <b>Статистика</b>\n\n"
        f"Всего игроков: <b>{fmt_num(s['total'])}</b>\n\n"
        "<b>Новые игроки / заходили в игру</b>\n"
        f"Сегодня: {fmt_num(n['today'])} / {fmt_num(v['today'])}\n"
        f"Вчера: {fmt_num(n['yesterday'])} / {fmt_num(v['yesterday'])}\n"
        f"За 7 дней: {fmt_num(n['week'])} / {fmt_num(v['week'])}\n"
        f"За 30 дней: {fmt_num(n['month'])} / {fmt_num(v['month'])}\n\n"
        f"✅ Активные: <b>{fmt_num(s['active'])}</b> "
        f"(заходили в {ACTIVE_DAYS_NEEDED}+ разных дня из последних {ACTIVE_WINDOW})\n"
        f"💤 Неактивные: <b>{fmt_num(s['inactive'])}</b>\n\n"
        f"Часовой пояс: {html.escape(TZ_NAME)}\n"
        f"Данные на {now_local().strftime('%d.%m.%Y %H:%M:%S')}"
    )


async def render_tops() -> str:
    parts = ["🏆 <b>Топы (10 лучших)</b>"]
    for metric, title in (("coins", "💰 По монетам"), ("power", "👆 По силе клика"),
                          ("earned", "📈 По общему заработку")):
        rows = await get_top(metric, 10)
        parts.append(f"\n<b>{title}</b>")
        if not rows:
            parts.append("Пока нет данных")
        for i, r in enumerate(rows, 1):
            parts.append(f"{i}. {player_label(r)} — {fmt_num(r['value'])}")
    parts.append(f"\nДанные на {now_local().strftime('%d.%m.%Y %H:%M:%S')}")
    return "\n".join(parts)


async def render_activity() -> str:
    a = await get_activity()
    s = await get_stats()
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    lines = ["📈 <b>Активность</b>\n", f"<b>Уникальные заходы за {ACTIVE_WINDOW} дней</b>"]
    for day, cnt in a["per_day"]:
        lines.append(f"{day.strftime('%d.%m')} ({names[day.weekday()]}): {fmt_num(cnt)}")
    lines.append(f"\n<b>Сколько дней из {ACTIVE_WINDOW} заходили</b>")
    for c in range(ACTIVE_WINDOW, 0, -1):
        if a["dist"].get(c):
            lines.append(f"{c} дн.: {fmt_num(a['dist'][c])} игр.")
    lines.append(
        f"\n✅ Активные ({ACTIVE_DAYS_NEEDED}+ дня): <b>{fmt_num(s['active'])}</b>\n"
        f"💤 Неактивные: <b>{fmt_num(s['inactive'])}</b>"
    )
    return "\n".join(lines)


async def render_player(tg_id: int) -> str:
    p = await get_player(tg_id)
    if not p:
        return f"👥 Игрок с ID <code>{tg_id}</code> не найден."
    uname = f"@{html.escape(p['username'])}" if p["username"] else "—"
    pending = []
    for field, col in GRANT_FIELDS.items():
        v = p.get(col) or 0
        if v:
            pending.append(f"{GRANT_LABELS[field]}: {v:+d} (ещё не доставлено)")
    pending_text = ("\n⏳ " + "\n⏳ ".join(pending) + "\n") if pending else ""
    return (
        "👥 <b>Игрок</b>\n\n"
        f"Имя: <b>{html.escape(p['name'] or '—')}</b>\n"
        f"Username: {uname}\n"
        f"Telegram ID: <code>{p['tg_id']}</code>\n\n"
        f"💰 Монет: <b>{fmt_num(p['coins'])}</b>\n"
        f"👆 Сила клика: <b>{fmt_num(p['tap_power'])}</b>\n"
        f"📈 Заработано всего: {fmt_num(p['earned'])}\n"
        f"🖱 Кликов: {fmt_num(p['taps'])}\n"
        f"{pending_text}\n"
        f"🕒 Последняя активность: {fmt_dt(p['last_seen'])}\n"
        f"📅 Первый заход: {p['first_day']}\n"
        f"Дней с заходами: {fmt_num(p['visit_days'])}\n"
        f"Данные на {now_local().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
        "Кнопками ниже можно выдать или списать ресурсы."
    )


PLAYERS_PROMPT = (
    "👥 <b>Игроки</b>\n\n"
    "Отправьте Telegram ID игрока (число) — покажу имя, монеты, силу клика "
    "и дату последней активности, а также дам кнопки выдачи/списания ресурсов."
)


async def safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not ADMIN_ID:
        await message.answer("ADMIN_ID не задан в .env — админка выключена.")
        return
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer(await render_menu(), reply_markup=admin_kb("menu"))


@router.callback_query(F.data.startswith("adm:"))
async def on_admin(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    parts = call.data.split(":")
    view = parts[1] if len(parts) > 1 else "menu"

    if view == "close":
        await call.answer()
        _pending_grant.pop(call.from_user.id, None)
        try:
            await call.message.delete()
        except TelegramBadRequest:
            await safe_edit(call.message, "Админка закрыта.", InlineKeyboardMarkup(inline_keyboard=[]))
        return

    if view == "grant" and len(parts) == 4:
        field, tg_id_s = parts[2], parts[3]
        if field not in GRANT_FIELDS or not tg_id_s.isdigit():
            await call.answer("Ошибка", show_alert=True)
            return
        tg_id = int(tg_id_s)
        _pending_grant[call.from_user.id] = (tg_id, field)
        await call.answer()
        await call.message.answer(
            f"✏️ Отправьте число — на сколько изменить «{GRANT_LABELS[field]}» "
            f"игроку <code>{tg_id}</code>.\n"
            "Положительное число — выдать, отрицательное — списать. Например: 1000 или -500.\n"
            "Изменение применится, когда игрок в следующий раз зайдёт в игру."
        )
        return

    if view == "search":
        _pending_grant.pop(call.from_user.id, None)
        _last_search.pop(call.from_user.id, None)
        await call.answer()
        await safe_edit(call.message, PLAYERS_PROMPT, admin_kb("players"))
        return

    if view == "stats":
        text, kb = await render_stats(), admin_kb("stats")
    elif view == "tops":
        text, kb = await render_tops(), admin_kb("tops")
    elif view == "activity":
        text, kb = await render_activity(), admin_kb("activity")
    elif view == "players":
        last = _last_search.get(call.from_user.id)
        if last:
            text, kb = await render_player(last), player_kb(last)
        else:
            text, kb = PLAYERS_PROMPT, admin_kb("players")
    else:
        text, kb = await render_menu(), admin_kb("menu")
    await call.answer("Обновлено ✓")
    await safe_edit(call.message, text, kb)


@router.message(F.text.regexp(r"^\s*-?\d{1,18}\s*$"))
async def on_admin_text(message: Message) -> None:
    """У админа: число — это либо сумма для выдачи/списания (если ждём её), либо Telegram ID для поиска."""
    if not is_admin(message.from_user.id):
        return
    value = int(message.text.strip())

    pending = _pending_grant.pop(message.from_user.id, None)
    if pending:
        tg_id, field = pending
        if value == 0:
            await message.answer("Нулевое изменение, ничего не делаю.", reply_markup=player_kb(tg_id))
            return
        ok = await adjust_player(tg_id, field, value)
        if not ok:
            await message.answer(f"Игрок с ID <code>{tg_id}</code> не найден.", reply_markup=admin_kb("players"))
            return
        _last_search[message.from_user.id] = tg_id
        verb = "Выдано" if value > 0 else "Списано"
        await message.answer(
            f"✅ {verb}: {GRANT_LABELS[field]} {value:+d} для игрока <code>{tg_id}</code>.\n"
            "Применится при следующем заходе игрока в игру.\n\n" + await render_player(tg_id),
            reply_markup=player_kb(tg_id),
        )
        return

    if value < 1000:  # Telegram ID всегда положительный и достаточно длинный
        return
    tg_id = value
    _last_search[message.from_user.id] = tg_id
    await message.answer(await render_player(tg_id), reply_markup=player_kb(tg_id))


# ---------- Запуск ----------

async def setup_bot_menu(bot: Bot) -> None:
    """Кнопка «Играть» рядом с полем ввода и список команд. Можно вызывать отдельно
    (например, из scripts/vercel_setup.py после деплоя на Vercel, где polling не запускается)."""
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🐀 Играть", web_app=WebAppInfo(url=webapp_url()))
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть Rat Combat"),
                BotCommand(command="top", description="Рейтинг игроков"),
            ]
        )
    except Exception:
        logging.exception("Не удалось настроить меню бота")


async def main() -> None:
    """Локальный запуск: long-polling + свой HTTP API (aiohttp). Не используется на Vercel —
    там бот работает через вебхук, см. api/webhook.py и README (раздел «Деплой на Vercel»)."""
    from aiohttp import web

    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example).")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = get_bot()

    runner = web.AppRunner(build_api())
    await runner.setup()
    await web.TCPSite(runner, API_HOST, API_PORT).start()
    log.info("API слушает %s:%s, публичный адрес: %s", API_HOST, API_PORT,
             API_PUBLIC_URL or "не задан (рейтинг в Mini App будет пуст)")
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан — команда /admin отключена")

    await setup_bot_menu(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
