"""Read persisted work that must finish before desktop shutdown."""


def has_background_work(db):
    with db.connection() as conn:
        for table, states in (
            ('sync_runs', "'running'"),
            ('collection_operations', "'running'"),
            ('ai_analysis_runs', "'queued','running','validating'"),
        ):
            if conn.execute(f'SELECT 1 FROM {table} WHERE status IN ({states}) LIMIT 1').fetchone():
                return True
    return False
