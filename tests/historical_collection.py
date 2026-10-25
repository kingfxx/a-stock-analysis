"""Construct collection batches in pre-scheduler migration fixtures (v23-v39)."""
from stock_analysis.collection.collection import encoded
from stock_analysis.infrastructure.storage import utc_now


def begin_batch(db, operation_id, dataset, provider, start=None, end=None, *, scope=None, **kwargs):
    with db.connection(write=True) as conn:
        return conn.execute(
            'INSERT INTO collection_batches(operation_id,domain,dataset,source_system,scope_start,scope_end,'
            'scope_json,trigger_type,status,started_at,parser_version,definition_version) '
            "VALUES(?,'market',?,?,?,?,?,'manual','running',?,'index-v2','v1')",
            (operation_id,dataset,provider,start,end,encoded(scope or {}),utc_now())).lastrowid
