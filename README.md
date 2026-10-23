# Telegram-кликер

Бот на Python + aiogram 3. Кнопка «Клик», улучшение силы клика за очки, топ игроков. Данные хранятся в SQLite.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
# впишите токен от @BotFather в .env
python bot.py
```

Токен хранится только в `.env`. Файл добавлен в `.gitignore`, в репозиторий он не попадёт.

## Команды

- `/start` открывает игру
- `/top` показывает топ игроков

## Как это работает

- Клик даёт `power` очков.
- Улучшение стоит `25 * power²` очков и повышает `power` на 1.
- Покупка проверяется и списывается одним SQL-запросом, поэтому двойные нажатия не уводят баланс в минус.

## Деплой

Подойдёт любой хостинг с постоянным процессом (Railway, Render, VPS). Команда запуска: `python bot.py`, переменная окружения: `BOT_TOKEN`. Если нужно, чтобы база переживала перезапуски, смонтируйте диск и укажите путь в `DB_PATH`.
