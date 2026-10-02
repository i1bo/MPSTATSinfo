# Продажи MPSTATS

Выгрузка продаж Wildberries и Ozon из MPSTATS в PostgreSQL и сайт с таблицей, фильтрами, графиками и выгрузкой в Excel/CSV.

```
loader.py              MPSTATS API → PostgreSQL (схема mpstats)
web/build_duckdb.py    PostgreSQL → data/mpstats.duckdb (аналитическая копия для сайта)
web/app.py             сайт (FastAPI), читает только файл DuckDB
```

Настройки — в `.env` (образец: `.env.example`). Файл `.env` и папка `data/` в git не попадают.

## Пароль на сайт

В `.env` задать `WEB_PASSWORD` (и при желании `WEB_USER`), перезапустить сайт. Вход запоминается на `WEB_SESSION_DAYS` дней (по умолчанию 30), кнопка «Выйти» — в шапке. Смена пароля сбрасывает все входы. После 10 неверных попыток с одного адреса вход блокируется на 15 минут.

Если сайт открыт в интернет, поставьте перед ним nginx или Caddy с https: без https пароль идёт открытым текстом.

## Сервер (Docker)

Первый запуск:

```bash
git clone <адрес репозитория> mpstats && cd mpstats
cp .env.example .env        # заполнить: WEB_PASSWORD, PG_DSN, MPSTATS_TOKEN
mkdir -p data               # сюда положить mpstats.duckdb (или собрать, см. ниже)
docker compose up -d --build
```

Сайт — на порту `WEB_PORT` (по умолчанию 8000).

Обновление кода:

```bash
git pull && docker compose up -d --build
```

Данные. Файл `data/mpstats.duckdb` (~6,5 ГБ) можно скопировать с машины, где он собран, или собрать на сервере из Postgres:

```bash
docker compose run --rm build-db && docker compose restart web
```

Сборке нужно свободное место: ~2× размер файла плюс временные файлы (сейчас ~15 ГБ).
Postgres в docker-compose не входит: `PG_DSN` должен указывать на существующую базу. Postgres на том же сервере — хост `host.docker.internal`, и Postgres должен принимать подключения из сети Docker (`listen_addresses`, `pg_hba.conf`).

Выгрузка из MPSTATS в контейнере — любая команда `loader.py`:

```bash
docker compose run --rm loader status
docker compose run --rm loader run --platform wb
```

## Windows (как сейчас)

- `start_web.bat` / `stop_web.bat` — сайт в фоне на http://localhost:8000, лог в `logs/web.log`
- `start_load.bat` / `stop_load.bat` — выгрузка из MPSTATS
- `python web/build_duckdb.py` — пересобрать файл для сайта (сайт перед подменой файла остановить)
