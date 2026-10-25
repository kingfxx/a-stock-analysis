CREATE TABLE ai_analysis_snapshots (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
    input_schema_version TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    input_json TEXT NOT NULL,
    source_manifest_json TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(instrument_id,input_schema_version,calculation_version,snapshot_hash),
    UNIQUE(id,instrument_id)
);
CREATE TRIGGER ai_snapshot_immutable BEFORE UPDATE ON ai_analysis_snapshots
BEGIN
    SELECT RAISE(ABORT, 'analysis snapshots are immutable');
END;
CREATE TABLE ai_analysis_runs (
    id TEXT PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
    snapshot_id INTEGER NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL DEFAULT 'openai',
    auth_mode TEXT NOT NULL DEFAULT 'chatgpt_plan',
    account_ref TEXT NOT NULL,
    model TEXT NOT NULL,
    resolved_model TEXT,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    prompt_json TEXT NOT NULL,
    output_schema_version TEXT NOT NULL,
    analysis_profile_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','validating','succeeded','failed','cancelled','interrupted')),
    verdict TEXT,
    summary TEXT,
    result_json TEXT,
    validation_json TEXT,
    response_id TEXT,
    usage_json TEXT,
    diagnostic_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY(snapshot_id,instrument_id) REFERENCES ai_analysis_snapshots(id,instrument_id),
    CHECK(status <> 'succeeded' OR (result_json IS NOT NULL AND verdict IS NOT NULL AND summary IS NOT NULL AND completed_at IS NOT NULL)),
    CHECK((status IN ('queued','running','validating') AND completed_at IS NULL) OR (status NOT IN ('queued','running','validating') AND completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX ai_active_stock ON ai_analysis_runs(instrument_id) WHERE status IN ('queued','running','validating');
CREATE UNIQUE INDEX ai_one_running ON ai_analysis_runs((1)) WHERE status IN ('running','validating');
CREATE INDEX ai_stock_history ON ai_analysis_runs(instrument_id,created_at DESC,id DESC);
CREATE TRIGGER ai_run_transition BEFORE UPDATE ON ai_analysis_runs
WHEN NOT ((OLD.status='queued' AND NEW.status IN ('running','cancelled','interrupted','failed'))
    OR (OLD.status='running' AND NEW.status IN ('validating','cancelled','interrupted','failed'))
    OR (OLD.status='validating' AND NEW.status IN ('succeeded','cancelled','interrupted','failed')))
BEGIN
    SELECT RAISE(ABORT, 'invalid analysis transition');
END;
CREATE TABLE ai_analysis_preferences (
    id INTEGER PRIMARY KEY CHECK(id=1),
    provider TEXT NOT NULL DEFAULT 'openai' CHECK(provider='openai'),
    model TEXT,
    analysis_profile_json TEXT NOT NULL DEFAULT '{"style":"value","horizon":"1_to_3_years"}',
    updated_at TEXT NOT NULL
);
