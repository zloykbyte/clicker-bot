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

import aiosqlite
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
    WebAppInfo,
)
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "clicker.db")
# Адрес Mini App (страница из папки docs, опубликованная через GitHub Pages).
# Должен быть https. Можно переопределить в .env: WEBAPP_URL=...
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://zloykbyte.github.io/clicker-bot/")
# Публичный https-адрес API этого бота (туннель или хостинг), его получает Mini App
# для рейтингов и учёта посещений. Без него игра работает, но рейтинг пуст.
API_PUBLIC_URL = (os.getenv("API_PUBLIC_URL") or "").rstrip("/")
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("PORT") or os.getenv("API_PORT") or 8080)

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
log = logging.getLogger("ratcombat")


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


# ---------- База данных ----------
# Старая таблица `users` (бывший классик-кликер) не читается и не изменяется:
# мы её не создаём и не удаляем, существующие данные остаются в файле как есть.

async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, coldef: str) -> None:
    """Добавляет колонку, если её ещё нет — безопасно для уже существующей базы."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cur.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                tg_id     INTEGER PRIMARY KEY,
                name      TEXT    NOT NULL DEFAULT '',
                username  TEXT    NOT NULL DEFAULT '',
                coins     INTEGER NOT NULL DEFAULT 0,
                tap_power INTEGER NOT NULL DEFAULT 1,
                earned    INTEGER NOT NULL DEFAULT 0,
                taps      INTEGER NOT NULL DEFAULT 0,
                first_day TEXT    NOT NULL,
                last_seen TEXT    NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS visits (
                tg_id INTEGER NOT NULL,
                day   TEXT    NOT NULL,
                PRIMARY KEY (tg_id, day)
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_visits_day ON visits(day)")
        # Накопленные выдачи/списания админа, которые ещё не доставлены игроку
        # (применяются на стороне клиента при следующей синхронизации).
        await _ensure_column(db, "players", "adj_coins", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "players", "adj_tap_power", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "players", "adj_energy", "INTEGER NOT NULL DEFAULT 0")
        await db.commit()


async def record_sync(tg_id: int, name: str, username: str,
                      coins: int, tap_power: int, earned: int, taps: int) -> None:
    """Сохраняет состояние игрока и засчитывает посещение (один день — одно)."""
    day = today_local().isoformat()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO players (tg_id, name, username, coins, tap_power, earned, taps, first_day, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                name = excluded.name, username = excluded.username,
                coins = excluded.coins, tap_power = excluded.tap_power,
                earned = excluded.earned, taps = excluded.taps,
                last_seen = excluded.last_seen
            """,
            (tg_id, name, username, coins, tap_power, earned, taps, day, ts),
        )
        await db.execute("INSERT OR IGNORE INTO visits (tg_id, day) VALUES (?, ?)", (tg_id, day))
        await db.commit()


TOP_COLUMNS = {"coins": "coins", "power": "tap_power", "earned": "earned"}
# Выдача/списание ресурсов из админки: публичное имя поля -> колонка с накопленной дельтой.
GRANT_FIELDS = {"coins": "adj_coins", "power": "adj_tap_power", "energy": "adj_energy"}
GRANT_LABELS = {"coins": "💰 Монеты", "power": "👆 Сила тапа", "energy": "⚡ Энергия"}


