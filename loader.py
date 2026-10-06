"""
Выгрузка MPSTATS (WB по предметам, Ozon по категориям) в PostgreSQL.

Команды:
  python loader.py init-db              создать схему mpstats
  python loader.py quota                показать остаток квоты
  python loader.py discover             найти доп. предметы WB из каталога "Бытовая техника"
  python loader.py plan                 заполнить очередь заданий (load_jobs)
  python loader.py test-quota           проверить, списывает ли квоту пагинация
  python loader.py plan-garden          задания сегмента «Сад и дача»
  python loader.py plan-pets            задания сегмента «Товары для животных» (автокормушки, автопоилки)
  python loader.py run [--limit N] [--platform wb|ozon] [--segment garden|pets]
                                        выполнять задания из очереди (можно прерывать и продолжать)
  python loader.py status               сводка по очереди и базе
  python loader.py progress             таблица прогресса по годам
  python loader.py requeue              вернуть прерванные/упавшие задания в очередь
                                        (запускать, когда ни один воркер не работает)
  Параллельно: запустить несколько "run" в разных окнах, задания не пересекутся.

Настройки берутся из файла .env рядом со скриптом (см. .env.example).
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import psycopg
import requests

BASE = Path(__file__).resolve().parent
REF = BASE / "справочники"
API = "https://mpstats.io/api"

# Граница перехода WB на estimated-метрики
WB_ESTIMATED_FROM = dt.date(2025, 9, 1)
START = dt.date(2020, 1, 1)

WB_BT_PARENTS = ["Бытовая техника", "Техника для кухни", "Техника для красоты и здоровья",
                 "Крупная бытовая техника", "Климатическая техника"]
WB_TOOLS_PARENTS = ["Электроинструмент и оборудование", "Ручной инструмент и оснастка"]
# Группы, чьи предметы берём, только если они есть в каталоге WB "Бытовая техника"
WB_BT_EXTRA_PARENTS = ["Здоровье", "Аксессуары для электроники", "Садовая техника"]

# Старое дерево Ozon: до перестройки в 2024 году инструменты жили по этим путям.
# Грузим их за годы до конца 2024 включительно; пересечения с новыми путями схлопываются по (sku, dt).
OZON_LEGACY_UNTIL = dt.date(2024, 12, 31)
OZON_LEGACY_PATHS = [
    ("Инструменты", "Строительство и ремонт/Инструменты"),
    ("Инструменты", "Строительство и ремонт/Расходные материалы и оснастка"),
]

OZON_PATHS = [
    ("Бытовая техника", "Бытовая техника"),
    ("Инструменты", "Строительство и ремонт/Инструменты для ремонта и строительства"),
    ("Инструменты", "Строительство и ремонт/Расходные материалы и оснастка для инструментов"),
]


# ---------------------------------------------------------------- сегмент «Сад и дача»
GARDEN_SEGMENT = "Сад и дача"
GARDEN_UNTIL = dt.date(2026, 8, 10)
WB_GARDEN_PARENTS = ["Садовая техника", "Садовые инструменты и полив", "Товары для отдыха и кемпинга"]
WB_GARDEN_SUBJECTS = [  # точечно из других групп: «parent / subject»
    "Все для садоводства / Средства от грызунов",
    "Все для садоводства / Отпугиватели и ловушки садовые",
    "Все для садоводства / Пистолеты поливочные",
    "Все для садоводства / Капельницы для капельного полива",
    "Все для садоводства / Фитинги для насосов",
    "Все для садоводства / Направляющие для шлангов",
    "Все для садоводства / Запчасти для систем автополива",
    "Бытовая химия / Защита от насекомых",
    "Хозяйственные товары / Мышеловки",
    "Спортивный товар / Палатки",
    "Спортивный товар / Кровати кемпинговые",
    "Спортивный товар / Скамейки кемпинговые",
    "Спортивный товар / Наборы кемпинговой мебели",
]
OZON_GARDEN_PATHS = [
    "Дом и сад/Дача и сад",
    "Туризм, рыбалка, охота/Туризм и отдых на природе",
    "Спорт и отдых/Водный спорт и отдых на воде/Бассейны",
    "Бытовая химия и гигиена/Бытовая химия/Защита от насекомых",
    "Бытовая химия и гигиена/Бытовая химия/Средства от грызунов",
]
# до перестройки дерева Ozon опрыскиватели лежали здесь
OZON_GARDEN_LEGACY = [("Дом и сад/Цветы и растения/Лейки и опрыскиватели", START, dt.date(2024, 12, 31))]


# ---------------------------------------------------------------- сегмент «Товары для животных»
# Название сегмента сортируется после остальных — id прежних сегментов на сайте (и ссылки с фильтрами) не сдвигаются
PETS_SEGMENT = "Товары для животных"
PETS_UNTIL = dt.date(2026, 8, 10)
# у WB отдельного предмета для автокормушек нет — грузим предметы целиком (решение пользователя)
WB_PETS_SUBJECTS = [
    "Товары для животных / Кормушки для животных",
    "Товары для животных / Поилки для животных",
]
OZON_PETS_CATS = "Товары для животных/Товары для кошек/Посуда для кошек/Автокормушки и автопоилки для кошек"
OZON_PETS_DOGS = "Товары для животных/Товары для собак/Посуда для собак/Автокормушки и автопоилки для собак"
OZON_PETS_PATHS = [OZON_PETS_CATS, OZON_PETS_DOGS]
# Нынешние пути MPSTATS отдаёт с 2024 года (категория товара берётся на конец периода), раньше — старое дерево:
# 2023 — свои категории автокормушек; 2020–2022 отдельной категории не было — берём весь раздел и отбираем
# товары по названию (pet_auto_name). Всё кладём в категории нынешних путей, чтобы на сайте был один ряд.
# (путь, с, по, категория в базе, отбор по названию)
OZON_PETS_LEGACY = [
    ("Товары для животных/Для кошек/Посуда для кошек/Автокормушки и автопоилки",
     dt.date(2023, 1, 1), dt.date(2023, 12, 31), OZON_PETS_CATS, False),
    ("Товары для животных/Для собак/Посуда/Автокормушки и автопоилки",
     dt.date(2023, 1, 1), dt.date(2023, 12, 31), OZON_PETS_DOGS, False),
    ("Товары для животных/Для кошек", START, dt.date(2022, 12, 31), OZON_PETS_CATS, True),
    ("Товары для животных/Для собак", START, dt.date(2022, 12, 31), OZON_PETS_DOGS, True),
]
OZON_LEGACY_TARGET = {path: (target, by_name) for path, _, _, target, by_name in OZON_PETS_LEGACY}

# отбор по названию: кормушка/поилка/фонтан/миска + признак автоматики, без птиц, рыб, грызунов и с/х животных
PET_FEED = re.compile(r"кормушк|кормилк|поилк|поильник|фонтан|миск|диспенсер|дозатор")
PET_AUTO = re.compile(r"автокорм|автопоил|автомат|фонтан|умн|таймер|wi-?fi|вай-?фай|электрон|программ|камер"
                      r"|беспровод|usb|насос|помп|датчик")
PET_OTHER = re.compile(r"птиц|попуга|канаре|\bкур|несуш|бройлер|цыпл|перепел|голуб|рыб|аквариум|грызун|хомяк"
                       r"|крол|шиншил|хорь|хорек|черепах|рептил|ниппел|пчел|теленк|телят|свин|козл|коз\b")


def pet_auto_name(name):
    n = (name or "").lower().replace("ё", "е")
    return bool(PET_FEED.search(n) and PET_AUTO.search(n) and not PET_OTHER.search(n))

# Догружаемые сегменты не перезаписывают дни товара, уже лежащие в другом сегменте
# (товар мог попасть в выдачу и прежних предметов) — существующие данные сайта не меняются.
ISOLATED_SEGMENTS = {PETS_SEGMENT}

# короткие латинские имена сегментов для командной строки (на сервере кириллицу набирать неудобно)
SEGMENT_ALIASES = {"garden": GARDEN_SEGMENT, "pets": PETS_SEGMENT}


def wb_garden_subjects():
    subs = read_csv(REF / "wb_subjects_all.csv")
    names = set(WB_GARDEN_SUBJECTS)
    return [(int(s["id"]), s["name"], s["first_date"] or "2020-01-01")
            for s in subs if s["parent"] in WB_GARDEN_PARENTS or s["name"] in names]


def wb_pets_subjects():
    subs = {s["name"]: s for s in read_csv(REF / "wb_subjects_all.csv")}
    missing = [n for n in WB_PETS_SUBJECTS if n not in subs]
    if missing:
        sys.exit(f"Нет в справочнике wb_subjects_all.csv: {missing}")
    return [(int(subs[n]["id"]), n, subs[n]["first_date"] or "2020-01-01") for n in WB_PETS_SUBJECTS]


# ---------------------------------------------------------------- настройки
def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    for k in ("MPSTATS_TOKEN", "PG_DSN"):
        if not os.environ.get(k):
            sys.exit(f"Не задан {k} в .env")


def cfg(name, default):
    return type(default)(os.environ.get(name, default))


def pg():
    return psycopg.connect(os.environ["PG_DSN"], autocommit=True)


# ---------------------------------------------------------------- API
session = requests.Session()


def api_headers():
    return {"X-Mpstats-TOKEN": os.environ["MPSTATS_TOKEN"], "Content-Type": "application/json"}


def get_quota():
    r = session.get(f"{API}/user/report_api_limit", headers=api_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def quota_left(q, platform):
    key = "wb_external" if platform == "wb" else "ozon_external"
    avail, used = q.get(f"available_{key}"), q.get(f"use_{key}")
    if avail is None or used is None:
        return None
    return avail - used


def post_page(url, payload, retries=6):
    """POST с повторами на 429/5xx и сетевые ошибки."""
    delay = 10
    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            r = session.post(url, headers=api_headers(), data=json.dumps(payload),
                             timeout=cfg("HTTP_TIMEOUT", 600))
            secs = time.time() - t0
            if r.status_code == 200:
                return r.json(), r.status_code, secs
            if r.status_code in (429, 500, 502, 503, 504):
                print(f"   HTTP {r.status_code}, повтор через {delay} c ({attempt}/{retries})")
            else:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                ValueError) as e:
            print(f"   сеть: {e.__class__.__name__}, повтор через {delay} c ({attempt}/{retries})")
        time.sleep(delay)
        delay = min(delay * 2, 300)
    raise RuntimeError("исчерпаны повторы запроса")


def endpoint(platform, path, d1, d2):
    if platform == "wb":
        return f"{API}/wb/get/subject?d1={d1}&d2={d2}&path={path}"
    return f"{API}/oz/get/category?d1={d1}&d2={d2}&path={requests.utils.quote(path)}"


def payload_for(metric, start_row, page_size):
    return {
        "startRow": start_row,
        "endRow": start_row + page_size,
        "filterModel": {metric: {"filterType": "number", "type": "notEqual", "filter": 0}},
        "sortModel": [{"colId": metric, "sort": "desc"}],
    }


# ---------------------------------------------------------------- справочники
def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))


def wb_subjects():
    """[(segment, subject_id, subject_name, first_date)]"""
    subs = read_csv(REF / "wb_subjects_all.csv")
    out, seen = [], set()
    extra_file = REF / "wb_bt_extra_subjects.csv"
    extra_ids = {r["id"] for r in read_csv(extra_file)} if extra_file.exists() else set()
    for s in subs:
        seg = None
        if s["parent"] in WB_BT_PARENTS or s["id"] in extra_ids:
            seg = "Бытовая техника"
        elif s["parent"] in WB_TOOLS_PARENTS:
            seg = "Инструменты"
        if seg and s["id"] not in seen:
            seen.add(s["id"])
            out.append((seg, int(s["id"]), s["name"], s["first_date"] or "2020-01-01"))
    return out


def periods(end):
    """Годовые отрезки, с разрывом на границе estimated у WB."""
    cuts = [START] + [dt.date(y, 1, 1) for y in range(START.year + 1, end.year + 1)]
    if WB_ESTIMATED_FROM not in cuts:
        cuts.append(WB_ESTIMATED_FROM)
    cuts = sorted(c for c in cuts if c <= end)
    res = []
    for i, c in enumerate(cuts):
        e = cuts[i + 1] - dt.timedelta(days=1) if i + 1 < len(cuts) else end
        res.append((c, e))
    return res


# ---------------------------------------------------------------- команды
def cmd_init_db(_):
    with pg() as con:
        con.execute((BASE / "schema.sql").read_text(encoding="utf-8"))
    print("Схема mpstats создана")


def cmd_quota(_):
    print(json.dumps(get_quota(), ensure_ascii=False, indent=1))


def cmd_discover(args):
    """Предметы, которые реально лежат в каталоге WB 'Бытовая техника', из доп. групп."""
    subs = {s["id"]: s for s in read_csv(REF / "wb_subjects_all.csv")}
    d2 = dt.date.today() - dt.timedelta(days=1)
    d1 = d2 - dt.timedelta(days=cfg("DISCOVER_DAYS", 365))
    found = {}
    path = requests.utils.quote("Бытовая техника")
    # 1) эндпоинт предметов категории
    r = session.get(f"{API}/wb/get/category/subjects?path={path}&d1={d1}&d2={d2}",
                    headers=api_headers(), timeout=300)
    if r.status_code == 200 and isinstance(r.json(), list):
        for row in r.json():
            sid = str(row.get("id") or row.get("subject_id") or "")
            if sid:
                found[sid] = row.get("name", "")
        print(f"category/subjects: {len(found)} предметов")
    elif not args.fallback:
        sys.exit(f"category/subjects недоступен (HTTP {r.status_code}): {r.text[:300]}\n"
                 "Запасной путь (листает товары категории): python loader.py discover --fallback")
    else:
        # 2) запасной путь: товары категории с продажами -> их subject_id
        print(f"category/subjects недоступен (HTTP {r.status_code}), собираю предметы по товарам")
        url = f"{API}/wb/get/category?d1={d1}&d2={d2}&path={path}"
        start = 0
        stats = {}  # subject_id -> [sku_count, revenue]
        while True:
            data, _, _ = post_page(url, payload_for("revenue", start, 5000))
            items = data.get("data", [])
            for it in items:
                sid = str(it.get("subject_id"))
                st = stats.setdefault(sid, [0, 0.0])
                st[0] += 1
                st[1] += it.get("revenue") or 0
            start += len(items)
            print(f"   {start}/{data.get('total')}")
            if len(items) < 5000 or start >= (data.get("total") or 0):
                break
        with open(REF / "wb_bt_catalog_subject_stats.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["subject_id", "name", "parent", "sku_in_catalog", "revenue_in_catalog"])
            for sid, (n, rev) in sorted(stats.items(), key=lambda x: -x[1][1]):
                s = subs.get(sid, {})
                w.writerow([sid, s.get("name", ""), s.get("parent", ""), n, round(rev)])
        min_sku = cfg("DISCOVER_MIN_SKU", 30)
        for sid, (n, rev) in stats.items():
            if n >= min_sku:
                found[sid] = subs.get(sid, {}).get("name", "")
    extra = [subs[sid] for sid in found if sid in subs and subs[sid]["parent"] in WB_BT_EXTRA_PARENTS]
    with open(REF / "wb_bt_extra_subjects.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=next(iter(subs.values())).keys(), delimiter=";")
        w.writeheader()
        w.writerows(extra)
    print(f"Доп. предметов из групп {WB_BT_EXTRA_PARENTS}: {len(extra)} -> справочники/wb_bt_extra_subjects.csv")
    for s in extra:
        print("  ", s["id"], s["name"])


def cmd_plan(args):
    end = dt.date.fromisoformat(args.d2) if args.d2 else dt.date.today() - dt.timedelta(days=1)
    jobs = []
    # WB у MPSTATS запаздывает: дальше WB_DATA_UNTIL данные недостоверны (задаётся в .env)
    wb_until = os.environ.get("WB_DATA_UNTIL")
    wb_end = min(end, dt.date.fromisoformat(wb_until)) if wb_until else end
    for seg, sid, name, first in wb_subjects():
        first_d = dt.date.fromisoformat(first)
        for d1, d2 in periods(wb_end):
            if d2 < first_d:
                continue
            metric = "revenue_estimated" if d1 >= WB_ESTIMATED_FROM else "revenue"
            jobs.append(("wb", seg, str(sid), name, d1, d2, metric))
    for seg, path in OZON_PATHS:
        for d1, d2 in periods(end):
            jobs.append(("ozon", seg, path, path, d1, d2, "revenue"))
    for seg, path in OZON_LEGACY_PATHS:
        for d1, d2 in periods(end):
            if d2 <= OZON_LEGACY_UNTIL:
                jobs.append(("ozon", seg, path, path, d1, d2, "revenue"))
    with pg() as con:
        cur = con.cursor()
        cur.executemany(
            """INSERT INTO mpstats.load_jobs (platform, segment, path, path_name, d1, d2, metric)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (platform, path, d1, d2) DO NOTHING""", jobs)
        n = con.execute("SELECT platform, count(*) FROM mpstats.load_jobs GROUP BY 1").fetchall()
    print(f"Сформировано {len(jobs)} заданий; в очереди: {dict(n)}")


def parse_items(job, items):
    """Разворачивает дневные массивы в строки sales_daily + карточки products."""
    platform, d1 = job["platform"], job["d1"]
    ndays = (job["d2"] - d1).days + 1
    est = job["metric"] == "revenue_estimated"
    daily, prods = [], []
    bad_len = 0
    legacy_target, by_name = OZON_LEGACY_TARGET.get(job["path"], (None, False)) if platform == "ozon" else (None, False)
    for it in items:
        sku = it.get("id")
        if not sku or (by_name and not pet_auto_name(it.get("name"))):
            continue
        if est:
            rev, orders = it.get("revenue_estimated_graph"), it.get("sales_estimated_graph")
        else:
            rev = it.get("revenue_graph")
            orders = it.get("sales_graph") or it.get("graph")
        price = it.get("price_graph") or []
        if not rev:
            continue
        if len(rev) != ndays:
            bad_len += 1
        if platform == "wb":
            # предмет = запрошенный предмет (решение пользователя). Собственный предмет товара — в
            # it["subject"]; у ~0–7% товаров он другой (MPSTATS отдаёт и товары из path_list).
            category = job["path_name"]
        else:
            category = legacy_target or it.get("niche") or it.get("category") or job["path_name"]
        first = last = None
        for i, v in enumerate(rev):
            if not v:
                continue
            day = d1 + dt.timedelta(days=i)
            first = first or day
            last = day
            o = orders[i] if orders and i < len(orders) else None
            p = price[i] if i < len(price) else None
            daily.append((sku, day, category, to_int(v), to_int(o), to_int(p)))
        if first:
            prods.append((sku, it.get("name"), it.get("brand"),
                          it.get("seller") or it.get("supplier"),
                          it.get("supplier_id") or it.get("seller_id"), category, first, last))
    return daily, prods, bad_len


def to_int(v):
    return None if v is None or v == "" else int(round(float(v)))


PLATFORM_CODE = {"wb": 1, "ozon": 2}
_category_ids = {}
_wb_subject_ids = {}  # название предмета WB -> subject_id (из ответов API)


def category_ids(con, job, names):
    """Возвращает {название: category_id}, заводя новые категории в справочнике."""
    pcode = PLATFORM_CODE[job["platform"]]
    missing = {n for n in names if (pcode, n) not in _category_ids}
    if missing:
        def subject_id(n):
            if job["platform"] != "wb":
                return None
            return _wb_subject_ids.get(n) or (int(job["path"]) if n == job["path_name"] else None)
        con.cursor().executemany(
            """INSERT INTO mpstats.categories (platform, name, segment, subject_id)
               VALUES (%s,%s,%s,%s) ON CONFLICT (platform, name) DO NOTHING""",
            [(pcode, n, job["segment"], subject_id(n)) for n in missing])
        for cid, n in con.execute("SELECT category_id, name FROM mpstats.categories "
                                  "WHERE platform=%s AND name = ANY(%s)", (pcode, list(missing))):
            _category_ids[(pcode, n)] = cid
    return {n: _category_ids[(pcode, n)] for n in names}


INT_MAX = 2_147_483_647


def own_segment(table, segment):
    """Условие «строка таблицы уже относится к сегменту задания» — для ISOLATED_SEGMENTS, иначе всегда истина."""
    if segment not in ISOLATED_SEGMENTS:
        return "TRUE"
    seg = segment.replace("'", "''")
    return f"{table}.category_id IN (SELECT category_id FROM mpstats.categories WHERE segment = '{seg}')"


def save(con, job, daily, prods):
    pcode = PLATFORM_CODE[job["platform"]]
    cids = category_ids(con, job, {r[2] for r in daily} | {p[5] for p in prods})
    own_daily = own_segment("sales_daily", job["segment"])
    own_over = own_segment("sales_daily_overflow", job["segment"])
    own_prod = own_segment("products", job["segment"])
    # аномальные значения (сбои данных MPSTATS) не влезают в integer — кладём их отдельно
    big = [r for r in daily if any(v is not None and abs(v) > INT_MAX for v in r[3:6])]
    if big:
        daily = [r for r in daily if not any(v is not None and abs(v) > INT_MAX for v in r[3:6])]
        con.cursor().executemany(
            """INSERT INTO mpstats.sales_daily_overflow (platform, sku, dt, category_id, revenue, orders, price)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (platform, sku, dt) DO UPDATE SET revenue=EXCLUDED.revenue,
                   orders=EXCLUDED.orders, price=EXCLUDED.price, category_id=EXCLUDED.category_id
               WHERE """ + own_over,
            [(pcode, sku, day, cids[cat], rev, o, p) for sku, day, cat, rev, o, p in big])
        print(f"   аномальных строк (> int): {len(big)} -> sales_daily_overflow")
    with con.transaction():
        con.execute("""CREATE TEMP TABLE IF NOT EXISTS tmp_daily
                       (LIKE mpstats.sales_daily) ON COMMIT DELETE ROWS""")
        cur = con.cursor()
        with cur.copy("""COPY tmp_daily (sku, dt, category_id, revenue, orders, price, platform)
                         FROM STDIN""") as cp:
            for sku, day, cat, rev, o, p in daily:
                cp.write_row((sku, day, cids[cat], rev, o, p, pcode))
        # один SKU может попасть в несколько предметов/страниц -> берём одну строку
        con.execute(f"""
            INSERT INTO mpstats.sales_daily (sku, dt, category_id, revenue, orders, price, platform)
            SELECT DISTINCT ON (platform, sku, dt) sku, dt, category_id, revenue, orders, price, platform
            FROM tmp_daily ORDER BY platform, sku, dt
            ON CONFLICT (platform, sku, dt) DO UPDATE SET
                revenue = EXCLUDED.revenue, orders = EXCLUDED.orders, price = EXCLUDED.price,
                category_id = EXCLUDED.category_id
            WHERE {own_daily}""")
        cur.executemany(f"""
            INSERT INTO mpstats.products (platform, sku, name, brand, seller, seller_id,
                                          category_id, first_dt, last_dt)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (platform, sku) DO UPDATE SET
                name = COALESCE(EXCLUDED.name, products.name),
                brand = COALESCE(EXCLUDED.brand, products.brand),
                seller = CASE WHEN EXCLUDED.last_dt >= products.last_dt
                              THEN COALESCE(EXCLUDED.seller, products.seller) ELSE products.seller END,
                seller_id = COALESCE(EXCLUDED.seller_id, products.seller_id),
                category_id = CASE WHEN EXCLUDED.last_dt >= products.last_dt AND {own_prod}
                                   THEN EXCLUDED.category_id ELSE products.category_id END,
                first_dt = LEAST(products.first_dt, EXCLUDED.first_dt),
                last_dt = GREATEST(products.last_dt, EXCLUDED.last_dt),
                updated_at = now()""",
            [(pcode, sku, name, brand, seller, sid, cids[cat], f, l)
             for sku, name, brand, seller, sid, cat, f, l in sorted(prods, key=lambda x: x[0])])


class DiskFull(Exception):
    pass


def run_job(con, job, page_size, max_pages=None, pause=0.0, quota_each_page=False):
    url = endpoint(job["platform"], job["path"], job["d1"], job["d2"])
    con.execute("UPDATE mpstats.load_jobs SET status='running', started_at=COALESCE(started_at, now()), "
                "error=NULL WHERE job_id=%s", (job["job_id"],))
    start, pages = job["next_row"], 0
    while True:
        free_gb = shutil.disk_usage(cfg("DATA_DISK", str(BASE))).free / 1e9
        if free_gb < cfg("MIN_FREE_GB", 5.0):
            raise DiskFull(f"на диске осталось {free_gb:.1f} ГБ")
        data, code, secs = post_page(url, payload_for(job["metric"], start, page_size))
        items = data.get("data") or []
        total = data.get("total") or 0
        daily, prods, bad_len = parse_items(job, items)
        if daily:
            save(con, job, daily, prods)
        note = None
        if quota_each_page:
            q = get_quota()
            note = f"left={quota_left(q, job['platform'])}"
        con.execute("""INSERT INTO mpstats.api_log (job_id, url, start_row, http_status, items, total,
                       seconds, quota_note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (job["job_id"], url, start, code, len(items), total, round(secs, 2), note))
        start += len(items)
        pages += 1
        con.execute("""UPDATE mpstats.load_jobs SET next_row=%s, total=%s, pages=pages+1,
                       items=items+%s, daily_rows=daily_rows+%s WHERE job_id=%s""",
                    (start, total, len(items), len(daily), job["job_id"]))
        print(f"   стр.{pages}: {start}/{total} товаров, +{len(daily)} строк, {secs:.0f} c"
              + (f", длина графиков не совпала у {bad_len}" if bad_len else "")
              + (f", квота {note}" if note else ""))
        if len(items) < page_size or start >= total:
            break
        if max_pages and pages >= max_pages:
            return False
        time.sleep(pause)
    con.execute("UPDATE mpstats.load_jobs SET status='done', finished_at=now() WHERE job_id=%s",
                (job["job_id"],))
    return True


