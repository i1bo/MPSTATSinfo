"use strict";
// Общее для главной страницы и страницы артикула: форматирование, запросы, общий график.

const $ = id => document.getElementById(id);

// ---------------------------------------------------------------- тема: как в системе, либо выбранная вручную
// Выбор кнопкой запоминается в браузере; без выбора работает настройка Windows (prefers-color-scheme).
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function savedTheme() { try { return localStorage.getItem("mpstats.theme"); } catch (e) { return null; } }
function isDark() { const t = savedTheme(); return t ? t === "dark" : darkQuery.matches; }
function applyTheme() {
  const t = savedTheme();
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const btn = document.getElementById("themeBtn");
  if (btn) {
    const dark = isDark();
    // в тёмной теме показываем солнце (переключит на светлую), в светлой — луну
    btn.innerHTML = dark
      ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>'
      : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11z"/></svg>';
    btn.title = btn.ariaLabel = dark ? "Светлая тема" : "Тёмная тема";
  }
  document.dispatchEvent(new Event("themechange"));
}
function toggleTheme() {
  try { localStorage.setItem("mpstats.theme", isDark() ? "light" : "dark"); } catch (e) {}
  applyTheme();
}
applyTheme();  // сразу, до отрисовки страницы — чтобы не мигала
darkQuery.addEventListener("change", applyTheme);
document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("themeBtn");
  if (btn) { btn.onclick = toggleTheme; applyTheme(); }
});
const fmtInt = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 0});
const fmt = v => v == null ? "" : fmtInt.format(v);
function fmtBig(v, unit = "") {
  if (v == null) return "—";
  const a = Math.abs(v);
  const f = (x, s) => new Intl.NumberFormat("ru-RU", {maximumFractionDigits: Math.abs(x) >= 100 ? 0 : 1}).format(x) + " " + s;
  const r = a >= 1e9 ? f(v / 1e9, "млрд") : a >= 1e6 ? f(v / 1e6, "млн") : a >= 1e4 ? f(v / 1e3, "тыс") : fmt(v);
  return unit ? r + " " + unit : r;
}
const fmtDate = s => s ? s.slice(8, 10) + "." + s.slice(5, 7) + "." + s.slice(0, 4) : "";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const parseNum = s => { const v = parseFloat(String(s).replace(/\s/g, "").replace(",", ".")); return isNaN(v) ? null : v; };
function shiftDays(iso, n) { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

async function api(path, body, signal) {
  const r = await fetch(path, body === undefined ? {signal} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal});
  if (r.status === 401) {  // вход истёк — на страницу входа, после неё вернёмся сюда же (с фильтрами)
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search) + location.hash;
    throw new Error("Нужно войти");
  }
  if (!r.ok) {
    let t = await r.text();
    try { t = JSON.parse(t).detail; t = typeof t === "string" ? t : JSON.stringify(t); } catch (e) {}
    throw new Error(t || r.statusText);
  }
  return r;
}
function status(text, isErr) {
  const el = $("status");
  el.textContent = text || "";
  el.className = "status" + (text ? " show" : "") + (isErr ? " error" : "");
}

// ссылки на карточку товара на маркетплейсе и на страницу анализа артикула
const PLATFORM_NAMES = {1: "Wildberries", 2: "Ozon"};
function mpUrl(platform, sku) {
  return +platform === 1 ? `https://www.wildberries.ru/catalog/${sku}/detail.aspx` : `https://www.ozon.ru/context/detail/id/${sku}/`;
}
function skuUrl(platform, sku, d1, d2) {
  return `/sku/${platform}/${sku}` + (d1 ? `#d1=${d1}&d2=${d2}` : "");
}

const GRAIN_NAMES = {day: "по дням", week: "по неделям", month: "по месяцам"};
function periodLabel(iso, grain) {
  if (grain === "month") return iso.slice(5, 7) + "." + iso.slice(0, 4);
  if (grain === "week") return "нед. с " + fmtDate(iso);
  return fmtDate(iso);
}

// ---------------------------------------------------------------- общий график
// Один график с переключателями показателей; у каждого показателя своя шкала:
// первая слева, остальные справа. Шкала окрашена в цвет своей линии.
const METRICS = [
  {key: "revenue", label: "Выручка", unit: "₽", color: "--series-1"},
  {key: "orders", label: "Заказы", unit: "шт", color: "--series-2"},
  {key: "avg_check", label: "Средний чек", unit: "₽", color: "--series-3"},
];

