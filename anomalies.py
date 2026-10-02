"""
Поиск аномалий в mpstats.sales_daily. Только чтение основной таблицы:
кандидаты складываются в mpstats.anomaly_candidates, сверка площадок — в mpstats.monthly_compare.

  python anomalies.py candidates   правила №1 (скачок заказов) и №2 (цены-заглушки)
  python anomalies.py compare      сверка Ozon и WB по месяцам и сегментам
  python anomalies.py report       сводка по кандидатам
"""
import sys
import time

import loader

# Правило №1: заказы за день > JUMP_RATIO медиан SKU и >= JUMP_MIN_ORDERS штук
JUMP_MIN_ORDERS = 500
JUMP_RATIO = 50
# Правило №2: цена >= PRICE_MIN и > PRICE_RATIO медиан цены SKU
PRICE_MIN = 1_000_000
PRICE_RATIO = 20


def candidates(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS mpstats.anomaly_candidates (
            rule        text     NOT NULL,
            platform    smallint NOT NULL,
            sku         bigint   NOT NULL,
            dt          date     NOT NULL,
            orders      integer,
            revenue     integer,
            price       integer,
            sku_median  numeric,
            ratio       numeric,
            PRIMARY KEY (rule, platform, sku, dt)
        )""")
    con.execute("TRUNCATE mpstats.anomaly_candidates")

    t = time.time()
    # Правило №1: сначала грубый отбор, затем медиана заказов по дням с продажами этого SKU
    con.execute(f"""
        CREATE TEMP TABLE big AS
        SELECT platform, sku, dt, orders, revenue, price
        FROM mpstats.sales_daily WHERE orders >= {JUMP_MIN_ORDERS}""")
    n = con.execute("SELECT count(*), count(DISTINCT (platform, sku)) FROM big").fetchone()
    print(f"правило 1: строк с заказами >= {JUMP_MIN_ORDERS}: {n[0]}, SKU: {n[1]} ({time.time() - t:.0f} c)")
    con.execute(f"""
        INSERT INTO mpstats.anomaly_candidates
        SELECT 'jump', b.platform, b.sku, b.dt, b.orders, b.revenue, b.price, m.med,
               round((b.orders / nullif(m.med, 0))::numeric, 1)
        FROM big b
        JOIN LATERAL (
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s.orders) AS med
            FROM mpstats.sales_daily s WHERE s.platform = b.platform AND s.sku = b.sku
        ) m ON true
        WHERE b.orders > {JUMP_RATIO} * m.med""")
    print(f"правило 1 готово ({time.time() - t:.0f} c)")

    # Правило №2: цены-заглушки
    t = time.time()
    con.execute(f"""
        CREATE TEMP TABLE pricey AS
        SELECT platform, sku, dt, orders, revenue, price
        FROM mpstats.sales_daily WHERE price >= {PRICE_MIN}""")
    n = con.execute("SELECT count(*), count(DISTINCT (platform, sku)) FROM pricey").fetchone()
    print(f"правило 2: строк с ценой >= {PRICE_MIN}: {n[0]}, SKU: {n[1]} ({time.time() - t:.0f} c)")
    con.execute(f"""
        INSERT INTO mpstats.anomaly_candidates
        SELECT 'price', b.platform, b.sku, b.dt, b.orders, b.revenue, b.price, m.med,
               round((b.price / nullif(m.med, 0))::numeric, 1)
        FROM pricey b
        JOIN LATERAL (
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s.price) AS med
            FROM mpstats.sales_daily s WHERE s.platform = b.platform AND s.sku = b.sku
        ) m ON true
        WHERE b.price > {PRICE_RATIO} * m.med""")
    print(f"правило 2 готово ({time.time() - t:.0f} c)")
    report(con)


def compare(con):
    t = time.time()
    con.execute("DROP TABLE IF EXISTS mpstats.monthly_compare")
    con.execute("""
        CREATE TABLE mpstats.monthly_compare AS
        SELECT date_trunc('month', s.dt)::date AS month, s.platform, c.segment,
               sum(s.revenue::bigint) AS revenue, sum(s.orders::bigint) AS orders,
               count(*) AS sku_days
        FROM mpstats.sales_daily s JOIN mpstats.categories c USING (category_id)
        WHERE s.dt BETWEEN '2021-01-01' AND '2024-12-31'
        GROUP BY 1, 2, 3""")
    print(f"monthly_compare готова ({time.time() - t:.0f} c)")
    rows = con.execute("""
        SELECT month, segment,
               max(revenue) FILTER (WHERE platform = 1) wb_rev, max(revenue) FILTER (WHERE platform = 2) oz_rev,
               max(sku_days) FILTER (WHERE platform = 1) wb_sku, max(sku_days) FILTER (WHERE platform = 2) oz_sku
        FROM mpstats.monthly_compare WHERE month BETWEEN '2021-01-01' AND '2024-12-01'
        GROUP BY 1, 2 ORDER BY 2, 1""").fetchall()
    print(f"{'месяц':<8} {'сегмент':<16} {'WB млн':>9} {'Ozon млн':>9} {'Oz/WB':>6} {'WB SKUдн':>9} {'Oz SKUдн':>9}")
    for month, seg, wr, orv, ws, os_ in rows:
        ratio = f"{float(orv) / float(wr):.2f}" if wr and orv else "-"
        print(f"{month:%Y-%m} {seg:<16} {float(wr or 0) / 1e6:>9.0f} {float(orv or 0) / 1e6:>9.0f} {ratio:>6} {ws or 0:>9} {os_ or 0:>9}")


def report(con):
    print("\nСводка кандидатов:")
    for r in con.execute("""
            SELECT rule, platform, extract(year FROM dt)::int y, count(*) n, count(DISTINCT sku) skus,
                   round(sum(revenue::bigint) / 1e6) rev_mln
            FROM mpstats.anomaly_candidates GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""):
        print(r)
    print("\nТоп-10 по выручке:")
    for r in con.execute("""
            SELECT a.rule, a.platform, a.sku, a.dt, a.orders, a.revenue, a.price, round(a.sku_median, 1), a.ratio, p.name
            FROM mpstats.anomaly_candidates a
            LEFT JOIN mpstats.products p ON p.platform = a.platform AND p.sku = a.sku
            ORDER BY a.revenue DESC LIMIT 10"""):
        print(r)


if __name__ == "__main__":
    loader.load_env()
    with loader.pg() as con:
        con.execute("SET max_parallel_workers_per_gather = 4")
        {"candidates": candidates, "compare": compare, "report": report}[sys.argv[1]](con)