def fetch_jobs(con, platform=None, limit=None, job_id=None):
    q = """SELECT job_id, platform, segment, path, path_name, d1, d2, metric, next_row
           FROM mpstats.load_jobs WHERE status IN ('new','running','error')"""
    params = []
    if platform:
        q += " AND platform=%s"
        params.append(platform)
    if job_id:
        q += " AND job_id=%s"
        params.append(job_id)
    q += " ORDER BY platform DESC, d1 DESC, job_id"
    if limit:
        q += f" LIMIT {int(limit)}"
    cur = con.execute(q, params)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def claim_job(con, platform=None, job_id=None, segment=None):
    """Атомарно забирает следующее задание — безопасно при нескольких параллельных воркерах."""
    cond, params = "status = 'new'", []
    if platform:
        cond += " AND platform = %s"
        params.append(platform)
    if segment:
        cond += " AND segment = %s"
        params.append(segment)
    if job_id:
        cond += " AND job_id = %s"
        params.append(job_id)
    cur = con.execute(f"""
        UPDATE mpstats.load_jobs SET status = 'running', started_at = COALESCE(started_at, now())
        WHERE job_id = (SELECT job_id FROM mpstats.load_jobs WHERE {cond}
                        ORDER BY d1 DESC, job_id FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING job_id, platform, segment, path, path_name, d1, d2, metric, next_row""", params)
    row = cur.fetchone()
    return dict(zip([c.name for c in cur.description], row)) if row else None