async def get_top(metric: str, limit: int) -> list[dict]:
    col = TOP_COLUMNS[metric]  # только из белого списка
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Администратор не участвует в рейтингах.
        if ADMIN_ID:
            cur = await db.execute(
                f"SELECT tg_id, name, username, {col} AS value FROM players "
                f"WHERE tg_id != ? ORDER BY {col} DESC, tg_id ASC LIMIT ?",
                (ADMIN_ID, limit),
            )
        else:
            cur = await db.execute(
                f"SELECT tg_id, name, username, {col} AS value FROM players "
                f"ORDER BY {col} DESC, tg_id ASC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in await cur.fetchall()]


async def adjust_player(tg_id: int, field: str, delta: int) -> bool:
    """Копит выдачу/списание ресурса для игрока; применится на его стороне при синхронизации."""
    col = GRANT_FIELDS[field]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM players WHERE tg_id = ?", (tg_id,))
        if not await cur.fetchone():
            return False
        await db.execute(f"UPDATE players SET {col} = {col} + ? WHERE tg_id = ?", (delta, tg_id))
        await db.commit()
        return True


async def take_grant(tg_id: int) -> dict:
    """Забирает и обнуляет накопленные выдачи игрока — вызывается при его синхронизации."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT adj_coins, adj_tap_power, adj_energy FROM players WHERE tg_id = ?",
            (tg_id,),
        )
        row = await cur.fetchone()
        if not row:
            return {}
        grant = dict(row)
        if any(grant.values()):
            await db.execute(
                "UPDATE players SET adj_coins = 0, adj_tap_power = 0, adj_energy = 0 WHERE tg_id = ?",
                (tg_id,),
            )
            await db.commit()
            return grant
        return {}


async def get_player(tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM players WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        if not row:
            return None
        p = dict(row)
        cur = await db.execute("SELECT COUNT(*) FROM visits WHERE tg_id = ?", (tg_id,))
        p["visit_days"] = (await cur.fetchone())[0]
        return p


async def get_stats() -> dict:
    today = today_local()
    d = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    async with aiosqlite.connect(DB_PATH) as db:
        async def one(sql, args=()):
            cur = await db.execute(sql, args)
            return (await cur.fetchone())[0]

        total = await one("SELECT COUNT(*) FROM players")
        new = {
            "today": await one("SELECT COUNT(*) FROM players WHERE first_day = ?", (d(0),)),
            "yesterday": await one("SELECT COUNT(*) FROM players WHERE first_day = ?", (d(1),)),
            "week": await one("SELECT COUNT(*) FROM players WHERE first_day >= ?", (d(6),)),
            "month": await one("SELECT COUNT(*) FROM players WHERE first_day >= ?", (d(29),)),
        }
        seen = {
            "today": await one("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day = ?", (d(0),)),
            "yesterday": await one("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day = ?", (d(1),)),
            "week": await one("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day >= ?", (d(6),)),
            "month": await one("SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day >= ?", (d(29),)),
        }
        active = await one(
            "SELECT COUNT(*) FROM (SELECT tg_id FROM visits WHERE day >= ? "
            "GROUP BY tg_id HAVING COUNT(DISTINCT day) >= ?)",
            (d(ACTIVE_WINDOW - 1), ACTIVE_DAYS_NEEDED),
        )
    return {"total": total, "new": new, "seen": seen,
            "active": active, "inactive": max(0, total - active)}


async def get_activity() -> dict:
    today = today_local()
    days = [(today - timedelta(days=n)) for n in range(ACTIVE_WINDOW - 1, -1, -1)]
    async with aiosqlite.connect(DB_PATH) as db:
        per_day = []
        for day in days:
            cur = await db.execute(
                "SELECT COUNT(DISTINCT tg_id) FROM visits WHERE day = ?", (day.isoformat(),)
            )
            per_day.append((day, (await cur.fetchone())[0]))
        cur = await db.execute(
            "SELECT c, COUNT(*) FROM (SELECT COUNT(DISTINCT day) AS c FROM visits "
            "WHERE day >= ? GROUP BY tg_id) GROUP BY c",
            (days[0].isoformat(),),
        )
        dist = {c: n for c, n in await cur.fetchall()}
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

_last_sync: dict[int, float] = {}


def cors_headers(request: web.Request) -> dict:
    origin = urlparse(WEBAPP_URL)
    allowed = f"{origin.scheme}://{origin.netloc}"
    h = {
        "Access-Control-Allow-Origin": allowed,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Cache-Control": "no-store",
        "Vary": "Origin",
    }
    return h


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers(request))
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers.update(cors_headers(request))
    return resp


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def api_sync(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    user = validate_init_data(str(body.get("initData", "")))
    if not user:
        return web.json_response({"ok": False, "error": "bad auth"}, status=401)
    uid = user["id"]
    now = time.monotonic()
    if now - _last_sync.get(uid, 0) < 3:
        return web.json_response({"ok": True, "throttled": True})
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
    return web.json_response(resp)


async def api_top(request: web.Request) -> web.Response:
    metric = request.query.get("metric", "coins")
    if metric not in TOP_COLUMNS:
        metric = "coins"
    try:
        limit = int(request.query.get("limit", "10"))
    except ValueError:
        limit = 10
    limit = limit if limit in (3, 10, 100) else 10
    rows = await get_top(metric, limit)  # всегда свежие данные из базы
    items = [
        {"rank": i + 1, "name": r["name"] or r["username"] or "Игрок", "value": r["value"]}
        for i, r in enumerate(rows)
    ]
    return web.json_response({"metric": metric, "limit": limit, "items": items,
                              "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def build_api() -> web.Application:
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

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example).")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    runner = web.AppRunner(build_api())
    await runner.setup()
    await web.TCPSite(runner, API_HOST, API_PORT).start()
    log.info("API слушает %s:%s, публичный адрес: %s", API_HOST, API_PORT,
             API_PUBLIC_URL or "не задан (рейтинг в Mini App будет пуст)")
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан — команда /admin отключена")

    try:
        # Кнопка «Играть» рядом с полем ввода и список команд
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
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
