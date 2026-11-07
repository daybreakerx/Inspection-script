DROP VIEW IF EXISTS order_reconciliation;
DROP VIEW IF EXISTS corrected_report;
DROP VIEW IF EXISTS original_report;
DROP TABLE IF EXISTS attributions;
DROP TABLE IF EXISTS orders;

CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY,
  merchant_id INTEGER NOT NULL,
  amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  is_test INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE attributions (
  order_id INTEGER NOT NULL,
  post_id TEXT NOT NULL
);

INSERT INTO orders VALUES
  (1001, 101, 300, 'USD', 'paid', 0, '2026-09-03 18:00'),
  (1002, 101, 450, 'USD', 'paid', 0, '2026-09-05 21:10'),
  (1003, 101, 200, 'USD', 'refunded', 0, '2026-09-08 16:45'),
  (1004, 101, 780, 'HKD', 'paid', 0, '2026-09-10 02:30'),
  (1005, 101, 500, 'USD', 'paid', 1, '2026-09-12 09:00'),
  (1006, 101, 350, 'USD', 'paid', 0, '2026-09-15 19:20'),
  (1007, 101, 250, 'USD', 'paid', 0, '2026-10-01 03:00'),
  (1008, 101, 400, 'USD', 'paid', 0, '2026-09-01 05:00'),
  (1009, 101, 600, 'USD', 'paid', 0, '2026-09-20 22:15'),
  (1010, 101, 150, 'USD', 'paid', 0, '2026-09-25 17:40');

INSERT INTO attributions VALUES
  (1001, 'P01'),
  (1002, 'P01'),
  (1002, 'P02'),
  (1003, 'P02'),
  (1004, 'P03'),
  (1005, 'P03'),
  (1006, 'P04'),
  (1007, 'P04'),
  (1008, 'P01'),
  (1010, 'P05');

CREATE VIEW original_report AS
SELECT SUM(o.amount) AS ai_revenue
FROM orders o
JOIN attributions a ON a.order_id = o.order_id
WHERE o.merchant_id = 101
  AND o.created_at >= '2026-09-01'
  AND o.created_at < '2026-10-01';

CREATE VIEW order_reconciliation AS
WITH original_by_order AS (
  SELECT o.order_id, SUM(o.amount) AS original_contribution
  FROM orders o
  JOIN attributions a ON a.order_id = o.order_id
  WHERE o.merchant_id = 101
    AND o.created_at >= '2026-09-01'
    AND o.created_at < '2026-10-01'
  GROUP BY o.order_id
),
valid_attributions AS (
  SELECT DISTINCT order_id
  FROM attributions
),
normalized_orders AS (
  SELECT
    o.*,
    CASE o.currency
      WHEN 'USD' THEN o.amount
      WHEN 'HKD' THEN o.amount / 7.8
    END AS amount_usd,
    CASE
      WHEN o.created_at >= '2026-09-01 07:00'
       AND o.created_at < '2026-10-01 07:00'
      THEN 1 ELSE 0
    END AS in_local_month,
    CASE WHEN v.order_id IS NOT NULL THEN 1 ELSE 0 END AS has_attribution
  FROM orders o
  LEFT JOIN valid_attributions v ON v.order_id = o.order_id
  WHERE o.merchant_id = 101
)
SELECT
  n.order_id,
  COALESCE(b.original_contribution, 0) AS original_contribution,
  CASE
    WHEN n.in_local_month = 1
     AND n.status = 'paid'
     AND n.is_test = 0
     AND n.has_attribution = 1
     AND n.currency IN ('USD', 'HKD')
    THEN n.amount_usd ELSE 0
  END AS correct_contribution,
  ROUND(
    COALESCE(b.original_contribution, 0) - CASE
      WHEN n.in_local_month = 1
       AND n.status = 'paid'
       AND n.is_test = 0
       AND n.has_attribution = 1
       AND n.currency IN ('USD', 'HKD')
      THEN n.amount_usd ELSE 0
    END,
    2
  ) AS difference
FROM normalized_orders n
LEFT JOIN original_by_order b ON b.order_id = n.order_id;

CREATE VIEW corrected_report AS
SELECT ROUND(SUM(correct_contribution), 2) AS ai_revenue
FROM order_reconciliation;

SELECT 'original_sql' AS query_name, ROUND(ai_revenue, 2) AS ai_revenue FROM original_report
UNION ALL
SELECT 'corrected_sql' AS query_name, ai_revenue FROM corrected_report;