def cmd_progress(_):
    """Таблица по годам: Ozon — категории верхнего уровня (и ниши в базе), WB — предметы."""
    with pg() as con:
        jobs = con.execute("""
            SELECT extract(year FROM d1)::int y, platform, path,
                   bool_and(status = 'done') done,
                   sum(CASE WHEN status='done' THEN 1 ELSE coalesce(next_row::numeric / nullif(total,0), 0) END)
                       / count(*) part
            FROM mpstats.load_jobs GROUP BY 1,2,3""").fetchall()
        # цифры берём из очереди заданий — без сканирования большой таблицы
        rows = dict(((y, p), int(n)) for y, p, n in con.execute("""
            SELECT extract(year FROM d1)::int, platform, sum(daily_rows)
            FROM mpstats.load_jobs GROUP BY 1,2"""))
        niches = {"ozon": con.execute("SELECT count(*) FROM mpstats.categories WHERE platform=2").fetchone()[0]}
    stat = {}
    for y, platform, path, done, part in jobs:
        s = stat.setdefault((y, platform), [0, 0, 0.0])
        s[0] += done
        s[1] += 1
        s[2] += float(part)
    print(f"Ниш Ozon в справочнике: {niches['ozon']}")
    print(f"{'Период':<8}| {'Ozon: категории (готово %)':<28}| {'строк':>11} || "
          f"{'WB: предметы (готово %)':<26}| {'строк':>11}")
    print("-" * 96)
    for y in sorted({k[0] for k in stat}, reverse=True):
        o = stat.get((y, "ozon"), [0, 0, 0])
        w = stat.get((y, "wb"), [0, 0, 0])
        ot = f"{o[0]}/{o[1]} ({o[2] / o[1] * 100:.0f}%)" if o[1] else "-"
        wt = f"{w[0]}/{w[1]} ({w[2] / w[1] * 100:.0f}%)" if w[1] else "-"
        print(f"{y:<8}| {ot:<28}| {rows.get((y, 'ozon'), 0):>11,} || "
              f"{wt:<26}| {rows.get((y, 'wb'), 0):>11,}")


