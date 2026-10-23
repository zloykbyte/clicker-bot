# Telegram-кликер

Бот на Python + aiogram 3. Кнопка «Клик», улучшение силы клика за очки, топ игроков. Данные хранятся в SQLite.

## Mini App

Игра лежит в `docs/index.html` и публикуется через GitHub Pages (Settings → Pages → Deploy from a branch → `main` → `/docs`). Адрес по умолчанию: `https://zloykbyte.github.io/clicker-bot/`, его можно заменить переменной `WEBAPP_URL` в `.env`.

Прогресс игрока хранится в облаке Telegram (CloudStorage) и в браузере. Серверной части пока нет: общего топа и бонусов за друзей ещё нет.

## Запуск на Windows в один клик

1. Установите Python с [python.org](https://www.python.org/downloads/) (отметьте «Add Python to PATH»).
2. Скачайте репозиторий (Code → Download ZIP) и распакуйте архив (правая кнопка → «Извлечь всё»).
3. Дважды кликните по `start.bat`. При первом запуске он спросит токен, создаст `.env`, установит пакеты и запустит бота.

Остановить бота: `Ctrl+C` или закрыть окно.

## Запуск вручную

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
