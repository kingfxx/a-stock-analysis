CREATE TABLE market_indicator_observations (
 id INTEGER PRIMARY KEY,
 indicator_key TEXT NOT NULL, scope_key TEXT NOT NULL, provider TEXT NOT NULL,
 channel TEXT NOT NULL, frequency TEXT NOT NULL, definition_version TEXT NOT NULL,
 observation_date TEXT NOT NULL, value REAL, close REAL, top5_amount_yi REAL, total_amount_yi REAL,
 value_status TEXT NOT NULL CHECK(value_status IN ('observed','source_missing')),
 precision_digits INTEGER, version INTEGER NOT NULL CHECK(version>0),
 is_latest INTEGER NOT NULL CHECK(is_latest IN (0,1)), content_hash TEXT NOT NULL,
 source_id INTEGER NOT NULL, batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 obtained_at TEXT NOT NULL, updated_at TEXT NOT NULL, withdrawal_reason TEXT,
 FOREIGN KEY(source_id,batch_id) REFERENCES collection_sources(id,batch_id),
 CHECK((value_status='observed' AND value IS NOT NULL) OR (value_status='source_missing' AND value IS NULL)),
 UNIQUE(indicator_key,scope_key,provider,channel,frequency,definition_version,observation_date,version)
);
CREATE UNIQUE INDEX market_indicator_latest ON market_indicator_observations
 (indicator_key,scope_key,provider,channel,frequency,definition_version,observation_date) WHERE is_latest=1;
CREATE TABLE collection_schedules (
 task_key TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 timezone TEXT NOT NULL, local_time TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX congestion_scheduled_day ON index_collection_operations(json_extract(scope_json,'$.scheduled_date'))
 WHERE json_extract(scope_json,'$.dataset')='market_congestion' AND json_extract(scope_json,'$.scheduled_date') IS NOT NULL;
