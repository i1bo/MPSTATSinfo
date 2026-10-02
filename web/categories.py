"""
Единые названия и сегменты категорий для сайта (в базах категории хранятся как прежде, с полными путями
и исходным сегментом). Справочник для проверки — категории_соответствие.xlsx (python web/categories.py).

WB:   «Крупная бытовая техника / Стиральные машины»                              -> «Стиральные машины»
Ozon: «Бытовая техника/Крупная бытовая техника/Стиральные машины/Полноразмерные» -> «Стиральные машины»

Базовое правило для Ozon: берём последний уровень пути, но поднимаемся к родителю, если последний уровень
лишь уточняет его (начинается с названия родителя, «Полноразмерные», «С фронтальной загрузкой»…)
или если родитель — это предмет WB. Дальше, чтобы категорий было меньше:

  A. Сегмент «Прочее»: ветки Ozon (первый уровень пути), дающие < 1% выручки своего сегмента
     (одежда, аптека, книги…). На сайте сегмент есть, но по умолчанию выключен в фильтре.
  Внутри каждого сегмента:
  B. Одно название, записанное по-разному: регистр, ё, порядок слов, окончания
     («Пилы сабельные» = «Сабельные пилы», «Септик» = «Септики»).
  C. Название — элемент перечисления в другом названии: «Кессоны» -> «Кессоны и оголовки для скважин».
  D. WB «X для Y» -> категория Ozon, у которой лист X, а среди родителей есть Y
     («Чаши для мультиварок» -> …/Аксессуары для мультиварок/Чаши).
  E. Категория с выручкой за всё время < SMALL_REVENUE поднимается на уровень выше (к родителю пути;
     у Ozon — не выше второго уровня).
"""
import collections
import re

OTHER_SEGMENT = "Прочее"
CORE_SHARE = 0.01            # A: ветка Ozon с меньшей долей выручки сегмента уходит в «Прочее»
SMALL_REVENUE = 1_000_000    # E: порог выручки за всё время, ₽

ADJ_ENDINGS = ("ые", "ие", "ый", "ий", "ая", "яя", "ое", "ее")
REFINE_PREFIX = re.compile(r"^(с|со|без|для|на|под)\s", re.IGNORECASE)


def is_refinement(leaf: str, parent: str) -> bool:
    """Последний уровень — уточнение родителя, а не самостоятельная категория."""
    lo = leaf.lower()
    if lo.startswith(parent.lower()):
        return True                                   # «Стиральные машины автомат»
    if " " not in leaf and lo.endswith(ADJ_ENDINGS):
        return True                                   # «Полноразмерные»
    return bool(REFINE_PREFIX.match(leaf))            # «С вертикальной загрузкой»


def path_parts(platform: int, name: str) -> list[str]:
    sep = " / " if platform == 1 else "/"
    return [p.strip() for p in name.split(sep) if p.strip()]


def display_name(platform: int, name: str, wb_subjects: set[str]) -> str:
    if platform == 1:
        return name.split(" / ", 1)[-1].strip()
    parts = path_parts(platform, name)
    if len(parts) < 3:
        return parts[-1] if parts else name
    leaf, parent = parts[-1], parts[-2]
    if leaf.lower() in wb_subjects:
        return leaf
    # родитель — не корень и не второй уровень (они слишком широкие)
    if len(parts) >= 4 and (parent.lower() in wb_subjects or is_refinement(leaf, parent)):
        return parent
    return leaf


# ---------------------------------------------------------------- сравнение названий
STOP = {"и", "с", "со", "в", "на", "без", "из", "по", "под", "от", "к", "а", "или"}
ENDINGS = sorted(["ики", "ки", "ы", "и", "а", "я", "ов", "ев", "ей", "ь", "ые", "ие", "ый", "ий", "ая", "яя", "ое",
                  "ее", "ой", "ую", "ами", "ями", "ах", "ях", "ом", "ем", "ам", "ям", "у", "ю", "о", "е"],
                 key=len, reverse=True)
# слишком общие названия («Аксессуары», «Запчасти») не вливаем в перечисления — это не одна и та же вещь
GENERIC = {"аксессуар", "запчаст", "комплектующ", "насадк", "насад", "расходн", "материал", "проч", "прочее",
           "принадлежност", "оборудован", "оборудовани", "товар", "набор", "инструмент", "оснастк", "оснаст", "детал"}


def stem(w: str) -> str:
    w = w.replace("ё", "е")
    for e in ENDINGS:
        if len(w) - len(e) >= 4 and w.endswith(e):
            return w[:-len(e)]
    return w


def tokens(s: str) -> frozenset:
    return frozenset(stem(w) for w in re.findall(r"[a-zа-я0-9]+", s.lower().replace("-", " ")) if w not in STOP)


def split_dlya(s: str):
    head, _, tail = s.lower().partition(" для ")
    return head, tail


def key(s: str):
    """(главная часть, часть после «для») — без регистра, порядка слов и окончаний."""
    head, tail = split_dlya(s)
    return tokens(head), tokens(tail)