def cmd_requeue(_):
    """Возвращает в очередь прерванные (running) и упавшие (error) задания; next_row сохраняется."""
    with pg() as con:
        n = con.execute("UPDATE mpstats.load_jobs SET status='new' WHERE status IN ('running','error')").rowcount
    print(f"Возвращено в очередь: {n}")


def cmd_run(args):
    page_size = cfg("PAGE_SIZE", 5000)
    reserve = cfg("QUOTA_RESERVE", 300)
    pause = cfg("PAUSE_SEC", 1.0)
    tag = f"[{args.platform or 'all'}#{os.getpid()}]"
    with pg() as con:
        n = 0
        while not args.limit or n < args.limit:
            job = claim_job(con, args.platform, args.job, SEGMENT_ALIASES.get(args.segment, args.segment))
            if not job:
                print(f"{tag} очередь пуста")
                return
            n += 1
            free_gb = shutil.disk_usage(cfg("DATA_DISK", str(BASE))).free / 1e9
            if free_gb < cfg("MIN_FREE_GB", 5.0):
                con.execute("UPDATE mpstats.load_jobs SET status='new' WHERE job_id=%s", (job["job_id"],))
                print(f"{tag} на диске осталось {free_gb:.1f} ГБ — останавливаюсь")
                return
            if n == 1 or n % 20 == 0:
                left = quota_left(get_quota(), job["platform"])
                print(f"{tag} квота {job['platform']}: осталось {left}")
                if left is not None and left < reserve:
                    con.execute("UPDATE mpstats.load_jobs SET status='new' WHERE job_id=%s", (job["job_id"],))
                    print(f"{tag} остаток квоты меньше резерва {reserve} — останавливаюсь")
                    return
            print(f"{tag} [{n}] job {job['job_id']} {job['platform']} {job['path_name']} "
                  f"{job['d1']}..{job['d2']} ({job['metric']})")
            try:
                run_job(con, job, page_size, pause=pause)
            except DiskFull as e:
                con.execute("UPDATE mpstats.load_jobs SET status='new' WHERE job_id=%s", (job["job_id"],))
                print(f"{tag} {e} — останавливаюсь")
                return
            except Exception as e:
                con.execute("UPDATE mpstats.load_jobs SET status='error', error=%s WHERE job_id=%s",
                            (str(e)[:2000], job["job_id"]))
                print(f"{tag}    ОШИБКА: {e}")
            time.sleep(pause)


