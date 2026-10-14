CREATE TABLE market_trade_snapshot (
 id INTEGER PRIMARY KEY,
 exchange TEXT NOT NULL CHECK(exchange IN ('sh','sz','bj')),
 trade_date TEXT NOT NULL CHECK(length(trade_date)=10),
 version INTEGER NOT NULL CHECK(version>0),
 is_latest INTEGER NOT NULL CHECK(is_latest IN (0,1)),
 definition_version TEXT NOT NULL,
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 source_id INTEGER NOT NULL,
 content_hash TEXT NOT NULL,
 obtained_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 security_code TEXT,
 secucode TEXT,
 security_name_abbr TEXT,
 org_code TEXT,
 trade_market TEXT,
 board_code TEXT,
 board_name TEXT,
 orig_board_code TEXT,
 source_trade_date TEXT,
 total_shares INTEGER,
 free_shares_a INTEGER,
 total_market_cap REAL,
 notlimited_marketcap_a REAL,
 close_price REAL,
 change_rate REAL,
 pe_ttm REAL,
 pe_lar REAL,
 pb_mrq REAL,
 pcf_ocf_lar REAL,
 pcf_ocf_ttm REAL,
 ps_ttm REAL,
 peg_car REAL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 UNIQUE(security_code,exchange,trade_date,definition_version,version)
);
CREATE UNIQUE INDEX market_trade_snapshot_latest ON market_trade_snapshot(security_code,exchange,trade_date,definition_version) WHERE is_latest=1;
CREATE INDEX market_trade_snapshot_date ON market_trade_snapshot(trade_date,is_latest,exchange,security_code);
CREATE VIEW market_cap_latest AS
 SELECT security_code AS stock_code,trade_date,total_market_cap AS total_cap,'eastmoney:market_trade_snapshot' AS source
 FROM market_trade_snapshot WHERE is_latest=1 AND definition_version='em-valueanalysis-v1' AND exchange IN ('sh','sz') AND total_market_cap IS NOT NULL
 UNION ALL
 SELECT c.stock_code,c.trade_date,c.total_cap,'legacy:sw_cap_facts' AS source
 FROM sw_cap_facts c
 WHERE c.import_id=(SELECT max(c2.import_id) FROM sw_cap_facts c2 WHERE c2.stock_code=c.stock_code AND c2.trade_date=c.trade_date
 AND c2.import_id NOT IN (SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.cap_status')='rejected'))
 AND NOT EXISTS (SELECT 1 FROM market_trade_snapshot n WHERE n.security_code=c.stock_code AND n.trade_date=c.trade_date AND n.exchange IN ('sh','sz') AND n.is_latest=1 AND n.definition_version='em-valueanalysis-v1' AND n.total_market_cap IS NOT NULL);
