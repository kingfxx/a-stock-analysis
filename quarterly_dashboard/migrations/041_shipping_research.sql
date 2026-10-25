CREATE TABLE research_shipping_rate_points (
 id INTEGER PRIMARY KEY, index_name TEXT NOT NULL, frequency TEXT NOT NULL,
 route TEXT NOT NULL, statistic TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 value REAL NOT NULL CHECK(value>0), unit TEXT NOT NULL, change_value REAL, change_kind TEXT,
 published_on TEXT, quality TEXT NOT NULL, channel TEXT NOT NULL,
 revision INTEGER NOT NULL, supersedes_id INTEGER REFERENCES research_shipping_rate_points(id),
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id), source_id INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 UNIQUE(index_name,frequency,route,statistic,period_start,period_end,channel,revision)
);
CREATE INDEX research_shipping_rate_lookup ON research_shipping_rate_points(index_name,frequency,route,period_start,channel);
CREATE TABLE research_shipping_report_metrics (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES company_report_documents(id),
 parse_id INTEGER NOT NULL REFERENCES company_report_parses(id), metric TEXT NOT NULL,
 scope TEXT NOT NULL, basis TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
 currency TEXT NOT NULL, evidence_page INTEGER NOT NULL, extractor_version TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(document_id,parse_id,metric,scope,basis,extractor_version)
);
CREATE TABLE research_shipping_industry_metrics (
 id INTEGER PRIMARY KEY, metric TEXT NOT NULL, scope TEXT NOT NULL, period TEXT NOT NULL,
 report_month TEXT NOT NULL, basis TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
 yoy REAL, published_on TEXT NOT NULL, revision INTEGER NOT NULL,
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id), source_id INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 UNIQUE(metric,scope,period,basis,revision)
);
CREATE TABLE research_shipping_forecast_versions (
 id INTEGER PRIMARY KEY, target TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
 model_version TEXT NOT NULL, input_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TRIGGER research_shipping_forecast_immutable BEFORE UPDATE ON research_shipping_forecast_versions
BEGIN SELECT RAISE(ABORT,'预测快照不可改写，请保存新版本'); END;
