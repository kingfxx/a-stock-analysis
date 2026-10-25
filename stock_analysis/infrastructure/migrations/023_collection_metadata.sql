-- New collectors only. No legacy data or foreign keys are migrated.
CREATE TABLE collection_batches (
 id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, idempotency_key TEXT,
 domain TEXT NOT NULL, dataset TEXT NOT NULL, source_system TEXT NOT NULL,
 scope_start TEXT, scope_end TEXT, scope_json TEXT NOT NULL DEFAULT '{}',
 frequency TEXT, universe_version TEXT, trigger_type TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','complete','failed','cancelled')),
 coverage_status TEXT NOT NULL DEFAULT 'unknown' CHECK(coverage_status IN ('complete','partial','empty','unknown')),
 started_at TEXT NOT NULL, finished_at TEXT, parser_version TEXT, definition_version TEXT,
 result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
 CHECK(scope_start IS NULL OR scope_end IS NULL OR scope_start<=scope_end)
);
CREATE INDEX collection_batch_scope ON collection_batches(domain,dataset,source_system,scope_start,scope_end,status);
CREATE INDEX collection_batch_operation ON collection_batches(operation_id,id);
CREATE UNIQUE INDEX collection_batch_idempotency ON collection_batches(domain,dataset,source_system,idempotency_key)
 WHERE idempotency_key IS NOT NULL AND status='complete';
CREATE TABLE collection_sources (
 id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES collection_batches(id),
 source_system TEXT NOT NULL, dataset TEXT NOT NULL, request_key TEXT NOT NULL,
 attempt_no INTEGER NOT NULL CHECK(attempt_no>=1), request_json TEXT NOT NULL,
 request_status TEXT NOT NULL DEFAULT 'unknown', parse_status TEXT NOT NULL DEFAULT 'unknown',
 http_status INTEGER, error TEXT, file_path TEXT, content_type TEXT, compression TEXT,
 raw_sha256 TEXT, stored_sha256 TEXT, hash_basis TEXT,
 raw_bytes INTEGER, stored_bytes INTEGER, fields_json TEXT, obtained_at TEXT,
 reused_source_id INTEGER REFERENCES collection_sources(id),
 UNIQUE(batch_id,dataset,request_key,attempt_no), UNIQUE(id,batch_id)
);
CREATE INDEX collection_source_batch ON collection_sources(batch_id,id);
CREATE TABLE collection_provider_state (
 source_system TEXT PRIMARY KEY, cooldown_until TEXT, reason TEXT,
 denials INTEGER NOT NULL DEFAULT 0, budget_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
