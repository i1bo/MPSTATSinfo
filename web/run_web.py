"""
Запуск сайта в фоне (без окна консоли): pythonw web/run_web.py
Вывод сервера пишется в logs/web.log. Запуск/остановка — start_web.bat / stop_web.bat.
"""
import logging
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
(BASE / "logs").mkdir(exist_ok=True)
# у pythonw нет консоли: sys.stdout/stderr = None, поэтому весь вывод — в файл
log = open(BASE / "logs" / "web.log", "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log
logging.basicConfig(stream=log, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    logging.info("=== запуск сайта (%s)", sys.executable)
    try:
        import uvicorn
        uvicorn.run("app:app", app_dir=str(Path(__file__).resolve().parent), host="127.0.0.1", port=8000,
                    log_config=None)
    except BaseException:
        logging.error("сайт упал:\n%s", traceback.format_exc())
        raise
