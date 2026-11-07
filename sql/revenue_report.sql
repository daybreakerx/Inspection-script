DROP VIEW IF EXISTS merchant_report;
DROP VIEW IF EXISTS order_reconciliation;
DROP VIEW IF EXISTS valid_attributions;
DROP TABLE IF EXISTS report_periods;
DROP TABLE IF EXISTS fx_rates;
DROP TABLE IF EXISTS attributions;
DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS merchants;

CREATE TABLE merchants (
  merchant_id TEXT PRIMARY KEY,
  timezone TEXT NOT NULL
);

CREATE TABLE orders (
  order_id TEXT NOT NULL,
  merchant_id TEXT NOT NULL,
  original_amount TEXT NOT NULL,
  amount REAL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  is_test INTEGER,
  created_utc TEXT
);

CREATE TABLE posts (
  post_id TEXT PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  source TEXT NOT NULL
);

CREATE TABLE attributions (
  order_id TEXT NOT NULL,
  post_id TEXT NOT NULL
);

CREATE TABLE fx_rates (
  currency TEXT PRIMARY KEY,
  currency_units_per_usd REAL NOT NULL
);

CREATE TABLE report_periods (
  merchant_id TEXT NOT NULL,
  month TEXT NOT NULL,
  timezone TEXT NOT NULL,
  start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL,
  reported_usd TEXT NOT NULL,
  PRIMARY KEY (merchant_id, month)
);

CREATE VIEW valid_attributions AS
SELECT DISTINCT a.order_id
FROM attributions a
JOIN orders o ON o.order_id = a.order_id
JOIN posts p ON p.post_id = a.post_id
WHERE p.source = 'Reelo'
  AND p.merchant_id = o.merchant_id;

CREATE VIEW order_reconciliation AS
SELECT
  rp.merchant_id,
  rp.month AS report_month,
  rp.timezone,
  rp.start_utc,
  rp.end_utc,
  rp.reported_usd,
  o.order_id,
  o.original_amount,
  o.amount,
  o.currency,
  o.status,
  o.is_test,
  o.created_utc,
  COALESCE(fx.currency_units_per_usd, NULL) AS currency_units_per_usd,
  CASE
    WHEN o.created_utc IS NOT NULL
     AND o.created_utc >= rp.start_utc
     AND o.created_utc < rp.end_utc
    THEN 1 ELSE 0
  END AS in_month,
  CASE WHEN o.status = 'paid' THEN 1 ELSE 0 END AS is_paid,
  CASE WHEN o.is_test = 0 THEN 1 ELSE 0 END AS is_not_test,
  CASE WHEN va.order_id IS NOT NULL THEN 1 ELSE 0 END AS has_attribution,
  CASE
    WHEN o.order_id IS NOT NULL
     AND o.amount IS NOT NULL
     AND o.is_test = 0
     AND o.status = 'paid'
     AND o.created_utc >= rp.start_utc
     AND o.created_utc < rp.end_utc
     AND va.order_id IS NOT NULL
     AND fx.currency_units_per_usd IS NOT NULL
    THEN ROUND(o.amount / fx.currency_units_per_usd, 6)
    ELSE 0
  END AS included_usd,
  CASE
    WHEN o.order_id IS NOT NULL
     AND o.amount IS NOT NULL
     AND o.is_test = 0
     AND o.status = 'paid'
     AND o.created_utc >= rp.start_utc
     AND o.created_utc < rp.end_utc
     AND va.order_id IS NOT NULL
     AND fx.currency_units_per_usd IS NOT NULL
    THEN 1 ELSE 0
  END AS included
FROM report_periods rp
LEFT JOIN orders o ON o.merchant_id = rp.merchant_id
LEFT JOIN fx_rates fx ON fx.currency = UPPER(o.currency)
LEFT JOIN valid_attributions va ON va.order_id = o.order_id;

CREATE VIEW merchant_report AS
SELECT
  merchant_id,
  report_month AS month,
  timezone,
  reported_usd,
  ROUND(COALESCE(SUM(included_usd), 0), 2) AS calculated_usd
FROM order_reconciliation
GROUP BY merchant_id, report_month, timezone, reported_usd;
