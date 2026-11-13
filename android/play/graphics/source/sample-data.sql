-- Synthetic demo data. Run once in an empty disposable capture installation.
CREATE TABLE sales AS SELECT * FROM (VALUES
  ('North', 'Analytics', 128, 38400),
  ('South', 'Analytics',  96, 28800),
  ('East',  'Analytics', 112, 33600),
  ('West',  'Analytics', 145, 43500),
  ('North', 'Storage',    84, 12600),
  ('South', 'Storage',    72, 10800),
  ('East',  'Storage',    96, 14400),
  ('West',  'Storage',   120, 18000)
) AS t(region, product, orders, revenue);

-- Phone / compact-tablet example.
SELECT region,
  sum(revenue) AS revenue
FROM sales
GROUP BY region
ORDER BY revenue DESC;

-- Large-tablet example.
SELECT region, sum(orders) AS orders,
  sum(revenue) AS revenue,
  round(sum(revenue) / sum(orders), 2) AS avg_order
FROM sales
GROUP BY region
ORDER BY revenue DESC;