def members(s: str):
    """Ключи элементов перечисления: «Кессоны и оголовки для скважин» -> кессоны, оголовки (с «для …» и без)."""
    head, tail = split_dlya(s)
    ms = [m for m in re.split(r",|\sи\s", head) if m.strip()]
    if len(ms) < 2:
        return []
    out = []
    for m in ms:
        out.append((tokens(m), frozenset()))
        if tail:
            out.append((tokens(m), tokens(tail)))
    return out


class Groups:
    """Объединение названий: merge(a, b) — a вливается в b и получает название b."""

    def __init__(self, names):
        self.par = {n: n for n in names}
        self.rule = {}

    def find(self, n):
        while self.par[n] != n:
            self.par[n] = self.par[self.par[n]]
            n = self.par[n]
        return n

    def merge(self, a, b, rule):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.par[ra] = rb
            self.rule.setdefault(a, rule)


def merge_names(names_rev: dict, platforms: dict, ozon_paths: list) -> dict:
    """Правила B, C, D внутри одного сегмента.
    names_rev: {название: выручка}, platforms: {название: {1, 2}}, ozon_paths: [(части пути, название)].
    Возвращает {название: (итоговое название, правило)}."""
    names = sorted(names_rev, key=lambda n: -names_rev[n])     # «денежные» первыми — они и дают название
    K = {n: key(n) for n in names}
    g = Groups(names)
    # B: одинаковый ключ
    first = {}
    for n in names:
        if K[n] in first:
            g.merge(n, first[K[n]], "B")
        else:
            first[K[n]] = n
    # C: название — элемент перечисления другого
    by_member = collections.defaultdict(list)
    for n in names:
        for m in members(n):
            by_member[m].append(n)
    for n in names:
        if members(n) or K[n][0] <= GENERIC:
            continue
        cand = [t for t in by_member.get(K[n], []) if g.find(t) != g.find(n)]
        # «Увлажнители» не вливаем в «Сауны и увлажнители для лица»: если у цели есть «для …», а у нас нет,
        # мы должны быть не крупнее цели (иначе это другая, более общая категория)
        cand = [t for t in cand if not (K[t][1] and not K[n][1] and names_rev[n] > names_rev[t])]
        if cand:
            cand.sort(key=lambda t: (len(members(t)), -names_rev[t]))
            g.merge(n, cand[0], "C")
    # D: WB «X для Y» -> Ozon …/Y…/X
    leaf_idx = collections.defaultdict(list)
    for parts, n in ozon_paths:
        if len(parts) >= 3:
            leaf_idx[K[n][0]].append((frozenset().union(*(tokens(p) for p in parts[:-1])), n))
    for n in names:
        head, tail = K[n]
        if platforms[n] != {1} or not tail or head <= GENERIC:
            continue
        cand = [o for anc, o in leaf_idx.get(head, []) if tail <= anc and g.find(o) != g.find(n)]
        if cand:
            g.merge(n, max(cand, key=lambda o: names_rev[o]), "D")
    return {n: (g.find(n), g.rule.get(n, "")) for n in names}


def canon(items):
    """items: [(название, выручка)] -> {название: самое «денежное» написание с тем же ключом}."""
    rev = collections.Counter()
    for n, r in items:
        rev[n] += r
    best = {}
    for n in sorted(rev, key=lambda n: -rev[n]):
        best.setdefault(key(n), n)
    return {n: best[key(n)] for n in rev}


def build(rows) -> dict:
    """rows: [(category_id, platform, path, segment, revenue)]
    -> {category_id: (название на сайте, сегмент на сайте, правило '', 'B', 'C', 'D', 'E', 'C+E'…)}."""
    rows = [(cid, p, name, seg, int(rev or 0)) for cid, p, name, seg, rev in rows]
    wb = {path_parts(1, n)[-1].lower() for _, p, n, _, _ in rows if p == 1}
    parts = {cid: path_parts(p, n) for cid, p, n, _, _ in rows}
    base = {cid: display_name(p, n, wb) for cid, p, n, _, _ in rows}

    # A: сегмент на сайте
    seg_rev, root_rev = collections.Counter(), collections.Counter()
    for cid, p, _, seg, rev in rows:
        if p == 2:
            seg_rev[seg] += rev
            root_rev[seg, parts[cid][0]] += rev
    site_seg = {cid: OTHER_SEGMENT if p == 2 and root_rev[seg, parts[cid][0]] < CORE_SHARE * max(seg_rev[seg], 1)
                else seg for cid, p, _, seg, _ in rows}

    # B, C, D внутри сегмента
    name, rule = {}, {}
    by_seg = collections.defaultdict(list)
    for r in rows:
        by_seg[site_seg[r[0]]].append(r)
    for seg, rs in by_seg.items():
        names_rev, plats, oz = collections.Counter(), collections.defaultdict(set), []
        for cid, p, _, _, rev in rs:
            names_rev[base[cid]] += rev
            plats[base[cid]].add(p)
            if p == 2:
                oz.append((parts[cid], base[cid]))
        merged = merge_names(names_rev, plats, oz)
        for cid, *_ in rs:
            name[cid], r = merged[base[cid]]
            if r:
                rule[cid] = r

    # E: мелкие -> на уровень выше
    grev = collections.Counter()
    for cid, _, _, _, rev in rows:
        grev[site_seg[cid], name[cid]] += rev
    for cid, p, _, _, _ in rows:
        pp = parts[cid]
        low = [i for i, x in enumerate(pp) if x.lower() == base[cid].lower()]
        up = (low[-1] if low else len(pp) - 1) - 1          # индекс родителя в пути
        if grev[site_seg[cid], name[cid]] < SMALL_REVENUE and up >= (1 if p == 2 else 0):
            name[cid] = pp[up]
            rule[cid] = rule.get(cid, "") + ("+E" if rule.get(cid) else "E")
    # родитель мог совпасть с уже существующим названием — приклеиваем к нему
    for seg, rs in by_seg.items():
        c = canon([(name[r[0]], r[4]) for r in rs])
        for r in rs:
            name[r[0]] = c[name[r[0]]]
    return {cid: (name[cid], site_seg[cid], rule.get(cid, "")) for cid, *_ in rows}


