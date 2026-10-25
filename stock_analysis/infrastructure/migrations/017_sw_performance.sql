ALTER TABLE sw_financial_facts ADD COLUMN basic_eps REAL;
ALTER TABLE sw_financial_facts ADD COLUMN bps REAL;
ALTER TABLE sw_financial_facts ADD COLUMN weighted_roe REAL;
ALTER TABLE sw_financial_facts ADD COLUMN operating_cashflow_per_share REAL;
ALTER TABLE sw_financial_facts ADD COLUMN deduct_basic_eps REAL;
ALTER TABLE sw_financial_facts ADD COLUMN dividend_yield REAL;
ALTER TABLE sw_financial_facts ADD COLUMN reported_gross_margin REAL;
ALTER TABLE sw_financial_facts ADD COLUMN performance_notice_date TEXT;
