"""
Сайт с таблицей продаж MPSTATS: фильтры, сортировка, сводка, графики, выгрузка в Excel/CSV.

  python -m uvicorn app:app --app-dir web --host 0.0.0.0 --port 8000

Читает файл DuckDB, собранный web/build_duckdb.py (путь — DUCKDB_PATH в .env, по умолчанию data/mpstats.duckdb).
Если в .env задан WEB_PASSWORD (и, по желанию, WEB_USER), сайт открывается только после входа (страница /login).

Как устроено: на каждый набор фильтров таблица «артикул × период» считается один раз и кладётся в кэш
(в памяти DuckDB). Листание страниц, сортировка, сводка, графики и выгрузка берут данные из кэша.
"""
import asyncio
import collections
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote

import duckdb
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
DB_PATH = Path(os.environ.get("DUCKDB_PATH", BASE / "data" / "mpstats.duckdb"))
CACHE_ROWS = int(os.environ.get("WEB_CACHE_ROWS", 30_000_000))
HAS_MONTHLY = False
HAS_OUTLIERS = False  # файл собран с выбросами (sales.outlier, таблица outliers)
EXCEL_MAX_ROWS = 1_048_575  # лимит листа Excel минус строка заголовка
ESTIMATED_FROM = "2025-09-01"  # WB с этой даты отдаёт оценочные выручку и заказы
OTHER_SEGMENT = "Прочее"       # случайно попавшие категории (см. categories.py), по умолчанию выключен в фильтре

# ---------------------------------------------------------------- столбцы таблицы
# key: (заголовок, тип, выражение в кэш-таблице r)
COLUMNS = {
   "platform":   ("Площадка",             "enum",   "CASE platform WHEN 1 THEN 'Wildberries' ELSE 'Ozon' END"),
    "sku":        ("Артикул",              "int",    "sku"),
    "name":       ("Наименование",         "text",   "name"),
    "brand":      ("Бренд",                "dim",    "brand"),
    "seller":     ("Продавец",             "dim",    "seller"),
    "category":   ("Категория",            "dim",    "category"),
    "segment":    ("Сегмент",              "enum",   "segment"),
    "revenue":    ("Выручка, ₽",           "num",    "revenue"),
    "orders":     ("Заказы, шт",           "num",    "orders"),
    "avg_price":  ("Средняя цена, ₽",      "num",    "avg_price"),
    "days":       ("Дней с продажами",     "num",    "days"),
    "first_dt":   ("Первая продажа",       "date",   "first_dt"),
    "last_dt":    ("Последняя продажа",    "date",   "last_dt"),
    "anomaly":    ("Аномальный период",    "bool",   "anomaly"),
    "outlier":    ("Есть выбросы",         "bool",   "outlier"),
}
NUM_COLS = [k for k, v in COLUMNS.items() if v[1] == "num"]

# списки в фильтрах: поле -> (выражение значения по строке продаж или таблицы «артикул × период» s, соединение)
CAT_JOIN = "JOIN d.categories c ON c.category_id = s.category_id"
FACET_EXPR = {
    "brand":    ("s.brand_id", ""),
    "seller":   ("s.seller_id", ""),
    "platform": ("s.platform", ""),
    "category": ("c.group_id", CAT_JOIN),
    "segment":  ("c.site_segment_id", CAT_JOIN),
}
DIM_FIELDS = {"brand", "seller", "category"}  # поля с режимом «все, кроме»


# ---------------------------------------------------------------- запросы
class NumFilter(BaseModel):
    op: Literal["gt", "gte", "lt", "lte", "eq", "ne", "between"]
    a: float
    b: float | None = None


class TextFilter(BaseModel):
    op: Literal["contains", "not_contains"] = "contains"
    value: str


class Filters(BaseModel):
    d1: dt.date
    d2: dt.date
    platform: list[int] = []
    segment: list[int] = []
    category: list[int] = []
    category_exclude: bool = False
    brand: list[int] = []
    brand_exclude: bool = False
    seller: list[int] = []
    seller_exclude: bool = False
    exclude_anomaly: bool = False
    exclude_outlier: bool = False
    sku: list[int] = []
    name: TextFilter | None = None
    num: dict[str, NumFilter] = {}

    def key(self) -> str:
        return hashlib.sha1(self.model_dump_json().encode()).hexdigest()[:16]


class RowsRequest(BaseModel):
    filters: Filters
    sort: str = "revenue"
    desc: bool = True
    page: int = Field(1, ge=1)
    page_size: int = Field(100, ge=1, le=1000)


