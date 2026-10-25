CREATE TABLE collection_rate_limits (
    task_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    settings_json TEXT NOT NULL CHECK(json_valid(settings_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_key, provider)
);
