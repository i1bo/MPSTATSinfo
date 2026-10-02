@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Запуск сайта в фоне (без окна консоли). Лог: logs\web.log. Остановить: stop_web.bat
powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:8000/api/meta -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
    echo Сайт уже работает: http://localhost:8000
    start "" http://localhost:8000
    timeout /t 3 >nul
    exit /b
)
echo Запускаю сайт...
rem Python из окружения проекта (.venv) — там все нужные пакеты, от пользователя Windows не зависит
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
    echo Нет окружения .venv. Создайте его: python -m venv .venv  и  .venv\Scripts\python -m pip install -r web\requirements.txt
    pause
    exit /b
)
start "" "%PYW%" web\run_web.py
powershell -NoProfile -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-WebRequest http://127.0.0.1:8000/api/meta -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { Start-Sleep 1 } }; exit 1"
if %errorlevel%==0 (
    echo Сайт запущен: http://localhost:8000
    start "" http://localhost:8000
    timeout /t 3 >nul
) else (
    echo Сайт не запустился, смотрите logs\web.log
    pause
)
