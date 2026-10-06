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

Всё в `docker-compose.yml`: `db` — PostgreSQL 16 (данные в `./pgdata`, снаружи сервера не виден),
`web` — сайт на порту `WEB_PORT`, `build-db` и `loader` — разовые команды. После перезагрузки сервера
`db` и `web` поднимаются сами.

Первый запуск:

```bash
git clone <адрес репозитория> ~/mpstats && cd ~/mpstats
cp .env.example .env        # заполнить: WEB_PASSWORD, PG_PASSWORD, PG_DSN (хост db), MPSTATS_TOKEN
mkdir -p data logs pgdata
docker compose up -d db     # пустая база; затем перенести дамп (ниже)
docker compose run --rm build-db
docker compose up -d --build
```

Перенос базы с Windows-машины. Дамп идёт сразу по сети и на диске не сохраняется. Запускать в cmd,
не в PowerShell: PowerShell 5 портит двоичные данные в конвейере.

```
set PGPASSWORD=пароль_локального_postgres
"%ProgramFiles%\PostgreSQL\16\bin\pg_dump.exe" -h localhost -p 5433 -U postgres -d mpstats -n mpstats -Fc -Z 6 | ssh -p ПОРТ пользователь@сервер "cat > ~/mpstats.dump"
```

На сервере:

```bash
cd ~/mpstats
docker compose cp ~/mpstats.dump db:/tmp/mpstats.dump
docker compose exec db pg_restore -U postgres -d mpstats --no-owner -j 4 /tmp/mpstats.dump
docker compose exec db rm /tmp/mpstats.dump && rm ~/mpstats.dump
```

Обновление кода:

```bash
git pull && docker compose up -d --build
```

Пересобрать файл сайта после догрузки данных (~6 мин):

```bash
docker compose run --rm build-db && docker compose restart web
```

Сборке нужно свободное место: ~2× размер файла плюс временные файлы (сейчас ~15 ГБ).

Выгрузка из MPSTATS в контейнере — любая команда `loader.py`:

```bash
docker compose run --rm loader status
docker compose run --rm loader run --platform wb
```

## Windows (как сейчас)

- `start_web.bat` / `stop_web.bat` — сайт в фоне на http://localhost:8000, лог в `logs/web.log`
- `start_load.bat` / `stop_load.bat` — выгрузка из MPSTATS
- `python web/build_duckdb.py` — пересобрать файл для сайта (сайт перед подменой файла остановить)
- `python web/build_duckdb.py --outliers` — пересчитать только выбросы в готовом файле (~1 мин, сайт остановить)

## Аномалии и выбросы

- **Аномальные периоды** — сбои MPSTATS за целые даты, справочник `mpstats.anomaly_periods` (anomaly_periods.sql).
  На сайте — «без аномальных периодов».
- **Выбросы** — отдельные дни товара с неправдоподобной ценой или заказами (цена-заглушка, сброс фиктивного
  остатка). Считаются при сборке файла (`mark_outliers` в web/build_duckdb.py), данные в Postgres не меняются:
  1. цена ≥ 50 000 ₽ и в 20+ раз выше цены в соседние дни продаж товара;
  2. цена в 10+ раз выше 99-го перцентиля цен категории за год (категория — исходный путь MPSTATS своей
     площадки) и при этом: в 5+ раз выше обычной цены товара (10-й перцентиль его цен), или ≥ 50 заказов
     за день, или в 100+ раз выше 99-го перцентиля. Товары, которые дорогие всегда, не трогаем;
  3. заказов ≥ 500 и в 50+ раз больше медианы заказов товара;
  4. средний чек дня в 10+ раз выше медианы чека товара (если у товара ≥ 5 дней с заказами) —
     заглушки на несколько дней подряд, где правило 1 не срабатывает.

  На сайте — «без выбросов» (включено по умолчанию), на странице артикула — список таких дней с причиной.

