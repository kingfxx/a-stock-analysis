CREATE TABLE legacy_imports_new (
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dataset TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    imported_at TEXT NOT NULL,
    PRIMARY KEY(path, dataset)
);
INSERT INTO legacy_imports_new SELECT path, content_hash, dataset, instrument_id, run_id, record_count, imported_at FROM legacy_imports;
DROP TABLE legacy_imports;
ALTER TABLE legacy_imports_new RENAME TO legacy_imports;

CREATE TABLE financial_reports (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    report_type TEXT NOT NULL,
    period TEXT NOT NULL CHECK(length(period) = 10),
    publish_date TEXT,
    update_time TEXT,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source, report_type, period)
);
CREATE INDEX financial_reports_periods ON financial_reports(instrument_id, source, report_type, period DESC);

CREATE TABLE dividend_events (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    event_key TEXT NOT NULL,
    source_event_id TEXT,
    report_period TEXT,
    proposal_date TEXT,
    notice_date TEXT,
    registration_date TEXT,
    ex_dividend_date TEXT,
    status TEXT NOT NULL,
    cash_per_ten REAL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    UNIQUE(instrument_id, source, event_key)
);
CREATE INDEX dividend_events_dates ON dividend_events(instrument_id, source, notice_date, ex_dividend_date);

CREATE TABLE valuation_observations (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    observed_on TEXT NOT NULL CHECK(length(observed_on) = 10),
    value REAL NOT NULL,
    source_windows_json TEXT NOT NULL DEFAULT '[]',
    sampling_version TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    obtained_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source, metric, observed_on)
);
CREATE INDEX valuation_observations_dates ON valuation_observations(instrument_id, source, metric, observed_on DESC);

CREATE TABLE industry_snapshots (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    industry_code TEXT,
    classification_basis TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    UNIQUE(instrument_id, source, snapshot_at)
);
CREATE INDEX industry_snapshots_latest ON industry_snapshots(instrument_id, source, snapshot_at DESC);

CREATE TABLE report_overrides (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    period TEXT NOT NULL CHECK(length(period) = 10),
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('manual', 'legacy')),
    source_path TEXT,
    updated_at TEXT NOT NULL,
    run_id INTEGER REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, period, field_name),
    CHECK((origin = 'manual' AND run_id IS NULL) OR (origin = 'legacy' AND run_id IS NOT NULL))
);

CREATE TABLE legacy_valuation_snapshots (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source_path TEXT NOT NULL,
    observed_month TEXT NOT NULL CHECK(length(observed_month) = 7),
    methodology_version TEXT NOT NULL,
    row_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    PRIMARY KEY(instrument_id, source_path, observed_month)
);

CREATE TRIGGER legacy_import_provenance_insert BEFORE INSERT ON legacy_imports
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.dataset=NEW.dataset)
        THEN RAISE(ABORT, 'legacy import provenance mismatch') END;
END;
CREATE TRIGGER financial_report_provenance_insert BEFORE INSERT ON financial_reports
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='financial:'||NEW.report_type AND r.adjustment='raw')
        THEN RAISE(ABORT, 'financial report provenance mismatch') END;
END;
CREATE TRIGGER financial_report_provenance_update BEFORE UPDATE ON financial_reports
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='financial:'||NEW.report_type AND r.adjustment='raw')
        THEN RAISE(ABORT, 'financial report provenance mismatch') END;
END;
CREATE TRIGGER dividend_provenance_insert BEFORE INSERT ON dividend_events
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='dividends' AND r.adjustment='raw')
        THEN RAISE(ABORT, 'dividend provenance mismatch') END;
END;
CREATE TRIGGER dividend_provenance_update BEFORE UPDATE ON dividend_events
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='dividends' AND r.adjustment='raw')
        THEN RAISE(ABORT, 'dividend provenance mismatch') END;
END;
CREATE TRIGGER valuation_provenance_insert BEFORE INSERT ON valuation_observations
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='valuation:'||NEW.metric AND r.adjustment='raw')
        THEN RAISE(ABORT, 'valuation provenance mismatch') END;
END;
CREATE TRIGGER valuation_provenance_update BEFORE UPDATE ON valuation_observations
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='valuation:'||NEW.metric AND r.adjustment='raw')
        THEN RAISE(ABORT, 'valuation provenance mismatch') END;
END;
CREATE TRIGGER industry_provenance_insert BEFORE INSERT ON industry_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.source=NEW.source
        AND r.dataset='industry' AND r.adjustment='raw')
        THEN RAISE(ABORT, 'industry provenance mismatch') END;
END;
CREATE TRIGGER override_provenance_insert BEFORE INSERT ON report_overrides
WHEN NEW.run_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.dataset='report_overrides')
        THEN RAISE(ABORT, 'override provenance mismatch') END;
END;
CREATE TRIGGER legacy_valuation_provenance_insert BEFORE INSERT ON legacy_valuation_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM sync_runs r WHERE r.id=NEW.run_id
        AND r.instrument_id=NEW.instrument_id AND r.dataset='valuation_legacy')
        THEN RAISE(ABORT, 'legacy valuation provenance mismatch') END;
END;
