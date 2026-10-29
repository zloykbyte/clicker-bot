@echo off
cd /d "%~dp0"
title Clicker bot

set PY=python
python --version >nul 2>nul
if not errorlevel 1 goto have_py
set PY=py -3
py -3 --version >nul 2>nul
if not errorlevel 1 goto have_py
echo.
echo Python was not found.
echo Install it from https://www.python.org/downloads/
echo and tick "Add Python to PATH" during installation. Then run this file again.
goto fail

:have_py
if exist .env goto have_env
echo.
echo Paste the NEW bot token from BotFather and press Enter.
set /p TOKEN=Token:
if "%TOKEN%"=="" goto no_token
echo.
echo The bot now needs a Postgres database (a free one from https://neon.tech or
echo https://supabase.com works fine). Paste its connection string
echo (looks like postgres://user:pass@host:5432/dbname) and press Enter.
set /p DBURL=Database URL:
if "%DBURL%"=="" goto no_db
>.env echo BOT_TOKEN=%TOKEN%
>>.env echo DATABASE_URL=%DBURL%
echo Token and database saved to .env
goto have_env

:no_token
echo Token is empty. Run this file again.
goto fail

:no_db
echo Database URL is empty. Run this file again.
goto fail

:have_env
if exist .venv\Scripts\python.exe goto have_venv
echo.
echo Creating environment (first run only)...
%PY% -m venv .venv
if errorlevel 1 goto fail

:have_venv
echo.
echo Installing packages...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 goto fail

echo.
echo Bot is running. Do not close this window. To stop: Ctrl+C
echo.
.venv\Scripts\python.exe bot.py
echo.
echo Bot stopped.
pause
exit /b 0

:fail
echo.
echo Something went wrong. Take a screenshot of this window (hide the token) and send it to Claude.
pause
exit /b 1