# ---------------------------------------------------------------- справочник для проверки
RULE_NAMES = {"": "", "B": "B: то же название (порядок слов, окончания)", "C": "C: часть перечисления",
              "D": "D: WB → категория Ozon", "E": "E: мелкая → родитель"}


def write_xlsx(con, path):
    """Справочник «исходный путь -> категория и сегмент на сайте» из готового файла DuckDB."""
    import xlsxwriter
    rows = con.execute("""
        SELECT c.category_id, c.platform, c.name, g.name, coalesce(c.revenue, 0)
        FROM categories c JOIN segments g USING (segment_id)""").fetchall()
    res = build(rows)
    wbs = {path_parts(1, x[2])[-1].lower() for x in rows if x[1] == 1}
    base = {cid: display_name(p, n, wbs) for cid, p, n, _, _ in rows}
    npaths = collections.Counter((res[r[0]][1], res[r[0]][0]) for r in rows)
    data = sorted(rows, key=lambda r: (res[r[0]][1] == OTHER_SEGMENT, res[r[0]][1], res[r[0]][0], -r[4]))
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet("Категории")
    head = wb.add_format({"bold": True, "bg_color": "#EEEEEE"})
    num = wb.add_format({"num_format": "#,##0"})
    cols = [("Категория на сайте", 45), ("Сегмент на сайте", 18), ("Категория в базе (исходный путь)", 90),
            ("Маркетплейс", 13), ("Сегмент (исходный)", 18), ("Выручка за всё время, ₽", 20),
            ("Путей в этой категории на сайте", 12), ("Было на сайте", 45), ("Правило", 42)]
    for i, (t, w) in enumerate(cols):
        ws.write(0, i, t, head)
        ws.set_column(i, i, w)
    for j, (cid, p, n, seg, rev) in enumerate(data, 1):
        new, sseg, rule = res[cid]
        rn = " + ".join(RULE_NAMES[x] for x in rule.split("+")) if rule else ""
        ws.write_row(j, 0, [new, sseg, n, "Wildberries" if p == 1 else "Ozon", seg])
        ws.write_number(j, 5, rev, num)
        ws.write_row(j, 6, [npaths[sseg, new], base[cid], rn])
    ws.autofilter(0, 0, len(data), len(cols) - 1)
    ws.freeze_panes(1, 0)
    # сводка
    sm = wb.add_worksheet("Сводка")
    sm.set_column(0, 0, 32)
    sm.set_column(1, 3, 18)
    sm.write_row(0, 0, ["Сегмент на сайте", "Было названий", "Стало", "Выручка, ₽"], head)
    segs = sorted({v[1] for v in res.values()}, key=lambda s: (s == OTHER_SEGMENT, s))
    for i, s in enumerate(segs, 1):
        ids_ = [r[0] for r in rows if res[r[0]][1] == s]
        sm.write_row(i, 0, [s, len({base[c].lower() for c in ids_}), len({res[c][0] for c in ids_})])
        sm.write_number(i, 3, sum(r[4] for r in rows if res[r[0]][1] == s), num)
    work = [r[0] for r in rows if res[r[0]][1] != OTHER_SEGMENT]
    sm.write_row(len(segs) + 1, 0, [f"Итого без «{OTHER_SEGMENT}» (уникальных)", len({base[c].lower() for c in work}),
                                    len({res[c][0] for c in work})], head)
    sm.write(len(segs) + 3, 0, f"Порог E: выручка за всё время < {SMALL_REVENUE:,} ₽".replace(",", " "))
    sm.write(len(segs) + 4, 0, f"«{OTHER_SEGMENT}»: ветки Ozon с долей < {CORE_SHARE:.0%} выручки сегмента")
    wb.close()


if __name__ == "__main__":
    import os
    import sys
    from pathlib import Path

    import duckdb
    BASE = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(BASE))
    import loader
    loader.load_env()
    db = Path(os.environ.get("DUCKDB_PATH", BASE / "data" / "mpstats.duckdb"))
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "категории_соответствие.xlsx"
    with duckdb.connect(str(db), read_only=True) as c:
        write_xlsx(c, out)
    print(f"Справочник записан: {out}")
