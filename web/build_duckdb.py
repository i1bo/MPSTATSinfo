"""
Сборка аналитической копии для сайта: PostgreSQL (mpstats) -> файл DuckDB.

  python web/build_duckdb.py

Postgres остаётся основной базой (в неё пишет loader.py). Сайт читает только файл DuckDB.
Файл собирается рядом (*.new.duckdb) и подменяет старый в конце, поэтому сайт можно не останавливать
(на Windows открытый файл подменить нельзя — тогда перезапустите сайт).

Что лежит в файле:
  sales       — дневные продажи (как mpstats.sales_daily), отсортированы по дате, бренд/продавец/сегмент
                уже подставлены числовыми id, флаг anomaly — дата попадает в mpstats.anomaly_periods
  products    — название, бренд, продавец по SKU
  brands, sellers, categories, segments — справочники для фильтров (с общей выручкой для сортировки)
  meta        — дата сборки и границы данных
"""
import os
import sys
import time
from pathlib import Path

import duckdb

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import loader  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import categories  # noqa: E402

PLATFORMS = {1: "Wildberries", 2: "Ozon"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def step(con, title, sql):
    t = time.time()
    log(f"{title}...")
    con.execute(sql)
    log(f"   готово за {time.time() - t:.0f} c")


def anomaly_expr(con):
    """SQL-выражение флага аномалии по справочнику периодов из Postgres."""
    rows = con.execute("SELECT platform, d1, d2 FROM pg.mpstats.anomaly_periods").fetchall()
    if not rows:
        return "false"
    return "(" + " OR ".join(
        f"(s.platform = {p} AND s.dt BETWEEN DATE '{d1}' AND DATE '{d2}')" for p, d1, d2 in rows) + ")"


def build(target: Path):
    tmp = target.with_suffix(".new.duckdb")
    for f in (tmp, Path(str(tmp) + ".wal")):
        f.unlink(missing_ok=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(tmp))
    con.execute(f"SET memory_limit = '{os.environ.get('DUCKDB_BUILD_MEMORY', '8GB')}'")
    con.execute(f"SET temp_directory = '{(target.parent / 'duckdb_tmp').as_posix()}'")
    con.execute("INSTALL postgres; LOAD postgres")
    con.execute("INSTALL excel")  # для быстрой выгрузки в Excel на сайте
    dsn = os.environ["PG_DSN"].replace("'", "''")
    con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY)")
    t0 = time.time()

    step(con, "справочник категорий", """
        CREATE TABLE segments AS
        SELECT row_number() OVER (ORDER BY segment)::UTINYINT AS segment_id, segment AS name
        FROM (SELECT DISTINCT segment FROM pg.mpstats.categories);
        CREATE TABLE categories AS
        SELECT c.category_id::INTEGER AS category_id, c.platform::UTINYINT AS platform, c.name,
               g.segment_id
        FROM pg.mpstats.categories c JOIN segments g ON g.name = c.segment;
    """)

    step(con, "карточки товаров (бренд, продавец, название)", """
        CREATE TEMP TABLE p AS
        SELECT platform::UTINYINT AS platform, sku, name,
               coalesce(nullif(trim(brand), ''), '(без бренда)')   AS brand,
               coalesce(nullif(trim(seller), ''), '(не указан)')   AS seller
        FROM pg.mpstats.products;
        CREATE TABLE brands AS
        SELECT row_number() OVER (ORDER BY brand)::INTEGER AS brand_id, brand AS name
        FROM (SELECT DISTINCT brand FROM p);
        CREATE TABLE sellers AS
        SELECT row_number() OVER (ORDER BY seller)::INTEGER AS seller_id, seller AS name
        FROM (SELECT DISTINCT seller FROM p);
        CREATE TABLE products AS
        SELECT p.platform, p.sku, p.name, b.brand_id, s.seller_id
        FROM p JOIN brands b ON b.name = p.brand JOIN sellers s ON s.name = p.seller;
        DROP TABLE p;
    """)

    # Сырые строки — в отдельный промежуточный файл (сжатый), чтобы не держать их в памяти/temp.
    # Сортировать все строки разом нельзя: на это не хватает места под временные файлы,
    # поэтому сортируем и дописываем по кварталам.
    stage = target.with_suffix(".stage.duckdb")
    stage.unlink(missing_ok=True)
    con.execute(f"ATTACH '{stage.as_posix()}' AS st")
    step(con, "дневные продажи: копирование из Postgres", """
        CREATE TABLE st.s_raw AS
        SELECT platform::UTINYINT AS platform, sku, dt, category_id::INTEGER AS category_id,
               revenue::BIGINT AS revenue, orders::INTEGER AS orders, price::INTEGER AS price
        FROM pg.mpstats.sales_daily;
    """)

    select = f"""
        SELECT s.dt, s.platform, c.segment_id, s.category_id,
               coalesce(p.brand_id, 0)  AS brand_id,
               coalesce(p.seller_id, 0) AS seller_id,
               s.sku, s.revenue, s.orders, s.price,
               {anomaly_expr(con)} AS anomaly
        FROM st.s_raw s
        JOIN categories c USING (category_id)
        LEFT JOIN products p ON p.platform = s.platform AND p.sku = s.sku
    """
    con.execute(f"CREATE TABLE sales AS {select} WHERE false")
    d1, d2 = con.execute("SELECT min(dt), max(dt) FROM st.s_raw").fetchone()
    t = time.time()
    log("дневные продажи: подстановка справочников и сортировка по дате (по кварталам)...")
    q = d1.replace(month=(d1.month - 1) // 3 * 3 + 1, day=1)
    while q <= d2:
        nxt = q.replace(year=q.year + 1, month=1) if q.month == 10 else q.replace(month=q.month + 3)
        con.execute(f"INSERT INTO sales {select} WHERE s.dt >= ? AND s.dt < ? "
                    f"ORDER BY s.dt, s.platform, c.segment_id", [q, nxt])
        log(f"   {q:%Y} кв.{(q.month - 1) // 3 + 1} готов")
        q = nxt
    log(f"   готово за {time.time() - t:.0f} c")
    con.execute("DETACH st")
    stage.unlink(missing_ok=True)

    # Помесячная предагрегация: строк в ~6 раз меньше. Сайт берёт из неё полные месяцы периода,
    # а неполные крайние дни — из дневной таблицы. Аномалия входит в ключ, чтобы фильтр по ней был точным.
    con.execute("""
        CREATE TABLE sales_m (dt DATE, platform UTINYINT, segment_id UTINYINT, category_id INTEGER,
            brand_id INTEGER, seller_id INTEGER, sku BIGINT, anomaly BOOLEAN,
            revenue BIGINT, orders BIGINT, days INTEGER, first_dt DATE, last_dt DATE, price_last INTEGER)
    """)
    t = time.time()
    log("помесячная таблица (по кварталам)...")
    q = d1.replace(month=(d1.month - 1) // 3 * 3 + 1, day=1)
    while q <= d2:
        nxt = q.replace(year=q.year + 1, month=1) if q.month == 10 else q.replace(month=q.month + 3)
        con.execute("""
            INSERT INTO sales_m
            SELECT date_trunc('month', dt)::DATE, platform, segment_id, category_id, brand_id, seller_id, sku,
                   anomaly, sum(revenue), sum(orders), count(*), min(dt), max(dt), arg_max(price, dt)
            FROM sales WHERE dt >= ? AND dt < ?
            GROUP BY ALL ORDER BY 1, 2, 3
        """, [q, nxt])
        q = nxt
    log(f"   готово за {time.time() - t:.0f} c")

    step(con, "общая выручка брендов и продавцов (для порядка в фильтрах)", """
        ALTER TABLE brands ADD COLUMN revenue BIGINT;
        UPDATE brands SET revenue = t.r FROM (SELECT brand_id, sum(revenue) r FROM sales GROUP BY 1) t
        WHERE brands.brand_id = t.brand_id;
        ALTER TABLE sellers ADD COLUMN revenue BIGINT;
        UPDATE sellers SET revenue = t.r FROM (SELECT seller_id, sum(revenue) r FROM sales GROUP BY 1) t
        WHERE sellers.seller_id = t.seller_id;
        ALTER TABLE categories ADD COLUMN revenue BIGINT;
        UPDATE categories SET revenue = t.r FROM (SELECT category_id, sum(revenue) r FROM sales GROUP BY 1) t
        WHERE categories.category_id = t.category_id;
        INSERT INTO brands VALUES (0, '(нет карточки)', 0);
        INSERT INTO sellers VALUES (0, '(нет карточки)', 0);
    """)
    category_groups(con)

    step(con, "служебная информация", """
        CREATE TABLE meta AS
        SELECT now() AS built_at, min(dt) AS min_dt, max(dt) AS max_dt, count(*) AS rows FROM sales;
    """)
    con.execute("DETACH pg")
    con.execute("CHECKPOINT")
    rows, d1, d2 = con.execute("SELECT rows, min_dt, max_dt FROM meta").fetchone()
    con.close()

    target.unlink(missing_ok=True)
    tmp.replace(target)
    log(f"Готово: {rows:,} строк, {d1} — {d2}, файл {target.stat().st_size / 1e9:.1f} ГБ, "
        f"всего {(time.time() - t0) / 60:.0f} мин")


def category_groups(con):
    """Единые названия и сегменты категорий для сайта (см. categories.py): categories.display / group_id /
    site_segment_id и справочник cat_groups. Исходные пути и сегменты (categories.segment_id,
    sales.segment_id) не меняются — сайт показывает и фильтрует сегмент по site_segment_id."""
    log("единые названия категорий...")
    con.execute(f"""
        INSERT INTO segments SELECT coalesce(max(segment_id), 0) + 1, '{categories.OTHER_SEGMENT}' FROM segments
        WHERE NOT EXISTS (SELECT 1 FROM segments WHERE name = '{categories.OTHER_SEGMENT}')
    """)
    rows = con.execute("""
        SELECT c.category_id, c.platform, c.name, g.name, coalesce(c.revenue, 0)
        FROM categories c JOIN segments g USING (segment_id)
    """).fetchall()
    seg_id = dict(con.execute("SELECT name, segment_id FROM segments").fetchall())
    res = categories.build(rows)
    con.execute("CREATE OR REPLACE TEMP TABLE cat_display (category_id INTEGER, display VARCHAR, seg UTINYINT)")
    con.executemany("INSERT INTO cat_display VALUES (?, ?, ?)",
                    [(cid, name, seg_id[seg]) for cid, (name, seg, _) in res.items()])
    for col, typ in (("display", "VARCHAR"), ("group_id", "INTEGER"), ("site_segment_id", "UTINYINT")):
        con.execute(f"ALTER TABLE categories DROP COLUMN IF EXISTS {col}")
        con.execute(f"ALTER TABLE categories ADD COLUMN {col} {typ}")
    con.execute("UPDATE categories SET display = d.display, site_segment_id = d.seg FROM cat_display d "
                "WHERE categories.category_id = d.category_id")
    # группа = одинаковое название без учёта регистра; показываем самое «денежное» написание
    con.execute("""
        CREATE OR REPLACE TABLE cat_groups AS
        SELECT row_number() OVER (ORDER BY key)::INTEGER AS group_id, key,
               arg_max(display, coalesce(revenue, 0)) AS name, sum(coalesce(revenue, 0))::BIGINT AS revenue,
               string_agg(DISTINCT CASE platform WHEN 1 THEN 'WB' ELSE 'Ozon' END, ', ') AS platforms
        FROM (SELECT lower(display) AS key, * FROM categories)
        GROUP BY key
    """)
    con.execute("UPDATE categories SET group_id = g.group_id FROM cat_groups g WHERE lower(categories.display) = g.key")
    con.execute("UPDATE categories SET display = g.name FROM cat_groups g WHERE categories.group_id = g.group_id")
    n = con.execute("SELECT count(*) FROM cat_groups").fetchone()[0]
    other = sum(1 for _, seg, _ in res.values() if seg == categories.OTHER_SEGMENT)
    log(f"   {len(rows)} путей -> {n} единых названий, в сегменте «{categories.OTHER_SEGMENT}» {other} путей")


if __name__ == "__main__":
    loader.load_env()
    target = Path(os.environ.get("DUCKDB_PATH", BASE / "data" / "mpstats.duckdb"))
    if "--categories" in sys.argv:
        # только пересчитать единые названия категорий в готовом файле (сайт должен быть остановлен)
        with duckdb.connect(str(target)) as c:
            category_groups(c)
    else:
        build(target)
