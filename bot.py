import asyncio
import html
import logging
import os

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "clicker.db")

router = Router()


# ---------- База данных ----------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name    TEXT    NOT NULL,
                score   INTEGER NOT NULL DEFAULT 0,
                power   INTEGER NOT NULL DEFAULT 1,
                total   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.commit()


async def get_user(user_id: int, name: str) -> dict:
    """Возвращает игрока, создаёт его при первом обращении."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
        await db.execute(
            "UPDATE users SET name = ? WHERE user_id = ?", (name, user_id)
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(await cur.fetchone())


async def add_click(user_id: int, name: str) -> dict:
    await get_user(user_id, name)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "UPDATE users SET score = score + power, total = total + power "
            "WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(await cur.fetchone())


async def buy_upgrade(user_id: int, name: str) -> tuple[bool, dict]:
    """Покупка улучшения. Проверка и списание очков идут одним запросом."""
    await get_user(user_id, name)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE users "
            "SET score = score - 25 * power * power, power = power + 1 "
            "WHERE user_id = ? AND score >= 25 * power * power",
            (user_id,),
        )
        await db.commit()
        ok = cur.rowcount > 0
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return ok, dict(await cur.fetchone())


async def get_top(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT name, total FROM users ORDER BY total DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]


# ---------- Оформление ----------

def upgrade_cost(power: int) -> int:
    return 25 * power * power


def render_text(u: dict) -> str:
    return (
        "🖱 <b>Кликер</b>\n\n"
        f"💰 Очки: <b>{u['score']}</b>\n"
        f"⚡ За клик: <b>+{u['power']}</b>\n"
        f"📈 Всего заработано: <b>{u['total']}</b>"
    )


def render_keyboard(u: dict) -> InlineKeyboardMarkup:
    cost = upgrade_cost(u["power"])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👆 Клик!", callback_data="click")],
            [
                InlineKeyboardButton(
                    text=f"⚡ Улучшить ({cost})", callback_data="upgrade"
                ),
                InlineKeyboardButton(text="🏆 Топ", callback_data="top"),
            ],
        ]
    )


def display_name(user) -> str:
    return user.full_name or user.username or str(user.id)


async def safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Редактирование сообщения с защитой от лимитов и «не изменено»."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramRetryAfter:
        pass  # слишком частые клики, пропускаем обновление картинки
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- Обработчики ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = await get_user(message.from_user.id, display_name(message.from_user))
    await message.answer(render_text(u), reply_markup=render_keyboard(u))


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    await message.answer(await render_top())


@router.callback_query(F.data == "click")
async def on_click(call: CallbackQuery) -> None:
    u = await add_click(call.from_user.id, display_name(call.from_user))
    await call.answer()
    await safe_edit(call.message, render_text(u), render_keyboard(u))


@router.callback_query(F.data == "upgrade")
async def on_upgrade(call: CallbackQuery) -> None:
    ok, u = await buy_upgrade(call.from_user.id, display_name(call.from_user))
    if ok:
        await call.answer(f"Улучшено! Теперь +{u['power']} за клик", show_alert=False)
    else:
        await call.answer(
            f"Не хватает очков, нужно {upgrade_cost(u['power'])}", show_alert=True
        )
    await safe_edit(call.message, render_text(u), render_keyboard(u))


async def render_top() -> str:
    rows = await get_top()
    if not rows:
        return "Пока никого нет, будьте первым!"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ игроков</b>\n"]
    for i, r in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {html.escape(r['name'])}: <b>{r['total']}</b>")
    return "\n".join(lines)


@router.callback_query(F.data == "top")
async def on_top(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(await render_top())


# ---------- Запуск ----------

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example).")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
