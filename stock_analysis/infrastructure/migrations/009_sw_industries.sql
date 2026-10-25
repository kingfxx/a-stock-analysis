CREATE TABLE sw_imports (
 id INTEGER PRIMARY KEY, obtained_at TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE,
 source_manifest_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status='complete')
);
CREATE TABLE sw_industries (
 code TEXT PRIMARY KEY, name TEXT NOT NULL, level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
 parent_code TEXT REFERENCES sw_industries(code), standard TEXT NOT NULL DEFAULT 'SW2021'
);
CREATE TABLE sw_memberships (
 import_id INTEGER NOT NULL REFERENCES sw_imports(id), stock_code TEXT NOT NULL, name TEXT NOT NULL,
 industry_code TEXT REFERENCES sw_industries(code), effective_date TEXT, source_update TEXT,
 PRIMARY KEY(import_id,stock_code)
);
CREATE TABLE sw_membership_history (
 import_id INTEGER NOT NULL REFERENCES sw_imports(id), stock_code TEXT NOT NULL,
 effective_date TEXT NOT NULL, industry_code TEXT NOT NULL, source_update TEXT NOT NULL,
 PRIMARY KEY(import_id,stock_code,effective_date,industry_code)
);
CREATE TABLE sw_financial_facts (
 import_id INTEGER NOT NULL REFERENCES sw_imports(id), stock_code TEXT NOT NULL,
 period TEXT NOT NULL, revenue REAL, parent_profit REAL, notice_date TEXT,
 provenance_json TEXT NOT NULL, PRIMARY KEY(import_id,stock_code,period)
);
CREATE INDEX sw_financial_period ON sw_financial_facts(import_id,period);
CREATE TABLE sw_cap_facts (
 import_id INTEGER NOT NULL REFERENCES sw_imports(id), stock_code TEXT NOT NULL,
 trade_date TEXT NOT NULL, total_cap REAL NOT NULL CHECK(total_cap>0),
 provenance_json TEXT NOT NULL, PRIMARY KEY(import_id,stock_code,trade_date)
);
-- Only tertiary totals are persisted. Higher levels sum their children.
-- An import is an audit version; the visible history keeps one observation per quarter.
CREATE TABLE sw_industry_cap_quarters (
 import_id INTEGER NOT NULL REFERENCES sw_imports(id), industry_code TEXT NOT NULL REFERENCES sw_industries(code),
 quarter TEXT NOT NULL, trade_date TEXT NOT NULL, expected_count INTEGER NOT NULL,
 known_count INTEGER NOT NULL, known_cap REAL, total_cap REAL,
 PRIMARY KEY(import_id,industry_code,quarter), CHECK(known_count<=expected_count),
 CHECK(total_cap IS NULL OR known_count=expected_count)
);
