"""Разовая настройка после деплоя на Vercel: регистрирует вебхук Telegram на адрес
`<API_PUBLIC_URL>/api/webhook` и настраивает кнопку «Играть» + список команд бота.

Запускать один раз локально (не на Vercel), с теми же переменными окружения,
что заданы в проекте на Vercel (BOT_TOKEN, API_PUBLIC_URL, WEBHOOK_SECRET —
удобнее всего положить их в .env рядом с этим скриптом и запустить:

    python scripts/vercel_setup.py

Можно запускать повторно — безопасно, просто обновит текущие настройки.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as botmod  # noqa: E402


async def main() -> None:
    if not botmod.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (.env или переменные окружения).")
    if not botmod.API_PUBLIC_URL:
        raise SystemExit(
            "Не задан API_PUBLIC_URL — это публичный адрес вашего деплоя на Vercel, "
            "например https://my-project.vercel.app"
        )

    bot = botmod.get_bot()
    webhook_url = botmod.API_PUBLIC_URL + "/api/webhook"

    kwargs = {"drop_pending_updates": True}
    if botmod.WEBHOOK_SECRET:
        kwargs["secret_token"] = botmod.WEBHOOK_SECRET
    else:
        print("⚠️  WEBHOOK_SECRET не задан — вебхук будет работать без доп. проверки "
              "секрета. Рекомендуется задать WEBHOOK_SECRET и в .env, и в настройках "
              "проекта на Vercel.")

    await bot.set_webhook(webhook_url, **kwargs)
    print(f"✅ Вебхук установлен: {webhook_url}")

    await botmod.setup_bot_menu(bot)
    print("✅ Кнопка «Играть» и команды бота настроены.")

    info = await bot.get_webhook_info()
    print("\nТекущее состояние вебхука:")
    print(f"  url: {info.url}")
    print(f"  pending_update_count: {info.pending_update_count}")
    if info.last_error_message:
        print(f"  ⚠️ последняя ошибка: {info.last_error_message}")

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
