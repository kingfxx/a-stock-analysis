-- Preserve the five-column legacy daily schedule contract, including congestion.
CREATE TABLE collection_schedule_rules (
 task_key TEXT PRIMARY KEY REFERENCES collection_schedules(task_key),
 rule_json TEXT NOT NULL, params_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE collection_schedule_slots (
 task_key TEXT NOT NULL REFERENCES collection_schedules(task_key), slot_key TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','complete','failed','pending')),
 operation_ref TEXT, attempts INTEGER NOT NULL DEFAULT 1,
 started_at TEXT NOT NULL, finished_at TEXT, updated_at TEXT NOT NULL, error TEXT,
 PRIMARY KEY(task_key,slot_key)
);
CREATE INDEX collection_slot_status ON collection_schedule_slots(status,task_key,slot_key);
CREATE INDEX sw_run_action ON sw_update_runs(action,id DESC);
CREATE INDEX index_operation_dataset ON index_collection_operations(json_extract(scope_json,'$.dataset'),started_at DESC);
ALTER TABLE sw_update_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1));
CREATE INDEX collection_slot_operation ON collection_schedule_slots(operation_ref);
