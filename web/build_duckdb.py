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
  outliers    — выбросы: дни товара с неправдоподобной ценой или заказами (правило и во сколько раз выше нормы),
                в sales и sales_m — флаг outlier; см. mark_outliers
  meta        — дата сборки и границы данных

  python web/build_duckdb.py --outliers   пересчитать только выбросы в готовом файле (сайт остановить)
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


SALES_M_COLS = """dt DATE, platform UTINYINT, segment_id UTINYINT, category_id INTEGER,
    brand_id INTEGER, seller_id INTEGER, sku BIGINT, anomaly BOOLEAN, outlier BOOLEAN,
    revenue BIGINT, orders BIGINT, days INTEGER, first_dt DATE, last_dt DATE, price_last INTEGER"""
SALES_M_SELECT = """
    SELECT date_trunc('month', dt)::DATE, platform, segment_id, category_id, brand_id, seller_id, sku,
           anomaly, outlier, sum(revenue), sum(orders), count(*), min(dt), max(dt), arg_max(price, dt)
    FROM sales"""

# Выбросы — отдельные дни товара с неправдоподобными ценой или заказами (цена-заглушка у продавца,
# сброс фиктивного остатка, который MPSTATS считает продажами). Правила по порядку приоритета:
#   1 цена >= OUT_NB_MIN_PRICE и в OUT_NB_RATIO+ раз выше цены в соседние дни продаж товара (до и после)
#   2 цена в OUT_CAT_RATIO+ раз выше 99-го перцентиля цен категории за год (категория — исходный путь MPSTATS
#     своей площадки, не единая категория сайта) и при этом одно из:
#       - цена в OUT_OWN_RATIO+ раз выше обычной цены товара — 10-го перцентиля его цен (не медианы: у многонедельных
#         заглушек завышено больше половины дней, и медиана тоже завышена);
#       - заказов за день >= OUT_CAT_MIN_ORDERS (заглушка с первого дня продаж: 257 тыс. ₽ × 698 шт.);
#       - цена в OUT_CAT_EXTREME+ раз выше 99-го перцентиля (так не бывает и у настоящих дорогих: Octane ~30x).
#     Товар, который дорогой всегда и продаётся умеренно (револьвер 66 тыс. ₽ по 10–20 шт.), не трогаем.
#   3 заказов >= OUT_JUMP_MIN_ORDERS и в OUT_JUMP_RATIO+ раз больше медианы заказов товара за день
#   4 средний чек дня (выручка / заказы) в OUT_CHK_RATIO+ раз выше медианы чека товара по всем его дням,
#     если дней с заказами у товара >= OUT_CHK_MIN_DAYS (ловит заглушки на несколько дней подряд, где правило 1
#     молчит: соседние дни тоже завышены). При 5x попадают и настоящие покупки дорогой модификации — поэтому 10x.
OUT_NB_MIN_PRICE, OUT_NB_RATIO = 50_000, 20
OUT_CAT_RATIO, OUT_OWN_RATIO, OUT_CAT_MIN_ORDERS, OUT_CAT_EXTREME = 10, 5, 50, 100
OUT_JUMP_MIN_ORDERS, OUT_JUMP_RATIO = 500, 50
OUT_CHK_RATIO, OUT_CHK_MIN_DAYS = 10, 5
OUT_PARTS = 8  # медиану чека по всем товарам считаем частями по sku % OUT_PARTS — разом не хватает памяти и диска


