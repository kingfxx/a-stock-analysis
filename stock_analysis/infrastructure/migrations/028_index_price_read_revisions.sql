-- Point series is part of index chart overlays; publish its generation transactionally.
CREATE TRIGGER index_read_index_price_definitions_insert AFTER INSERT ON index_price_definitions
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_definitions_update AFTER UPDATE ON index_price_definitions
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_definitions_delete AFTER DELETE ON index_price_definitions
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_series_insert AFTER INSERT ON index_price_series
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_series_update AFTER UPDATE ON index_price_series
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_series_delete AFTER DELETE ON index_price_series
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_observations_insert AFTER INSERT ON index_price_observations
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_observations_update AFTER UPDATE ON index_price_observations
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;

CREATE TRIGGER index_read_index_price_observations_delete AFTER DELETE ON index_price_observations
BEGIN
 UPDATE index_read_revisions SET revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE scope='indices';
END;
