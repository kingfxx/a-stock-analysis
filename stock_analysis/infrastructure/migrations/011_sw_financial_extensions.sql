-- Nullable cumulative CNY amounts; keep immutable imports and per-field provenance.
ALTER TABLE sw_financial_facts ADD COLUMN operating_revenue REAL;
ALTER TABLE sw_financial_facts ADD COLUMN operating_cost REAL;
ALTER TABLE sw_financial_facts ADD COLUMN deduct_parent_profit REAL;
ALTER TABLE sw_financial_facts ADD COLUMN operating_profit REAL;
