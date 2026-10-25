CREATE TABLE index_valuation_series (
 id INTEGER PRIMARY KEY,
 index_code TEXT NOT NULL REFERENCES index_identities(index_code),
 provider TEXT NOT NULL, series_key TEXT NOT NULL,
 frequency TEXT NOT NULL CHECK(frequency IN ('daily','monthly')),
 definition_version TEXT NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(provider,series_key,definition_version)
 REFERENCES index_metric_definitions(provider,series_key,definition_version),
 UNIQUE(index_code,provider,series_key,frequency,definition_version)
);
INSERT INTO index_valuation_series(index_code,provider,series_key,frequency,definition_version,created_at)
SELECT DISTINCT index_code,provider,series_key,frequency,definition_version,
 strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM index_valuation_observations
ORDER BY index_code,provider,series_key,frequency,definition_version;

CREATE TABLE index_valuation_observations_new (
 id INTEGER PRIMARY KEY,
 series_id INTEGER NOT NULL REFERENCES index_valuation_series(id),
 observation_date TEXT NOT NULL,
 pe_source REAL, pe_dynamic REAL, pe_static REAL, pe_ttm REAL, pe_rolling REAL,
 pb REAL, ps REAL, ps_ttm REAL, dividend_yield REAL, withdrawal_reason TEXT,
 content_hash TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), is_latest INTEGER NOT NULL CHECK(is_latest IN (0,1)),
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id), source_id INTEGER NOT NULL,
 obtained_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 UNIQUE(series_id,observation_date,version)
);
INSERT INTO index_valuation_observations_new
SELECT f.id,s.id,f.observation_date,f.pe_source,f.pe_dynamic,f.pe_static,f.pe_ttm,f.pe_rolling,
 f.pb,f.ps,f.ps_ttm,f.dividend_yield,f.withdrawal_reason,f.content_hash,
 f.version,f.is_latest,f.batch_id,f.source_id,f.obtained_at,f.updated_at
FROM index_valuation_observations f JOIN index_valuation_series s
ON s.index_code=f.index_code AND s.provider=f.provider AND s.series_key=f.series_key
AND s.frequency=f.frequency AND s.definition_version=f.definition_version
ORDER BY f.id;

-- Abort the surrounding migration transaction if any original value or identity changed.
CREATE TEMP TABLE index_series_migration_check(ok INTEGER NOT NULL CHECK(ok=1));
INSERT INTO index_series_migration_check
SELECT CASE WHEN
 (SELECT count(*) FROM index_valuation_observations)=(SELECT count(*) FROM index_valuation_observations_new)
 AND NOT EXISTS(
 SELECT * FROM index_valuation_observations
 EXCEPT
 SELECT f.id,s.index_code,s.provider,s.series_key,f.observation_date,s.frequency,
 f.pe_source,f.pe_dynamic,f.pe_static,f.pe_ttm,f.pe_rolling,f.pb,f.ps,f.ps_ttm,f.dividend_yield,
 f.withdrawal_reason,s.definition_version,f.content_hash,f.version,f.is_latest,
 f.batch_id,f.source_id,f.obtained_at,f.updated_at
 FROM index_valuation_observations_new f JOIN index_valuation_series s ON s.id=f.series_id
 ) THEN 1 ELSE 0 END;
DROP TABLE index_series_migration_check;
DROP TABLE index_valuation_observations;
ALTER TABLE index_valuation_observations_new RENAME TO index_valuation_observations;
CREATE UNIQUE INDEX index_valuation_latest
ON index_valuation_observations(series_id,observation_date) WHERE is_latest=1;
CREATE TRIGGER index_valuation_series_immutable BEFORE UPDATE ON index_valuation_series
BEGIN
 SELECT RAISE(ABORT,'insert a new index series identity');
END;
