"""
Сжатие mpstats.sales_daily: пересборка первичного ключа + VACUUM.
Старый индекс удаляется до постройки нового, потому что для обычного REINDEX не хватает места на диске.
Во время работы не запускать загрузки.
"""
import shutil
import time

import loader


def free_gb():
    return round(shutil.disk_usage("C:/").free / 1e9, 1)


def sizes(con):
    t, i = con.execute("SELECT pg_table_size('mpstats.sales_daily'), "
                       "pg_indexes_size('mpstats.sales_daily')").fetchone()
    return f"таблица {t / 1e9:.1f} ГБ, индекс {i / 1e9:.1f} ГБ, свободно на диске {free_gb()} ГБ"


def step(con, title, sql):
    t = time.time()
    print(f"{time.strftime('%H:%M:%S')} {title}...", flush=True)
    con.execute(sql)
    print(f"   готово за {time.time() - t:.0f} c | {sizes(con)}", flush=True)


if __name__ == "__main__":
    loader.load_env()
    with loader.pg() as con:
        print("до:", sizes(con), flush=True)
        con.execute("SET maintenance_work_mem = '2000MB'")
        con.execute("SET max_parallel_maintenance_workers = 4")
        step(con, "удаляю старый индекс (первичный ключ)",
             "ALTER TABLE mpstats.sales_daily DROP CONSTRAINT sales_daily_pkey")
        step(con, "VACUUM ANALYZE (помечаю мёртвые строки к переиспользованию)",
             "VACUUM (ANALYZE) mpstats.sales_daily")
        step(con, "строю новый индекс (первичный ключ platform, sku, dt)",
             "ALTER TABLE mpstats.sales_daily ADD CONSTRAINT sales_daily_pkey PRIMARY KEY (platform, sku, dt)")
        print("после:", sizes(con), flush=True)