def cmd_test_quota(args):
    """Один предмет, маленькие страницы, квота после каждой страницы."""
    with pg() as con:
        jobs = fetch_jobs(con, "wb", job_id=args.job) if args.job else None
        if not jobs:
            cur = con.execute("""SELECT job_id, platform, segment, path, path_name, d1, d2, metric, next_row
                                 FROM mpstats.load_jobs WHERE platform='wb' AND status='new'
                                 AND path='8465' ORDER BY d1 DESC LIMIT 1""")
            cols = [c.name for c in cur.description]
            jobs = [dict(zip(cols, r)) for r in cur.fetchall()]
        job = jobs[0]
        q0 = quota_left(get_quota(), "wb")
        print(f"Квота до: {q0}. Задание {job['job_id']}: {job['path_name']} {job['d1']}..{job['d2']}")
        run_job(con, job, page_size=args.page_size, max_pages=args.pages, quota_each_page=True)
        q1 = quota_left(get_quota(), "wb")
        print(f"Квота после: {q1}. Списано: {q0 - q1 if q0 is not None else '?'}")
        con.execute("UPDATE mpstats.load_jobs SET status='new', next_row=0, pages=0, items=0, "
                    "daily_rows=0 WHERE job_id=%s", (job["job_id"],))


def plan_segment(segment, until, subjects, ozon_paths, ozon_legacy=(), ozon_categories=()):
    """Задания сегмента за 2020 — until. Предметы WB, уже загруженные в другом сегменте, не перегружаются —
    им только меняется сегмент в справочнике. Повторный запуск безопасен: задания уникальны по (площадка, путь, период).
    ozon_categories — категории Ozon сегмента: они могли быть заведены раньше другим сегментом (ниша товара из чужой
    выгрузки), тогда новые строки легли бы в чужой сегмент — переносим их в этот."""
    with pg() as con:
        if ozon_categories:
            n = con.execute("UPDATE mpstats.categories SET segment=%s WHERE platform=2 AND name = ANY(%s) "
                            "AND segment <> %s", (segment, list(ozon_categories), segment)).rowcount
            print(f"Категорий Ozon перенесено в «{segment}»: {n}")
        loaded = {int(r[0]) for r in con.execute(
            "SELECT DISTINCT path FROM mpstats.load_jobs WHERE platform='wb' AND segment <> %s", (segment,))}
        moved = [sid for sid, _, _ in subjects if sid in loaded]
        if moved:
            con.execute("UPDATE mpstats.categories SET segment=%s WHERE platform=1 AND subject_id = ANY(%s)",
                        (segment, moved))
        jobs = []
        for sid, name, first in subjects:
            if sid in loaded:
                continue
            for d1, d2 in periods(until):
                if d2 >= dt.date.fromisoformat(first):
                    metric = "revenue_estimated" if d1 >= WB_ESTIMATED_FROM else "revenue"
                    jobs.append(("wb", segment, str(sid), name, d1, d2, metric))
        for path in ozon_paths:
            for d1, d2 in periods(until):
                jobs.append(("ozon", segment, path, path, d1, d2, "revenue"))
        for path, legacy_since, legacy_until in ozon_legacy:
            for d1, d2 in periods(until):
                if legacy_since <= d1 and d2 <= legacy_until:
                    jobs.append(("ozon", segment, path, path, d1, d2, "revenue"))
        con.cursor().executemany(
            """INSERT INTO mpstats.load_jobs (platform, segment, path, path_name, d1, d2, metric)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (platform, path, d1, d2) DO NOTHING""", jobs)
        new = con.execute("SELECT platform, count(*) FROM mpstats.load_jobs WHERE segment=%s AND status='new' "
                          "GROUP BY 1", (segment,)).fetchall()
    print(f"«{segment}»: предметов WB {len(subjects)} (уже загружены ранее, перенесены в сегмент: {len(moved)}), "
          f"путей Ozon {len(ozon_paths)}; заданий {len(jobs)}, ждут выгрузки: {dict(new)}")


