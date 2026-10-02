# Один образ на всё: сайт (по умолчанию), сборка файла DuckDB, выгрузка из MPSTATS.
# Запуск и команды — см. README.md и docker-compose.yml.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY web/requirements.txt web/requirements.txt
RUN pip install -r web/requirements.txt \
    # модули DuckDB ставим при сборке образа, а не при первом запросе: на сервере может не быть интернета
    && python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL excel'); c.execute('INSTALL postgres')"

COPY . .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/static/style.css', timeout=4)"

# --proxy-headers: за nginx/Caddy сайт видит настоящий адрес клиента и https (нужно для защиты входа и cookie)
CMD ["python", "-m", "uvicorn", "app:app", "--app-dir", "web", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
