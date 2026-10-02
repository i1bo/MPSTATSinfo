@echo off
chcp 65001 >nul
echo Останавливаю потоки выгрузки...
wmic process where "name='python.exe' and commandline like '%%loader.py run%%'" call terminate >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*loader.py run*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Остановлено. Перед следующим запуском start_load.bat сам вернет задания в очередь.
pause
