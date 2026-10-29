"""Общие вещи для serverless-функций в api/*.py (Vercel, Python runtime).

Каждый файл в api/ — отдельная функция на платформе, но все они работают с одним
и тем же ботом: импортируют bot.py из корня репозитория, чтобы не дублировать
логику (БД, проверку initData, обработчики команд и админки).
"""
import logging
import os
import sys

# Vercel запускает каждую функцию в своём окружении; на всякий случай явно
# добавляем корень репозитория в sys.path, чтобы `import bot` всегда находился,
# независимо от того, как именно платформа разложила файлы.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO)

import bot as botmod  # noqa: E402  (импорт после правки sys.path — так и задумано)

__all__ = ["botmod"]
