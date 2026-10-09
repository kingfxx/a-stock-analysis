-- Logical projection only: calculation and membership rules stay in Python.
CREATE VIEW market_financial_industry AS
SELECT i.security_code, i.exchange, i.report_date,
       i.total_operate_income AS revenue, i.parent_netprofit AS parent_profit,
       i.operate_income AS operating_revenue, i.operate_cost AS operating_cost,
       p.weightavg_roe AS weighted_roe,
       i.source_id AS income_source_id, p.source_id AS performance_source_id,
       max(i.updated_at,coalesce(p.updated_at,i.updated_at)) AS updated_at
FROM market_financial_income i
LEFT JOIN market_financial_performance p
  ON p.security_code=i.security_code AND p.exchange=i.exchange
 AND p.report_date=i.report_date AND p.is_latest=1
WHERE i.is_latest=1;
