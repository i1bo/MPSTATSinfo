@echo off
chcp 65001 >nul
echo Останавливаю сайт...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and ($_.CommandLine -like '*run_web.py*' -or $_.CommandLine -like '*uvicorn app:app*') }; $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; if ($p) { 'Остановлено.' } else { 'Сайт не был запущен.' }"
timeout /t 3 >nul
