CREATE TABLE research_aluminum_forecast_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    forecast_start_year INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    parent_version_id INTEGER
);
CREATE TRIGGER research_aluminum_forecast_immutable
BEFORE UPDATE ON research_aluminum_forecast_versions
BEGIN
    SELECT RAISE(ABORT, '预测快照不可改写，请保存新版本');
END;
