@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo Возвращаю прерванные задания в очередь...
python loader.py requeue
echo Запускаю 1 поток Ozon и 6 потоков WB (свернутые окна, логи в папке logs)
start "mpstats ozon" /min cmd /c "python -u loader.py run --platform ozon >> logs\run_ozon.log 2>&1"
for /l %%i in (1,1,6) do start "mpstats wb%%i" /min cmd /c "python -u loader.py run --platform wb >> logs\run_wb%%i.log 2>&1"
echo Готово. Прогресс: python loader.py progress
pause