def ids(values):
    return ", ".join(str(int(v)) for v in values)


def where_rows(f: Filters, d1, d2):
    """Условия на строки таблиц sales / sales_m (до сложения по периоду) и параметры к ним."""
    cond = ["s.dt BETWEEN ? AND ?"]
    params = [d1, d2]
    if f.platform:
        cond.append(f"s.platform IN ({ids(f.platform)})")
    if f.segment:  # сегмент на сайте (site_segment_id), он может отличаться от исходного в s.segment_id
        cond.append(f"s.category_id IN (SELECT category_id FROM d.categories "
                    f"WHERE site_segment_id IN ({ids(f.segment)}))")
    if f.category:  # в фильтре — единые категории (группы путей), см. categories.py
        cond.append(f"s.category_id {'NOT IN' if f.category_exclude else 'IN'} "
                    f"(SELECT category_id FROM d.categories WHERE group_id IN ({ids(f.category)}))")
    if f.brand:
        cond.append(f"s.brand_id {'NOT IN' if f.brand_exclude else 'IN'} ({ids(f.brand)})")
    if f.seller:
        cond.append(f"s.seller_id {'NOT IN' if f.seller_exclude else 'IN'} ({ids(f.seller)})")
    if f.exclude_anomaly:
        cond.append("NOT s.anomaly")
    if f.exclude_outlier and HAS_OUTLIERS:
        cond.append("NOT s.outlier")
    if f.sku:
        cond.append(f"s.sku IN ({ids(f.sku)})")
    if f.name and f.name.value.strip():
        neg = "NOT " if f.name.op == "not_contains" else ""
        cond.append(f"{neg}EXISTS (SELECT 1 FROM d.products p WHERE p.platform = s.platform "
                    f"AND p.sku = s.sku AND p.name ILIKE ?)")
        params.append("%" + f.name.value.strip() + "%")
    return " AND ".join(cond), params


DAILY_COLS = ("s.dt, s.platform, s.sku, s.category_id, s.brand_id, s.seller_id, s.revenue, s.orders, "
              "1 AS days, s.dt AS first_dt, s.dt AS last_dt, s.anomaly, {outlier}")
MONTHLY_COLS = ("s.dt, s.platform, s.sku, s.category_id, s.brand_id, s.seller_id, s.revenue, s.orders, "
                "s.days, s.first_dt, s.last_dt, s.anomaly, {outlier}")


def month_add(d: dt.date, n: int) -> dt.date:
    m = d.year * 12 + d.month - 1 + n
    return dt.date(m // 12, m % 12 + 1, 1)


def source(f: Filters, daily_only=False):
    """Отфильтрованные строки за период: полные месяцы — из помесячной таблицы sales_m (в ~6 раз меньше),
    неполные крайние дни — из дневной sales. Для группировки по неделям — только дневная."""
    m1 = f.d1 if f.d1.day == 1 else month_add(f.d1, 1)       # первый полный месяц
    m_end = month_add(f.d2 + dt.timedelta(days=1), 0)          # начало месяца после последнего полного
    parts, params = [], []

    def add(table, cols, d1, d2):
        w, p = where_rows(f, d1, d2)
        cols = cols.format(outlier="s.outlier" if HAS_OUTLIERS else "false AS outlier")
        parts.append(f"SELECT {cols} FROM d.{table} s WHERE {w}")
        params.extend(p)

    if daily_only or m1 >= m_end or not HAS_MONTHLY:
        add("sales", DAILY_COLS, f.d1, f.d2)
    else:
        if f.d1 < m1:
            add("sales", DAILY_COLS, f.d1, m1 - dt.timedelta(days=1))
        add("sales_m", MONTHLY_COLS, m1, month_add(m_end, -1))
        if m_end <= f.d2:
            add("sales", DAILY_COLS, m_end, f.d2)
    return "(" + " UNION ALL ".join(parts) + ")", params


NUM_OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "<>"}


def where_agg(f: Filters):
    """Условия на сложенные за период значения (выручка > 1 млн и т.п.)."""
    cond, params = [], []
    for col, nf in f.num.items():
        if col not in NUM_COLS:
            raise HTTPException(400, f"Неизвестный столбец {col}")
        if nf.op == "between":
            lo, hi = sorted([nf.a, nf.b if nf.b is not None else nf.a])
            cond.append(f"{col} BETWEEN ? AND ?")
            params += [lo, hi]
        else:
            cond.append(f"{col} {NUM_OPS[nf.op]} ?")
            params.append(nf.a)
    return (" AND ".join(cond) or "true"), params


