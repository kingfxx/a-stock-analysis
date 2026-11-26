"""Keep a superseded business payload linked to its original synchronization run."""

from stock_analysis.infrastructure.storage import utc_now


def retain_previous(conn, record_key, previous):
    conn.execute('INSERT INTO stock_fact_revisions(run_id,record_key,raw_json,obtained_at,archived_at) '
                 'VALUES (?,?,?,?,?) ON CONFLICT(run_id,record_key) DO NOTHING',
                 (previous['run_id'], record_key, previous['raw_json'], previous['obtained_at'], utc_now()))