def mark_outliers(con):
    """Таблица outliers (platform, sku, dt, rule, ratio, ref) и флаг sales.outlier.
    ratio — во сколько раз значение дня выше ref: цены соседних дней (1), 99-го перцентиля цен категории (2),
    медианы заказов товара (3), медианы среднего чека товара (4)."""
    t = time.time()
    log("выбросы (неправдоподобные цены и заказы по дням товара)...")
    # по шагам через временные таблицы: одним запросом DuckDB держит в памяти всё сразу и не помещается
    con.execute("""
        CREATE OR REPLACE TEMP TABLE o_cat AS
        SELECT platform, category_id, year(dt) AS y, quantile_cont(price, 0.99) AS p99
        FROM sales WHERE price > 0 GROUP BY ALL
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE o_r2 AS
        SELECT s.platform, s.sku, s.dt, s.price, s.orders, c.p99
        FROM sales s JOIN o_cat c ON c.platform = s.platform AND c.category_id = s.category_id AND c.y = year(s.dt)
        WHERE s.price > {OUT_CAT_RATIO} * c.p99
    """)
    # соседние дни — только у товаров, где цена хоть раз была высокой (окно по всей таблице слишком тяжёлое)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE o_hp AS
        SELECT DISTINCT platform, sku FROM sales WHERE price >= {OUT_NB_MIN_PRICE}
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE o_nb AS
        SELECT * FROM (
            SELECT platform, sku, dt, price,
                   greatest(coalesce(lag(price) OVER w, 0), coalesce(lead(price) OVER w, 0)) AS ref
            FROM sales SEMI JOIN o_hp USING (platform, sku)
            WINDOW w AS (PARTITION BY platform, sku ORDER BY dt)
        ) WHERE price >= {OUT_NB_MIN_PRICE}
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE o_big AS
        SELECT platform, sku, dt, orders FROM sales WHERE orders >= {OUT_JUMP_MIN_ORDERS}
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE o_med AS
        SELECT platform, sku, quantile_cont(price, 0.1) AS price_p10, median(orders) AS orders
        FROM sales SEMI JOIN (SELECT platform, sku FROM o_r2 UNION SELECT platform, sku FROM o_big) k USING (platform, sku)
        GROUP BY ALL
    """)
    con.execute("CREATE OR REPLACE TEMP TABLE o_chk (platform UTINYINT, sku BIGINT, med DOUBLE, n BIGINT)")
    for part in range(OUT_PARTS):
        con.execute(f"""
            INSERT INTO o_chk SELECT platform, sku, median(revenue / orders), count(*)
            FROM sales WHERE orders > 0 AND sku % {OUT_PARTS} = {part} GROUP BY ALL
        """)
    con.execute(f"""
        CREATE OR REPLACE TABLE outliers AS
        WITH c AS (
            SELECT platform, sku, dt, 1 AS rule, price / ref AS ratio, ref
            FROM o_nb WHERE ref > 0 AND price > {OUT_NB_RATIO}::BIGINT * ref
            UNION ALL
            SELECT r.platform, r.sku, r.dt, 2, r.price / r.p99, r.p99
            FROM o_r2 r JOIN o_med m USING (platform, sku)
            WHERE r.price > {OUT_OWN_RATIO} * m.price_p10 OR r.orders >= {OUT_CAT_MIN_ORDERS}
               OR r.price > {OUT_CAT_EXTREME} * r.p99
            UNION ALL
            SELECT b.platform, b.sku, b.dt, 3, b.orders / m.orders, m.orders
            FROM o_big b JOIN o_med m USING (platform, sku)
            WHERE m.orders > 0 AND b.orders > {OUT_JUMP_RATIO} * m.orders
            UNION ALL
            SELECT s.platform, s.sku, s.dt, 4, s.revenue / s.orders / m.med, m.med
            FROM sales s JOIN o_chk m USING (platform, sku)
            WHERE s.orders > 0 AND m.n >= {OUT_CHK_MIN_DAYS} AND m.med > 0
              AND s.revenue / s.orders > {OUT_CHK_RATIO} * m.med
        )
        SELECT platform, sku, dt, min(rule)::UTINYINT AS rule,
               round(arg_min(ratio, rule), 1) AS ratio, round(arg_min(ref, rule))::BIGINT AS ref
        FROM c GROUP BY ALL ORDER BY platform, sku, dt
    """)
    for tmp in ("o_cat", "o_r2", "o_hp", "o_nb", "o_big", "o_med", "o_chk"):
        con.execute(f"DROP TABLE {tmp}")
    con.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS outlier BOOLEAN DEFAULT false")
    con.execute("UPDATE sales SET outlier = false WHERE outlier")
    con.execute("""
        UPDATE sales SET outlier = true FROM outliers o
        WHERE sales.platform = o.platform AND sales.sku = o.sku AND sales.dt = o.dt
    """)
    for p, rule, n, rev in con.execute("""
            SELECT o.platform, o.rule, count(*), round(sum(s.revenue) / 1e9, 2) FROM outliers o
            JOIN sales s USING (platform, sku, dt) GROUP BY ALL ORDER BY 1, 2""").fetchall():
        log(f"   {PLATFORMS[p]}, правило {rule}: {n} дней, выручка {rev} млрд ₽")
    log(f"   готово за {time.time() - t:.0f} c")


def refresh_outliers(target: Path):
    """Пересчитать выбросы в готовом файле без полной сборки (сайт должен быть остановлен):
    флаг в sales и строки sales_m тех товаров и месяцев, где флаг мог поменяться."""
    month_keys = "SELECT DISTINCT platform, sku, date_trunc('month', dt)::DATE AS m FROM outliers"
    with duckdb.connect(str(target)) as con:
        con.execute(f"SET memory_limit = '{os.environ.get('DUCKDB_BUILD_MEMORY', '8GB')}'")
        con.execute(f"SET temp_directory = '{(target.parent / 'duckdb_tmp').as_posix()}'")
        con.execute("BEGIN")
        had = con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'outliers'").fetchone()[0]
        con.execute(f"CREATE TEMP TABLE touched_old AS {month_keys if had else 'SELECT NULL::UTINYINT AS platform, NULL::BIGINT AS sku, NULL::DATE AS m WHERE false'}")
        mark_outliers(con)
        con.execute(f"CREATE TEMP TABLE touched AS {month_keys} UNION SELECT * FROM touched_old")
        con.execute("ALTER TABLE sales_m ADD COLUMN IF NOT EXISTS outlier BOOLEAN DEFAULT false")
        con.execute("""
            DELETE FROM sales_m WHERE EXISTS (SELECT 1 FROM touched t
                WHERE t.platform = sales_m.platform AND t.sku = sales_m.sku AND t.m = sales_m.dt)
        """)
        con.execute(f"""
            INSERT INTO sales_m (dt, platform, segment_id, category_id, brand_id, seller_id, sku, anomaly, outlier,
                                 revenue, orders, days, first_dt, last_dt, price_last)
            {SALES_M_SELECT}
            SEMI JOIN touched t ON t.platform = sales.platform AND t.sku = sales.sku
                               AND t.m = date_trunc('month', sales.dt)
            GROUP BY ALL
        """)
        n = con.execute("SELECT count(*) FROM touched").fetchone()[0]
        con.execute("COMMIT")
        con.execute("CHECKPOINT")
        log(f"sales_m: пересчитано товаро-месяцев: {n}")


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

    mark_outliers(con)

    # Помесячная предагрегация: строк в ~6 раз меньше. Сайт берёт из неё полные месяцы периода,
    # а неполные крайние дни — из дневной таблицы. Аномалия и выброс входят в ключ, чтобы фильтры по ним были точными.
    con.execute(f"CREATE TABLE sales_m ({SALES_M_COLS})")
    t = time.time()
    log("помесячная таблица (по кварталам)...")
    q = d1.replace(month=(d1.month - 1) // 3 * 3 + 1, day=1)
    while q <= d2:
        nxt = q.replace(year=q.year + 1, month=1) if q.month == 10 else q.replace(month=q.month + 3)
        con.execute(f"INSERT INTO sales_m {SALES_M_SELECT} WHERE dt >= ? AND dt < ? GROUP BY ALL ORDER BY 1, 2, 3",
                    [q, nxt])
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
    elif "--outliers" in sys.argv:
        # только пересчитать выбросы в готовом файле (сайт должен быть остановлен)
        refresh_outliers(target)
    else:
        build(target)
