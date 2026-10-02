-- Вариант А: справочник аномальных периодов + флаг в витрине v_sales.
-- Данные в sales_daily не меняются; флаг считается по дате на лету.

CREATE TABLE IF NOT EXISTS mpstats.anomaly_periods (
    id        serial   PRIMARY KEY,
    platform  smallint NOT NULL,          -- 1 = Wildberries, 2 = Ozon
    d1        date     NOT NULL,
    d2        date     NOT NULL,
    comment   text     NOT NULL
);

INSERT INTO mpstats.anomaly_periods (platform, d1, d2, comment)
SELECT * FROM (VALUES
    (2::smallint, DATE '2022-05-23', DATE '2022-12-31',
     'Сбой данных MPSTATS: выручка и число продававших SKU в 5–20 раз выше соседних периодов'),
    (2::smallint, DATE '2023-10-29', DATE '2023-11-10',
     'Сбой данных MPSTATS: выручка в 20–40 раз выше нормы')
) v(platform, d1, d2, comment)
WHERE NOT EXISTS (SELECT 1 FROM mpstats.anomaly_periods);

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
       p.name                                 AS "Наименование",
       (a.id IS NOT NULL)                     AS "Аномалия",
       a.comment                              AS "Комментарий аномалии"
FROM mpstats.sales_daily s
JOIN mpstats.categories c USING (category_id)
LEFT JOIN mpstats.products p ON p.platform = s.platform AND p.sku = s.sku
LEFT JOIN mpstats.anomaly_periods a ON a.platform = s.platform AND s.dt BETWEEN a.d1 AND a.d2;