# ---------------------------------------------------------------- хранилище и кэш
class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.key_locks = collections.defaultdict(threading.Lock)
        self.con = None
        self.mtime = None
        self.checked = 0.0
        self.cache = collections.OrderedDict()  # key -> имя таблицы
        self.open()

    def open(self):
        if not self.path.exists():
            raise RuntimeError(f"Нет файла {self.path}. Соберите его: python web/build_duckdb.py")
        con = duckdb.connect(":memory:")
        tmp = Path(tempfile.gettempdir()) / "mpstats_web_duckdb"
        con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
        con.execute(f"SET memory_limit = '{os.environ.get('DUCKDB_MEMORY', '6GB')}'")
        con.execute("SET preserve_insertion_order = false")  # без этого CREATE TABLE AS в разы медленнее
        # потолок временных файлов: без него тяжёлый запрос может занять весь свободный диск
        con.execute(f"SET max_temp_directory_size = '{os.environ.get('DUCKDB_TEMP_MAX', '3GB')}'")
        con.execute(f"ATTACH '{self.path.as_posix()}' AS d (READ_ONLY)")
        # кэш результатов — в отдельном файле на диске (сжато), а не в памяти: результат
        # «всё время × месяц» — это десятки миллионов строк
        cache_file = tmp / f"cache_{os.getpid()}.duckdb"
        tmp.mkdir(parents=True, exist_ok=True)
        for old_file in tmp.glob("cache_*.duckdb*"):
            try:
                old_file.unlink()
            except OSError:
                pass  # файл занят другим запущенным процессом сайта
        con.execute(f"ATTACH '{cache_file.as_posix()}' AS cache")
        global HAS_MONTHLY, HAS_OUTLIERS  # файл, собранный старой версией build_duckdb.py, может быть без них
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'd'").fetchall()}
        HAS_MONTHLY = "sales_m" in tables
        HAS_OUTLIERS = "outliers" in tables
        old = self.con
        self.con, self.mtime, self.cache = con, self.path.stat().st_mtime, collections.OrderedDict()
        if old is not None:
            old.close()

    def cursor(self):
        now = time.time()
        if now - self.checked > 30:  # раз в 30 с проверяем, не пересобран ли файл
            self.checked = now
            with self.lock:
                if self.path.stat().st_mtime != self.mtime:
                    self.open()
        return self.con.cursor()

    def result(self, f: Filters):
        """Имя кэш-таблицы с результатом «артикул × период» для фильтров f (считает при необходимости)."""
        key = f.key()
        with self.key_locks[key]:
            with self.lock:
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return self.cache[key][0], True
            name = f"r_{key}"
            src, ps = source(f)
            wa, pa = where_agg(f)
            cur = self.cursor()
            # два шага: сначала сложение по артикулу и периоду (самое тяжёлое), потом подстановка
            # названий — так DuckDB не держит в памяти одновременно группировку и соединения
            # В кэше только числа и id: названия (товар, бренд, продавец, категория) занимают в разы
            # больше памяти, их подставляем при показе страницы и выгрузке — см. named()
            cur.execute(f"""
                CREATE OR REPLACE TABLE cache.main.{name} AS
                SELECT a.*, c.segment_id FROM (
                    SELECT *, round(revenue / nullif(orders, 0))::BIGINT AS avg_price FROM (
                        SELECT s.platform, s.sku,
                               sum(s.revenue)              AS revenue,
                               sum(s.orders)               AS orders,
                               sum(s.days)                 AS days,
                               min(s.first_dt)             AS first_dt,
                               max(s.last_dt)              AS last_dt,
                               max(s.category_id)          AS category_id,
                               any_value(s.brand_id)       AS brand_id,
                               any_value(s.seller_id)      AS seller_id,
                               bool_or(s.anomaly)          AS anomaly,
                               bool_or(s.outlier)          AS outlier
                        FROM {src} s
                        GROUP BY ALL
                    )
                ) a
                LEFT JOIN (SELECT category_id, site_segment_id AS segment_id FROM d.categories) c ON c.category_id = a.category_id
                WHERE {wa}
            """, ps + pa)
            self.remember(cur, key, name)
            return name, False

    def remember(self, cur, key, name):
        n = cur.execute(f"SELECT count(*) FROM cache.main.{name}").fetchone()[0]
        with self.lock:
            self.cache[key] = (name, n)
            # держим в памяти не больше CACHE_ROWS строк результатов, старые выбрасываем
            while len(self.cache) > 1 and sum(r for _, r in self.cache.values()) > CACHE_ROWS:
                _, (old, _) = self.cache.popitem(last=False)
                cur.execute(f"DROP TABLE IF EXISTS cache.main.{old}")

    def facet(self, f: Filters, field: str):
        """Имя кэш-таблицы (id, revenue) — значения поля field и их выручка под фильтры f без фильтра
        по самому полю (для списков в фильтрах)."""
        ff = f.model_copy(update={field: []} | ({f"{field}_exclude": False} if field in DIM_FIELDS else {}))
        key = f"{ff.key()}_{field}"
        with self.key_locks[key]:
            with self.lock:
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return self.cache[key][0], True
                have_rows = ff.key() in self.cache
            name = f"v_{key}"
            cur = self.cursor()
            expr, join = FACET_EXPR[field]
            if have_rows or ff.num:
                # таблица «артикул × период» уже посчитана (обычно — своего фильтра у поля нет,
                # это та же таблица, что в основной выдаче) или нужна ради условий на суммы (выручка > …)
                rows, _ = self.result(ff)
                sql, ps = f"FROM cache.main.{rows} s {join}", []
            else:
                # иначе — сразу из продаж: сложение по нескольким тысячам значений вместо миллионов
                # артикулов, в разы быстрее и легче по памяти
                src, ps = source(ff)
                sql = f"FROM {src} s {join}"
            cur.execute(f"CREATE OR REPLACE TABLE cache.main.{name} AS "
                        f"SELECT {expr} AS id, sum(s.revenue) AS revenue {sql} GROUP BY 1", ps)
            self.remember(cur, key, name)
            return name, False


