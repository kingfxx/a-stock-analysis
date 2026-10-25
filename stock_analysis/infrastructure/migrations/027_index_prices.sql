-- Index points share identities/provenance, never stock price tables or valuation facts.
CREATE TABLE index_price_definitions (
 provider TEXT NOT NULL, series_key TEXT NOT NULL, definition_version TEXT NOT NULL,
 definition_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(provider,series_key,definition_version)
);
CREATE TABLE index_price_series (
 id INTEGER PRIMARY KEY,
 index_code TEXT NOT NULL REFERENCES index_identities(index_code),
 provider TEXT NOT NULL, series_key TEXT NOT NULL,
 frequency TEXT NOT NULL CHECK(frequency IN ('daily','monthly')),
 return_type TEXT NOT NULL CHECK(return_type IN ('price','total_return','net_return','unspecified')),
 definition_version TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(provider,series_key,definition_version)
 REFERENCES index_price_definitions(provider,series_key,definition_version),
 UNIQUE(index_code,provider,series_key,frequency,return_type,definition_version)
);
CREATE TRIGGER index_price_series_immutable BEFORE UPDATE ON index_price_series
BEGIN
 SELECT RAISE(ABORT,'insert a new index price series identity');
END;
CREATE TABLE index_price_observations (
 id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL REFERENCES index_price_series(id),
 observation_date TEXT NOT NULL,
 open REAL CHECK(open IS NULL OR open>0), high REAL CHECK(high IS NULL OR high>0),
 low REAL CHECK(low IS NULL OR low>0), close REAL CHECK(close IS NULL OR close>0),
 withdrawal_reason TEXT, content_hash TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), is_latest INTEGER NOT NULL CHECK(is_latest IN (0,1)),
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id), source_id INTEGER NOT NULL,
 obtained_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 UNIQUE(series_id,observation_date,version),
 CHECK(high IS NULL OR low IS NULL OR high>=low),
 CHECK(high IS NULL OR close IS NULL OR high>=close),
 CHECK(low IS NULL OR close IS NULL OR low<=close),
 CHECK(high IS NULL OR open IS NULL OR high>=open),
 CHECK(low IS NULL OR open IS NULL OR low<=open)
);
CREATE UNIQUE INDEX index_price_latest
ON index_price_observations(series_id,observation_date) WHERE is_latest=1;
