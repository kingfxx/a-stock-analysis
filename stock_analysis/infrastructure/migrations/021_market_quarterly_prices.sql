-- Whole-market sparse quarter-end prices must not create followed instruments.
CREATE TABLE market_price_batches (
 id INTEGER PRIMARY KEY,
 content_hash TEXT NOT NULL UNIQUE,
 member_import_id INTEGER NOT NULL REFERENCES sw_imports(id),
 start_period TEXT NOT NULL, end_period TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL CHECK(status IN ('running','complete')),
 result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE market_price_sources (
 id INTEGER PRIMARY KEY,
 batch_id INTEGER NOT NULL REFERENCES market_price_batches(id),
 source TEXT NOT NULL, dataset TEXT NOT NULL,
 params_json TEXT NOT NULL, file_path TEXT NOT NULL,
 content_hash TEXT NOT NULL, obtained_at TEXT NOT NULL,
 UNIQUE(batch_id,file_path)
);
CREATE TABLE market_quarterly_prices (
 security_code TEXT NOT NULL CHECK(length(security_code)=6 AND security_code NOT GLOB '*[^0-9]*'),
 exchange TEXT NOT NULL CHECK(exchange IN ('sh','sz')),
 quarter_end TEXT NOT NULL CHECK(length(quarter_end)=10),
 trade_date TEXT NOT NULL CHECK(length(trade_date)=10 AND trade_date<=quarter_end),
 close REAL NOT NULL CHECK(close>0),
 adjustment TEXT NOT NULL DEFAULT 'raw' CHECK(adjustment='raw'),
 source_id INTEGER NOT NULL REFERENCES market_price_sources(id),
 batch_id INTEGER NOT NULL REFERENCES market_price_batches(id),
 obtained_at TEXT NOT NULL,
 PRIMARY KEY(security_code,exchange,quarter_end)
);
CREATE INDEX market_quarterly_prices_period ON market_quarterly_prices(quarter_end,exchange,security_code);
CREATE TRIGGER market_quarterly_price_source_insert BEFORE INSERT ON market_quarterly_prices
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_price_sources s
   WHERE s.id=NEW.source_id AND s.batch_id=NEW.batch_id AND s.dataset='cn_stock_real_bar1d')
 THEN RAISE(ABORT,'quarterly price source batch mismatch') END;
END;