store = None


def get_store():
    global store
    if store is None:
        store = Store(DB_PATH)
    return store


# ---------------------------------------------------------------- приложение
app = FastAPI(title="MPSTATS — продажи")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ---------------------------------------------------------------- вход по паролю
# Пароль (и, по желанию, логин) — в .env. После входа браузер получает подписанную cookie на WEB_SESSION_DAYS дней.
# Подпись зависит от пароля: сменили пароль в .env и перезапустили сайт — все входы сбрасываются.
WEB_USER = os.environ.get("WEB_USER", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
AUTH = bool(WEB_PASSWORD)
SESSION_DAYS = int(os.environ.get("WEB_SESSION_DAYS", 30))
SESSION_COOKIE = "mpstats_session"
_SECRET = hashlib.sha256(f"{os.environ.get('WEB_SECRET', '')}|{WEB_USER}|{WEB_PASSWORD}".encode()).digest()
PUBLIC = ("/login", "/logout", "/static/style.css", "/static/common.js")
# защита от подбора: не больше LOGIN_MAX_FAILS неудачных попыток с одного адреса за LOGIN_WINDOW секунд
LOGIN_MAX_FAILS, LOGIN_WINDOW = 10, 15 * 60
_fails: dict[str, list[float]] = collections.defaultdict(list)


def _sign(expires: int) -> str:
    return hmac.new(_SECRET, f"{WEB_USER}|{expires}".encode(), hashlib.sha256).hexdigest()


def session_ok(request: Request) -> bool:
    exp, _, sig = request.cookies.get(SESSION_COOKIE, "").partition(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(int(exp)))


def safe_next(url: str) -> str:
    """Куда вернуть после входа — только адрес этого же сайта."""
    return url if url.startswith("/") and not url.startswith("//") and not url.startswith("/login") else "/"


def login_page(error: str = "", next_url: str = "/", status_code: int = 200, user: str = ""):
    html = page("login.html").body.decode()
    html = html.replace("{{error}}", f'<div class="login-error" role="alert">{error}</div>' if error else "")
    attr = lambda v: v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    html = html.replace("{{next}}", attr(next_url)).replace("{{user}}", attr(user))
    html = html.replace("{{user_hidden}}", "" if WEB_USER else "hidden")
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not AUTH or path in PUBLIC or session_ok(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Нужно войти"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if not AUTH or session_ok(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return login_page(next_url=safe_next(next))


@app.post("/login")
async def login(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    next_url = safe_next(form.get("next", "/"))
    ip = request.client.host if request.client else "?"
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < LOGIN_WINDOW]
    if len(_fails[ip]) >= LOGIN_MAX_FAILS:
        return login_page("Слишком много попыток. Попробуйте через 15 минут.", next_url, 429)
    ok = hmac.compare_digest(form.get("user", "").strip() if WEB_USER else "", WEB_USER) &         hmac.compare_digest(form.get("password", ""), WEB_PASSWORD)
    if not ok:
        _fails[ip].append(now)
        await asyncio.sleep(1)  # медленнее перебор
        return login_page("Неверный логин или пароль" if WEB_USER else "Неверный пароль", next_url, 401,
                          form.get("user", ""))
    _fails.pop(ip, None)
    exp = int(now) + SESSION_DAYS * 86400
    r = RedirectResponse(next_url, status_code=303)
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(SESSION_COOKIE, f"{exp}.{_sign(exp)}", max_age=SESSION_DAYS * 86400,
                 httponly=True, samesite="lax", secure=https)
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(SESSION_COOKIE)
    return r


def page(name):
    """HTML-страница со ссылками на стили/скрипт, помеченными версией файла: после правки
    браузер сразу берёт свежий файл, а не старый из кэша."""
    html = (STATIC / name).read_text(encoding="utf-8")
    for asset in ("style.css", "common.js"):
        v = int((STATIC / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
    if AUTH and name != "login.html":
        html = html.replace('<button type="button" class="theme-btn"',
                            '<a class="logout" href="/logout">Выйти</a><button type="button" class="theme-btn"', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return page("index.html")


@app.get("/sku/{platform}/{sku}")
def sku_page(platform: int, sku: int):
    return page("sku.html")


@app.get("/api/sku")
def sku_info(platform: int, sku: int):
    """Всё по одному артикулу: карточка и дневные продажи за всю историю
    (их немного — до нескольких тысяч строк; период и группировку выбирает страница)."""
    t = time.time()
    cur = get_store().cursor()
    card = cur.execute("""
        SELECT p.name, b.name, s.name, p.brand_id, p.seller_id FROM d.products p
        LEFT JOIN d.brands b USING (brand_id) LEFT JOIN d.sellers s USING (seller_id)
        WHERE p.platform = ? AND p.sku = ?
    """, [platform, sku]).fetchone()
    out = ("o.rule, o.ratio, o.ref", "LEFT JOIN d.outliers o ON o.platform = s.platform AND o.sku = s.sku AND o.dt = s.dt")         if HAS_OUTLIERS else ("NULL, NULL, NULL", "")
    days = cur.execute(f"""
        SELECT s.dt, s.revenue, s.orders, s.price, s.anomaly, c.display, g.name, c.group_id, g.segment_id, {out[0]}
        FROM d.sales s
        LEFT JOIN d.categories c USING (category_id) LEFT JOIN d.segments g ON g.segment_id = c.site_segment_id
        {out[1]}
        WHERE s.platform = ? AND s.sku = ? ORDER BY s.dt
    """, [platform, sku]).fetchall()
    if not card and not days:
        raise HTTPException(404, "Артикул не найден")
    # id — те же, что у фильтров главной страницы (категория = group_id, сегмент = site_segment_id)
    cats = [{"id": i, "name": n} for i, n in dict.fromkeys((d[7], d[5]) for d in days if d[5])]
    segs = [{"id": i, "name": n} for i, n in dict.fromkeys((d[8], d[6]) for d in days if d[6])]
    return {
        "platform": platform, "sku": sku,
        "name": card[0] if card else None, "brand": card[1] if card else None, "seller": card[2] if card else None,
        "brand_id": card[3] if card else None, "seller_id": card[4] if card else None,
        "categories": cats, "segments": segs, "estimated_from": ESTIMATED_FROM,
        # [дата, выручка, заказы, цена, аномалия, выброс: null или [правило, во сколько раз, с чем сравнивали]]
        "days": [[d[0].isoformat(), d[1], d[2], d[3], d[4], [d[9], d[10], d[11]] if d[9] else None] for d in days],
        "seconds": round(time.time() - t, 2),
    }


@app.get("/api/meta")
def meta():
    cur = get_store().cursor()
    built_at, min_dt, max_dt, rows = cur.execute("SELECT built_at::TIMESTAMP, min_dt, max_dt, rows FROM d.meta").fetchone()
    segments = cur.execute("SELECT segment_id, name FROM d.segments ORDER BY name = ?, name",
                           [OTHER_SEGMENT]).fetchall()
    return {
        "built_at": built_at.isoformat(), "min_dt": min_dt.isoformat(), "max_dt": max_dt.isoformat(),
        "rows": rows, "estimated_from": ESTIMATED_FROM, "excel_max_rows": EXCEL_MAX_ROWS, "has_outliers": HAS_OUTLIERS,
        "platforms": [{"id": 1, "name": "Wildberries"}, {"id": 2, "name": "Ozon"}],
        "segments": [{"id": i, "name": n, "default_off": n == OTHER_SEGMENT} for i, n in segments],
        "columns": [{"key": k, "label": v[0], "type": v[1]} for k, v in COLUMNS.items()],
    }


ALL_IDS_MAX = 50_000


class ValuesRequest(BaseModel):
    field: Literal["brand", "seller", "category", "platform", "segment"]
    filters: Filters
    q: str = ""
    limit: int = Field(200, ge=1, le=1000)
    ids: list[int] = []        # id, которые нужно вернуть обязательно (уже выбранные галочки)
    all_ids: bool = False      # вернуть id всех найденных (для «Выбрать все» при поиске)


FACET_DICT = {"brand": ("brands", "brand_id"), "seller": ("sellers", "seller_id"), "category": ("cat_groups", "group_id")}


@app.post("/api/values")
def values(req: ValuesRequest):
    """Значения для фильтра-списка с учётом периода и всех остальных фильтров (кроме фильтра по этому же полю):
    выбран продавец и период — в списке брендов только бренды этого продавца за период.
    Выручка у значения — за выбранный период с учётом остальных фильтров. См. Store.facet."""
    t = time.time()
    field = req.field
    st = get_store()
    name, cached = st.facet(req.filters, field)
    cur = st.cursor()

    if field not in FACET_DICT:  # маркетплейс, сегмент — короткие списки: только выручка по значениям
        rows = cur.execute(f"SELECT id, revenue FROM cache.main.{name} WHERE id IS NOT NULL").fetchall()
        return {"items": [{"id": i, "revenue": int(r or 0)} for i, r in rows],
                "cached": cached, "seconds": round(time.time() - t, 2)}

    table, key = FACET_DICT[field]
    extra = ", n.platforms" if field == "category" else ", NULL"
    facet = f"cache.main.{name}"
    pattern = "%" + req.q.strip() + "%"
    found_sql = (f"FROM {facet} t JOIN d.{table} n ON n.{key} = t.id WHERE n.name ILIKE ? "
                 f"ORDER BY t.revenue DESC NULLS LAST, n.name")
    if req.all_ids:
        found = cur.execute(f"SELECT t.id, n.name {found_sql} LIMIT {ALL_IDS_MAX + 1}", [pattern]).fetchall()
        return {"items": [{"id": i, "name": n} for i, n in found[:ALL_IDS_MAX]],
                "truncated": len(found) > ALL_IDS_MAX}
    # count(*) OVER () считается до LIMIT — общее число найденных за тот же проход
    rows = cur.execute(f"SELECT t.id, n.name, coalesce(t.revenue, 0) {extra}, count(*) OVER () {found_sql} LIMIT ?",
                       [pattern, req.limit]).fetchall()
    total = rows[0][4] if rows else 0
    selected = []
    if req.ids:  # выбранные показываем, даже если под остальные фильтры они уже не попадают (с нулём)
        selected = cur.execute(f"SELECT n.{key}, n.name, coalesce(t.revenue, 0) {extra} FROM d.{table} n "
                               f"LEFT JOIN {facet} t ON t.id = n.{key} WHERE n.{key} IN ({ids(req.ids[:300])})"
                               ).fetchall()

    def pack(r):
        return {"id": r[0], "name": r[1], "revenue": r[2], "platform": r[3]}
    return {"items": [pack(r) for r in rows], "selected": [pack(r) for r in selected], "total": total,
            "cached": cached, "seconds": round(time.time() - t, 2)}


def check_sort(sort, f: Filters):
    if sort not in COLUMNS:
        raise HTTPException(400, f"Нельзя сортировать по {sort}")
    return COLUMNS[sort][2]


TEXT_SORT = {"name", "brand", "seller", "category", "segment"}  # для сортировки нужны названия


def named(rel):
    """Строки кэш-таблицы (или выборки из неё) с подставленными названиями."""
    return f"""(SELECT r.*, p.name, br.name AS brand, se.name AS seller, c.display AS category, g.name AS segment
        FROM {rel} r
        LEFT JOIN d.products p ON p.platform = r.platform AND p.sku = r.sku
        LEFT JOIN d.brands br ON br.brand_id = r.brand_id
        LEFT JOIN d.sellers se ON se.seller_id = r.seller_id
        LEFT JOIN d.categories c ON c.category_id = r.category_id
        LEFT JOIN d.segments g ON g.segment_id = r.segment_id)"""


def order_by(sort, desc):
    return f"{COLUMNS[sort][2]} {'DESC' if desc else 'ASC'} NULLS LAST, revenue DESC, sku"


@app.post("/api/rows")
def rows(req: RowsRequest):
    t = time.time()
    st = get_store()
    name, cached = st.result(req.filters)
    check_sort(req.sort, req.filters)
    cols = list(COLUMNS)
    select = ", ".join(COLUMNS[k][2] + " AS " + k for k in cols)
    order = order_by(req.sort, req.desc)
    page = [req.page_size, (req.page - 1) * req.page_size]
    cur = st.cursor()
    if req.sort in TEXT_SORT:
        # сортировка по тексту: названия нужны для всех строк
        data = cur.execute(f"SELECT {select} FROM {named('cache.main.' + name)} ORDER BY {order} "
                           f"LIMIT ? OFFSET ?", page).fetchall()
    else:
        # сначала страница по числам из кэша, названия — только для её строк
        data = cur.execute(f"""
            SELECT {select} FROM {named(f"(SELECT * FROM cache.main.{name} ORDER BY {order} LIMIT ? OFFSET ?)")}
            ORDER BY {order}
        """, page).fetchall()
    total = cur.execute(f"SELECT count(*) FROM cache.main.{name}").fetchone()[0]
    return {"columns": cols, "rows": [list(r) for r in data], "total": total,
            "cached": cached, "seconds": round(time.time() - t, 2)}


def chart_grain(f: Filters, grain: str = "auto"):
    if grain != "auto":
        return grain
    days = (f.d2 - f.d1).days + 1
    return "day" if days <= 62 else "week" if days <= 370 else "month"


def shares(cur, name, field, top=10):
    key = f"{field}_id"
    rows = cur.execute(f"""
        WITH t AS (SELECT {key} AS id, sum(revenue) AS revenue FROM cache.main.{name}
                   GROUP BY 1 ORDER BY revenue DESC NULLS LAST LIMIT ?)
        SELECT t.id, n.name, t.revenue FROM t LEFT JOIN d.{field}s n ON n.{key} = t.id
        ORDER BY t.revenue DESC NULLS LAST
    """, [top]).fetchall()
    total = cur.execute(f"SELECT sum(revenue) FROM cache.main.{name}").fetchone()[0] or 0
    items = [{"id": i, "name": n, "revenue": int(r or 0)} for i, n, r in rows]
    rest = int(total) - sum(x["revenue"] for x in items)
    if rest > 0:
        items.append({"id": None, "name": "Остальные", "revenue": rest})
    return items


@app.post("/api/summary")
def summary(f: Filters, grain: Literal["auto", "day", "week", "month"] = "auto"):
    t = time.time()
    st = get_store()
    name, cached = st.result(f)
    cur = st.cursor()
    r = cur.execute(f"""
        SELECT count(*), sum(revenue), sum(orders), count(DISTINCT (platform, sku)),
               count(DISTINCT brand_id), count(DISTINCT seller_id), avg(revenue),
               bool_or(platform = 1 AND last_dt >= DATE '{ESTIMATED_FROM}'), bool_or(anomaly), bool_or(outlier)
        FROM cache.main.{name}
    """).fetchone()
    totals = {
        "rows": r[0], "revenue": int(r[1] or 0), "orders": int(r[2] or 0), "products": r[3],
        "brands": r[4], "sellers": r[5], "avg_revenue": round(r[6] or 0),
        "avg_check": round((r[1] or 0) / r[2]) if r[2] else None,
        "has_estimated": bool(r[7]), "has_anomaly": bool(r[8]), "has_outlier": bool(r[9]),
    }

    # динамика: по строкам тех артикулов, что прошли фильтр
    grain = chart_grain(f, grain)
    src, ps = source(f, daily_only=grain != "month")
    semi = ""
    if f.num:
        semi = f"SEMI JOIN cache.main.{name} r ON r.platform = s.platform AND r.sku = s.sku"
    series = cur.execute(f"""
        SELECT date_trunc('{grain}', s.dt)::DATE AS b, sum(s.revenue), sum(s.orders)
        FROM {src} s {semi}
        GROUP BY 1 ORDER BY 1
    """, ps).fetchall()
    return {
        "totals": totals,
        "series": {"grain": grain, "points": [
            {"t": b.isoformat(), "revenue": int(rv or 0), "orders": int(o or 0),
             "avg_check": round(rv / o) if o else None} for b, rv, o in series]},
        "brands": shares(cur, name, "brand"),
        "sellers": shares(cur, name, "seller"),
        "cached": cached, "seconds": round(time.time() - t, 2),
    }


def temp_file(suffix):
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(name)


class ExportRequest(BaseModel):
    filters: Filters
    sort: str = "revenue"
    desc: bool = True
    format: Literal["xlsx", "csv"] = "xlsx"


@app.post("/api/export")
def export(req: ExportRequest):
    st = get_store()
    name, _ = st.result(req.filters)
    check_sort(req.sort, req.filters)
    cols = list(COLUMNS)
    select = ", ".join(f'{COLUMNS[k][2]} AS "{COLUMNS[k][0]}"' for k in cols)
    sql = f"SELECT {select} FROM {named('cache.main.' + name)} ORDER BY {order_by(req.sort, req.desc)}"
    stamp = time.strftime("%Y%m%d_%H%M")
    cur = st.cursor()

    if req.format == "csv":
        path = temp_file(".csv")
        cur.execute(f"COPY ({sql}) TO '{path.as_posix()}' (HEADER, DELIMITER ';')")

        def stream():
            try:
                yield "﻿".encode()  # BOM — чтобы Excel открыл кириллицу
                with open(path, "rb") as fh:
                    while chunk := fh.read(1 << 20):
                        yield chunk
            finally:
                path.unlink(missing_ok=True)
        return StreamingResponse(stream(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="mpstats_{stamp}.csv"'})

    def stream_file(path):
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    yield chunk
        finally:
            path.unlink(missing_ok=True)
    xlsx_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    xlsx_disp = {"Content-Disposition": f'attachment; filename="mpstats_{stamp}.xlsx"'}

    # Быстрый путь: модуль excel в DuckDB (C++) — 1 млн строк примерно за 30 с против 90 с у xlsxwriter
    try:
        cur.execute("LOAD excel")
        select_x = ", ".join(
            (f"CASE WHEN {COLUMNS[k][2]} THEN 'да' ELSE '' END" if COLUMNS[k][1] == "bool" else COLUMNS[k][2])
            + f' AS "{COLUMNS[k][0]}"' for k in cols)
        path = temp_file(".xlsx")
        path.unlink()  # COPY создаёт файл сам
        cur.execute(f"COPY (SELECT {select_x} FROM {named('cache.main.' + name)} "
                    f"ORDER BY {order_by(req.sort, req.desc)} LIMIT {EXCEL_MAX_ROWS}) "
                    f"TO '{path.as_posix()}' (FORMAT xlsx, HEADER true, SHEET 'Данные')")
        return StreamingResponse(stream_file(path), media_type=xlsx_type, headers=xlsx_disp)
    except duckdb.Error:
        pass  # модуль excel не установлен — пишем через xlsxwriter

    import xlsxwriter
    path = temp_file(".xlsx")
    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    ws = wb.add_worksheet("Данные")
    head = wb.add_format({"bold": True, "bg_color": "#EEEEEE"})
    fmt_date = wb.add_format({"num_format": "dd.mm.yyyy"})
    fmt_num = wb.add_format({"num_format": "#,##0"})
    for i, k in enumerate(cols):
        ws.write(0, i, COLUMNS[k][0], head)
        ws.set_column(i, i, 45 if k == "name" else 22 if COLUMNS[k][1] in ("dim", "text") else 14)
    ws.freeze_panes(1, 0)
    fmts = [fmt_date if COLUMNS[k][1] == "date" else fmt_num if COLUMNS[k][1] == "num" else None for k in cols]
    cur.execute(sql + f" LIMIT {EXCEL_MAX_ROWS}")
    row = 1
    while batch := cur.fetchmany(50_000):
        for rec in batch:
            for i, v in enumerate(rec):
                if v is None:
                    continue
                if fmts[i] is fmt_date:
                    ws.write_datetime(row, i, dt.datetime.combine(v, dt.time()), fmt_date)
                elif fmts[i] is not None:
                    ws.write_number(row, i, v, fmts[i])
                elif isinstance(v, bool):
                    ws.write_string(row, i, "да" if v else "")
                elif isinstance(v, int):
                    ws.write_number(row, i, v)
                else:
                    ws.write_string(row, i, str(v))
            row += 1
    ws.autofilter(0, 0, max(row - 1, 1), len(cols) - 1)
    wb.close()
    return StreamingResponse(stream_file(path), media_type=xlsx_type, headers=xlsx_disp)
