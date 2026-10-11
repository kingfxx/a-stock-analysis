-- Transactional cache generation: normal stock/page writes do not touch it.
CREATE TABLE index_read_revisions (
 scope TEXT PRIMARY KEY CHECK(scope='indices'),
 revision INTEGER NOT NULL CHECK(revision>=0),
 updated_at TEXT NOT NULL
);
INSERT INTO index_read_revisions VALUES('indices',0,strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TRIGGER index_read_index_valuation_observations_insert AFTER INSERT ON index_valuation_observations
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_valuation_observations_update AFTER UPDATE ON index_valuation_observations
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_valuation_observations_delete AFTER DELETE ON index_valuation_observations
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_valuation_series_insert AFTER INSERT ON index_valuation_series
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_valuation_series_update AFTER UPDATE ON index_valuation_series
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_valuation_series_delete AFTER DELETE ON index_valuation_series
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_catalog_insert AFTER INSERT ON index_catalog
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_catalog_update AFTER UPDATE ON index_catalog
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_catalog_delete AFTER DELETE ON index_catalog
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_metric_definitions_insert AFTER INSERT ON index_metric_definitions
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_metric_definitions_update AFTER UPDATE ON index_metric_definitions
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_metric_definitions_delete AFTER DELETE ON index_metric_definitions
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_sw_industries_insert AFTER INSERT ON sw_industries
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_sw_industries_update AFTER UPDATE ON sw_industries
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_sw_industries_delete AFTER DELETE ON sw_industries
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;
