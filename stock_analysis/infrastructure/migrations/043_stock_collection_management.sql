-- Control metadata only; existing stock facts and HTTP evidence are unchanged.
CREATE TABLE stock_collection_operations (
 id TEXT PRIMARY KEY, mode TEXT NOT NULL, trigger_type TEXT NOT NULL,
 scope_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','complete','failed','cancelled')),
 cancel_requested INTEGER NOT NULL DEFAULT 0,
 started_at TEXT NOT NULL, finished_at TEXT, error TEXT
);
CREATE TABLE stock_collection_items (
 id INTEGER PRIMARY KEY,
 operation_id TEXT NOT NULL REFERENCES stock_collection_operations(id),
 code TEXT NOT NULL, dataset TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','complete','failed','cancelled','reused')),
 started_at TEXT, finished_at TEXT, error TEXT,
 UNIQUE(operation_id,code,dataset)
);
CREATE UNIQUE INDEX stock_collection_active_item ON stock_collection_items(code,dataset)
 WHERE status IN ('pending','running');
CREATE UNIQUE INDEX stock_collection_initialization ON stock_collection_operations(json_extract(scope_json,'$.initial_code'))
 WHERE trigger_type='initial';
CREATE INDEX stock_collection_recent ON stock_collection_operations(started_at DESC);
CREATE INDEX stock_collection_item_success ON stock_collection_items(code,dataset,finished_at);
CREATE INDEX stock_collection_batch_parent ON collection_batches(json_extract(scope_json,'$.parent_operation'));
CREATE INDEX stock_collection_batch_item ON collection_batches(json_extract(scope_json,'$.parent_item'));