def cmd_plan_garden(_):
    plan_segment(GARDEN_SEGMENT, GARDEN_UNTIL, wb_garden_subjects(), OZON_GARDEN_PATHS, OZON_GARDEN_LEGACY)


def cmd_plan_pets(_):
    plan_segment(PETS_SEGMENT, PETS_UNTIL, wb_pets_subjects(), OZON_PETS_PATHS,
                 [(path, since, until) for path, since, until, _, _ in OZON_PETS_LEGACY], OZON_PETS_PATHS)


def cmd_status(_):
    with pg() as con:
        for row in con.execute("""SELECT platform, status, count(*), sum(pages), sum(items), sum(daily_rows)
                                  FROM mpstats.load_jobs GROUP BY 1,2 ORDER BY 1,2"""):
            print("jobs", row)
        for row in con.execute("""SELECT platform, count(*), min(dt), max(dt), round(sum(revenue)/1e9,2)
                                  FROM mpstats.sales_daily GROUP BY 1"""):
            print("sales_daily (строк, с, по, выручка млрд)", row)
        for row in con.execute("""SELECT job_id, path_name, d1, left(error, 200) FROM mpstats.load_jobs
                                  WHERE status='error' LIMIT 10"""):
            print("error", row)
    print("quota", get_quota())


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init-db")
    sp.add_parser("quota")
    p = sp.add_parser("discover")
    p.add_argument("--fallback", action="store_true")
    p = sp.add_parser("plan")
    p.add_argument("--d2", help="конец периода, по умолчанию вчера")
    p = sp.add_parser("run")
    p.add_argument("--platform", choices=["wb", "ozon"])
    p.add_argument("--limit", type=int)
    p.add_argument("--job", type=int)
    p.add_argument("--segment", help="только задания сегмента: garden, pets или полное название")
    p = sp.add_parser("test-quota")
    p.add_argument("--job", type=int)
    p.add_argument("--page-size", type=int, default=200)
    p.add_argument("--pages", type=int, default=3)
    sp.add_parser("status")
    sp.add_parser("requeue")
    sp.add_parser("progress")
    sp.add_parser("plan-garden")
    sp.add_parser("plan-pets")
    args = ap.parse_args()
    load_env()
    {"init-db": cmd_init_db, "quota": cmd_quota, "discover": cmd_discover, "plan": cmd_plan,
     "run": cmd_run, "test-quota": cmd_test_quota, "status": cmd_status,
     "requeue": cmd_requeue, "progress": cmd_progress,
     "plan-garden": cmd_plan_garden, "plan-pets": cmd_plan_pets}[args.cmd](args)


if __name__ == "__main__":
    main()
