-- Replace domain control tables; payload migration is verified in schema_upgrade.
CREATE TABLE collection_operations (
 id INTEGER PRIMARY KEY,
 operation_id TEXT UNIQUE,
 task_key TEXT NOT NULL DEFAULT 'indices',
 mode TEXT NOT NULL DEFAULT 'incremental', trigger_type TEXT NOT NULL DEFAULT 'manual',
 scope_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL CHECK(status IN ('running','complete','failed','cancelled')),
 cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 started_at TEXT NOT NULL, finished_at TEXT, worker_started_at TEXT,
 result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
 action TEXT, target TEXT, recheck INTEGER CHECK(recheck IN (0,1)),
 CHECK(operation_id IS NOT NULL OR action IS NOT NULL)
);
-- Numeric industry run IDs remain stable API/business identities in the same row.
CREATE TRIGGER collection_industry_identity AFTER INSERT ON collection_operations
WHEN NEW.action IS NOT NULL AND NEW.operation_id IS NULL
BEGIN
 UPDATE collection_operations SET operation_id='sw_update_runs:'||NEW.id,
 task_key=CASE NEW.action WHEN 'financial_period' THEN 'market_financial'
 WHEN 'cap_quarter' THEN 'trade_snapshot' ELSE 'industry:'||NEW.action END,
 mode=CASE WHEN NEW.recheck=1 THEN 'review' ELSE 'incremental' END
 WHERE id=NEW.id;
END;
CREATE INDEX collection_operation_recent ON collection_operations(task_key,started_at DESC);
CREATE UNIQUE INDEX collection_initial_stock ON collection_operations(json_extract(scope_json,'$.initial_code'))
 WHERE task_key='stocks' AND trigger_type='initial';

CREATE TABLE collection_batches_v44 (
 id INTEGER PRIMARY KEY,
 operation_id TEXT REFERENCES collection_operations(operation_id), legacy_operation_ref TEXT,
 sync_run_id INTEGER REFERENCES sync_runs(id), idempotency_key TEXT,
 domain TEXT NOT NULL, dataset TEXT NOT NULL, source_system TEXT NOT NULL,
 scope_start TEXT, scope_end TEXT, scope_json TEXT NOT NULL DEFAULT '{}',
 frequency TEXT, universe_version TEXT, trigger_type TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','complete','failed','cancelled','reused','skipped')),
 coverage_status TEXT NOT NULL DEFAULT 'unknown' CHECK(coverage_status IN ('complete','partial','empty','unknown')),
 created_at TEXT, started_at TEXT, finished_at TEXT, parser_version TEXT, definition_version TEXT,
 result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
 code TEXT, work_dataset TEXT, work_key TEXT, sequence INTEGER NOT NULL DEFAULT 0,
 mode TEXT, reused_batch_id INTEGER REFERENCES collection_batches(id),
 CHECK(scope_start IS NULL OR scope_end IS NULL OR scope_start<=scope_end)
);

ALTER TABLE collection_schedules RENAME COLUMN task_key TO schedule_id;
ALTER TABLE collection_schedules ADD COLUMN task_key TEXT;
ALTER TABLE collection_schedules ADD COLUMN rule_json TEXT;
ALTER TABLE collection_schedules ADD COLUMN params_json TEXT;
ALTER TABLE collection_schedules ADD COLUMN rule_updated_at TEXT;
ALTER TABLE collection_schedules ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1));
ALTER TABLE collection_schedule_slots RENAME COLUMN task_key TO schedule_id;