class MetricsChart {
  constructor(el, togglesEl, storageKey) {
    this.el = el;
    this.tg = togglesEl;
    this.key = storageKey;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(storageKey)); } catch (e) {}
    const valid = Array.isArray(saved) ? saved.filter(k => METRICS.some(m => m.key === k)) : [];
    this.on = new Set(valid.length ? valid : METRICS.map(m => m.key));
    this.chart = echarts.init(el, null, {renderer: "svg"});
    this.points = [];
    this.grain = "day";
    this.renderToggles();
    window.addEventListener("resize", () => this.chart.resize());
  }

  renderToggles() {
    this.tg.innerHTML = METRICS.map(m => {
      const on = this.on.has(m.key);
      return `<button type="button" class="toggle ${on ? "" : "off"}" data-k="${m.key}" style="--c: var(${m.color})" aria-pressed="${on}" title="${on ? "Скрыть" : "Показать"}"><i></i>${m.label}</button>`;
    }).join("");
    this.tg.querySelectorAll("button").forEach(b => b.onclick = () => {
      const k = b.dataset.k;
      if (this.on.has(k)) { if (this.on.size === 1) return; this.on.delete(k); } else this.on.add(k);
      try { localStorage.setItem(this.key, JSON.stringify([...this.on])); } catch (e) {}
      this.renderToggles();
      this.draw();
    });
  }

  set(points, grain) { this.points = points; this.grain = grain; this.draw(); }

  draw() {
    const vis = METRICS.filter(m => this.on.has(m.key));
    const pts = this.points, grain = this.grain;
    const AXIS_W = 84;                       // ширина одной правой шкалы вместе с подписью
    const right = Math.max(1, vis.length - 1);
    this.el.style.height = "340px";
    this.chart.resize();
    const muted = cssVar("--muted"), grid = cssVar("--grid"), axis = cssVar("--axis"), surf = cssVar("--surface"), text = cssVar("--text");
    const ts = pts.map(p => p.t);
    this.chart.setOption({
      animation: false,
      grid: {left: 84, right: 16 + (right - 1) * AXIS_W + 72, top: 16, bottom: 30},
      xAxis: {type: "category", data: ts, boundaryGap: false,
        axisLine: {lineStyle: {color: axis}}, axisTick: {show: false},
        axisLabel: {color: muted, hideOverlap: true,
          formatter: v => grain === "month" ? v.slice(5, 7) + "." + v.slice(2, 4) : v.slice(8, 10) + "." + v.slice(5, 7) + (ts.length > 400 ? "." + v.slice(2, 4) : "")}},
      yAxis: vis.map((m, i) => {
        const c = cssVar(m.color);
        return {type: "value", position: i === 0 ? "left" : "right", offset: i <= 1 ? 0 : (i - 1) * AXIS_W,
          name: `${m.label}, ${m.unit}`, nameLocation: "middle", nameGap: i === 0 ? 62 : 54, nameRotate: i === 0 ? 90 : -90,
          nameTextStyle: {color: c, fontWeight: 600},
          splitNumber: 4, alignTicks: true,
          axisLine: {show: true, lineStyle: {color: c, width: 1.5, opacity: 0.55}}, axisTick: {show: false},
          splitLine: {show: i === 0, lineStyle: {color: grid}},
          axisLabel: {color: muted, formatter: v => fmtBig(v)}};
      }),
      axisPointer: {lineStyle: {color: axis}},
      tooltip: {trigger: "axis", backgroundColor: surf, borderColor: axis, textStyle: {color: text},
        formatter: ps => {
          const p = pts[ps[0].dataIndex];
          if (!p) return "";
          return `<b>${periodLabel(p.t, grain)}</b><br>` + vis.map(m =>
            `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${cssVar(m.color)};margin-right:6px"></span>` +
            `${m.label}: <b>${p[m.key] == null ? "—" : fmt(p[m.key]) + " " + m.unit}</b>`).join("<br>");
        }},
      series: vis.map((m, i) => {
        const c = cssVar(m.color);
        return {name: m.label, type: "line", yAxisIndex: i, data: pts.map(p => p[m.key]),
          showSymbol: pts.length <= 40, symbol: "circle", symbolSize: 5, connectNulls: false,
          lineStyle: {width: 2, color: c, opacity: 0.8}, itemStyle: {color: c, opacity: 0.85, borderWidth: 0},
          areaStyle: vis.length === 1 ? {color: c, opacity: 0.08} : undefined};
      }),
      graphic: pts.length ? [] : [{type: "text", left: "center", top: "middle", style: {text: "нет данных", fill: muted, fontSize: 14}}],
    }, true);
  }
}
