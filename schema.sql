-- Схема базы выгрузки MPSTATS (WB + Ozon в одной базе)
-- Компактное хранение: ~150 млн строк должны помещаться в ~15 ГБ
CREATE SCHEMA IF NOT EXISTS mpstats;

-- Справочник категорий: WB — предмет, Ozon — категория (ниша)
-- platform: 1 = Wildberries, 2 = Ozon
CREATE TABLE IF NOT EXISTS mpstats.categories (
    category_id  serial   PRIMARY KEY,
    platform     smallint NOT NULL,
    name         text     NOT NULL,
    segment      text     NOT NULL,          -- 'Бытовая техника' | 'Инструменты'
    subject_id   integer,                    -- только WB
    UNIQUE (platform, name)
);

-- Дневные продажи: одна строка = SKU x день с ненулевой выручкой
-- Порядок колонок подобран под выравнивание, чтобы строка была короче
CREATE TABLE IF NOT EXISTS mpstats.sales_daily (
    sku          bigint   NOT NULL,
    dt           date     NOT NULL,
    category_id  integer  NOT NULL,
    revenue      integer  NOT NULL,          -- руб.; WB с 2025-09-01: revenue_estimated
    orders       integer,                    -- шт.; WB с 2025-09-01: sales_estimated
    price        integer,                    -- руб.
    platform     smallint NOT NULL,
    PRIMARY KEY (platform, sku, dt)
);

-- Строки с аномальными значениями (сбои данных MPSTATS), не влезающими в integer
CREATE TABLE IF NOT EXISTS mpstats.sales_daily_overflow (
    platform     smallint NOT NULL,
    sku          bigint   NOT NULL,
    dt           date     NOT NULL,
    category_id  integer  NOT NULL,
    revenue      bigint   NOT NULL,
    orders       bigint,
    price        bigint,
    PRIMARY KEY (platform, sku, dt)
);

-- Карточки: бренд, продавец, название (последнее известное значение)
CREATE TABLE IF NOT EXISTS mpstats.products (
    platform    smallint NOT NULL,
    sku         bigint   NOT NULL,
    name        text,
    brand       text,
    seller      text,
    seller_id   bigint,
    category_id integer,
    first_dt    date,
    last_dt     date,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, sku)
);

-- Очередь заданий: позволяет продолжить выгрузку после сбоя
CREATE TABLE IF NOT EXISTS mpstats.load_jobs (
    job_id       serial PRIMARY KEY,
    platform     text NOT NULL,
    segment      text NOT NULL,
    path         text NOT NULL,              -- WB: subject_id, Ozon: путь категории
    path_name    text NOT NULL,
    d1           date NOT NULL,
    d2           date NOT NULL,
    metric       text NOT NULL,              -- 'revenue' | 'revenue_estimated'
    status       text NOT NULL DEFAULT 'new',-- new | running | done | error
    next_row     integer NOT NULL DEFAULT 0, -- с какой строки продолжать
    total        integer,
    pages        integer NOT NULL DEFAULT 0,
    items        integer NOT NULL DEFAULT 0,
    daily_rows   bigint  NOT NULL DEFAULT 0,
    error        text,
    started_at   timestamptz,
    finished_at  timestamptz,
    UNIQUE (platform, path, d1, d2)
);

-- Журнал запросов к API и остатка квоты
CREATE TABLE IF NOT EXISTS mpstats.api_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    job_id      integer,
    url         text,
    start_row   integer,
    http_status integer,
    items       integer,
    total       integer,
    seconds     numeric(8,2),
    quota_note  text
);

-- Итоговая витрина в нужном формате
CREATE OR REPLACE VIEW mpstats.v_sales AS
SELECT s.dt                                   AS "Дата",
       s.sku                                  AS "SKU",
       c.name                                 AS "Категория",
       s.revenue                              AS "Выручка",
       s.orders                               AS "Заказы",
       s.price                                AS "Цена",
       p.brand                                AS "Бренд",
       p.seller                               AS "Организация",
       CASE s.platform WHEN 1 THEN 'Wildberries' ELSE 'Ozon' END AS "Площадка",
       c.segment                              AS "Сегмент",
       (s.platform = 1 AND s.dt >= DATE '2025-09-01') AS "Estimated",
       p.name                                 AS "Наименование"
FROM mpstats.sales_daily s
JOIN mpstats.categories c USING (category_id)
LEFT JOIN mpstats.products p ON p.platform = s.platform AND p.sku = s.sku;

-- Справочник аномальных периодов и флаг «Аномалия» в витрине — см. anomaly_periods.sql
-- (применять после этого файла; он пересоздаёт v_sales с колонками аномалий)
