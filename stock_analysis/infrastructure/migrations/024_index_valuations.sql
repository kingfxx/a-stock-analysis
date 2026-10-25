CREATE TABLE index_identities (
 index_code TEXT PRIMARY KEY, created_at TEXT NOT NULL
);
CREATE TABLE index_catalog (
 id INTEGER PRIMARY KEY, index_code TEXT NOT NULL REFERENCES index_identities(index_code),
 name TEXT NOT NULL, index_type TEXT NOT NULL CHECK(index_type IN ('broad','industry','strategy','theme')),
 classification_system TEXT, industry_level INTEGER, parent_index_code TEXT REFERENCES index_identities(index_code),
 valid_from TEXT, valid_to TEXT, validity_status TEXT NOT NULL CHECK(validity_status IN ('confirmed','unknown')),
 mapping_status TEXT NOT NULL DEFAULT 'confirmed' CHECK(mapping_status IN ('confirmed','pending')),
 source_id INTEGER REFERENCES collection_sources(id), updated_at TEXT NOT NULL,
 CHECK(valid_from IS NULL OR valid_to IS NULL OR valid_from<=valid_to),
 CHECK(parent_index_code IS NULL OR parent_index_code<>index_code),
 CHECK((index_type='industry' AND classification_system IS NOT NULL AND (industry_level IS NULL OR industry_level>=1))
    OR (index_type<>'industry' AND classification_system IS NULL AND industry_level IS NULL AND parent_index_code IS NULL)),
 CHECK(industry_level IS NULL OR industry_level<>1 OR parent_index_code IS NULL),
 CHECK(validity_status<>'confirmed' OR valid_from IS NOT NULL)
);
CREATE INDEX index_catalog_filter ON index_catalog(index_type,classification_system,industry_level,parent_index_code);
CREATE UNIQUE INDEX index_catalog_unknown ON index_catalog(index_code) WHERE validity_status='unknown';
CREATE TABLE index_provider_codes (
 id INTEGER PRIMARY KEY, index_code TEXT NOT NULL REFERENCES index_identities(index_code),
 provider TEXT NOT NULL, provider_code TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
 source_id INTEGER REFERENCES collection_sources(id), updated_at TEXT NOT NULL,
 CHECK(valid_from IS NULL OR valid_to IS NULL OR valid_from<=valid_to)
);
CREATE INDEX index_provider_lookup ON index_provider_codes(provider,provider_code,valid_from,valid_to);
CREATE TRIGGER index_mapping_overlap BEFORE INSERT ON index_provider_codes
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM index_provider_codes p WHERE p.provider=NEW.provider AND p.provider_code=NEW.provider_code
 AND coalesce(p.valid_from,'0001-01-01')<=coalesce(NEW.valid_to,'9999-12-31')
 AND coalesce(NEW.valid_from,'0001-01-01')<=coalesce(p.valid_to,'9999-12-31'))
 THEN RAISE(ABORT,'overlapping index provider mapping') END;
END;
CREATE TRIGGER index_catalog_overlap BEFORE INSERT ON index_catalog
WHEN NEW.validity_status='confirmed'
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM index_catalog c WHERE c.index_code=NEW.index_code AND c.validity_status='confirmed'
 AND c.valid_from<=coalesce(NEW.valid_to,'9999-12-31') AND NEW.valid_from<=coalesce(c.valid_to,'9999-12-31'))
 THEN RAISE(ABORT,'overlapping index catalog versions') END;
END;
CREATE TRIGGER index_catalog_parent BEFORE INSERT ON index_catalog
WHEN NEW.parent_index_code IS NOT NULL
BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM index_catalog p WHERE p.index_code=NEW.parent_index_code
 AND p.classification_system=NEW.classification_system AND p.industry_level=NEW.industry_level-1
 AND (NEW.validity_status='unknown' OR (p.validity_status='confirmed' AND p.valid_from<=NEW.valid_from
 AND coalesce(p.valid_to,'9999-12-31')>=coalesce(NEW.valid_to,'9999-12-31'))))
 THEN RAISE(ABORT,'invalid index parent classification or validity') END;
END;
-- Catalog/mappings are immutable history rows; replace pending unknown rows only through repository validation.
CREATE TRIGGER index_catalog_immutable BEFORE UPDATE OF index_code,index_type,classification_system,industry_level,parent_index_code,valid_from,valid_to,validity_status ON index_catalog
BEGIN
 SELECT RAISE(ABORT,'insert a new catalog history version');
END;
CREATE TRIGGER index_mapping_immutable BEFORE UPDATE ON index_provider_codes
BEGIN
 SELECT RAISE(ABORT,'insert a new provider mapping version');
END;
CREATE TABLE index_metric_definitions (
 provider TEXT NOT NULL, series_key TEXT NOT NULL, definition_version TEXT NOT NULL,
 definition_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(provider,series_key,definition_version)
);
CREATE TABLE index_valuation_observations (
 id INTEGER PRIMARY KEY, index_code TEXT NOT NULL REFERENCES index_identities(index_code),
 provider TEXT NOT NULL, series_key TEXT NOT NULL, observation_date TEXT NOT NULL,
 frequency TEXT NOT NULL CHECK(frequency IN ('daily','monthly')),
 pe_source REAL, pe_dynamic REAL, pe_static REAL, pe_ttm REAL, pe_rolling REAL,
 pb REAL, ps REAL, ps_ttm REAL, dividend_yield REAL, withdrawal_reason TEXT,
 definition_version TEXT NOT NULL, content_hash TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), is_latest INTEGER NOT NULL CHECK(is_latest IN (0,1)),
 batch_id INTEGER NOT NULL REFERENCES collection_batches(id), source_id INTEGER NOT NULL,
 obtained_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 FOREIGN KEY(provider,series_key,definition_version) REFERENCES index_metric_definitions(provider,series_key,definition_version),
 UNIQUE(index_code,provider,series_key,observation_date,frequency,definition_version,version)
);
CREATE UNIQUE INDEX index_valuation_latest ON index_valuation_observations(index_code,provider,series_key,observation_date,frequency,definition_version) WHERE is_latest=1;
CREATE INDEX index_valuation_dates ON index_valuation_observations(index_code,observation_date,provider);
CREATE TABLE index_collection_operations (
 operation_id TEXT PRIMARY KEY, mode TEXT NOT NULL, scope_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','complete','failed','cancelled')),
 cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 started_at TEXT NOT NULL, finished_at TEXT, result_json TEXT NOT NULL DEFAULT '{}', error TEXT
);
