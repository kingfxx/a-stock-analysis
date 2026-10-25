"""Existing domain data upgrade; SQL migration history remains unchanged."""
from stock_analysis.infrastructure.storage import StorageError

def migrate_public_sources(conn):
    """Mechanically rebuild financial tables and preserve every historical column/value."""
    tables = ("market_financial_income", "market_financial_balance",
              "market_financial_cashflow", "market_financial_performance")
    view = conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='market_financial_industry'").fetchone()
    if view:
        conn.execute("DROP VIEW market_financial_industry")
    for table in tables:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not row or not row[0]:
            raise StorageError(f"Missing financial table {table}")
        original = row[0]
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
        legacy = original.replace("batch_id INTEGER NOT NULL REFERENCES market_financial_batches(id)",
                                  "batch_id INTEGER REFERENCES market_financial_batches(id)")
        legacy = legacy.replace("source_id INTEGER NOT NULL REFERENCES market_financial_sources(id)",
                                "source_id INTEGER REFERENCES market_financial_sources(id)")
        if legacy == original:
            raise StorageError(f"Unexpected legacy references in {table}")
        create = legacy.replace(f"CREATE TABLE {table} (", f"CREATE TABLE {table}_v32 (", 1).rstrip().removesuffix(";")
        columns = ("collection_batch_id INTEGER REFERENCES collection_batches(id),"
            " collection_source_id INTEGER,")
        marker = " UNIQUE(security_code,exchange,report_date,version)"
        if marker not in create:
            raise StorageError(f"Cannot locate uniqueness constraint for {table}")
        create = create.replace(marker, " "+columns+marker.strip(), 1)
        constraint = (", CHECK((batch_id IS NOT NULL AND source_id IS NOT NULL AND collection_batch_id IS NULL AND collection_source_id IS NULL)"
            " OR (batch_id IS NULL AND source_id IS NULL AND collection_batch_id IS NOT NULL AND collection_source_id IS NOT NULL)),"
            " FOREIGN KEY(collection_source_id,collection_batch_id) REFERENCES collection_sources(id,batch_id)")
        if not create.endswith(")"):
            raise StorageError(f"Cannot parse table definition for {table}")
        conn.execute(create[:-1] + constraint + ")")
        names = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        quoted = ",".join('"'+name.replace('"','""')+'"' for name in names)
        conn.execute(f"INSERT INTO {table}_v32({quoted},collection_batch_id,collection_source_id) "
                     f"SELECT {quoted},NULL,NULL FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_v32 RENAME TO {table}")
        for statement in indexes:
            conn.execute(statement)
    conn.execute("ALTER TABLE sw_memberships ADD COLUMN collection_batch_id INTEGER REFERENCES collection_batches(id)")
    conn.execute("ALTER TABLE sw_memberships ADD COLUMN collection_source_id INTEGER REFERENCES collection_sources(id)")
    for verb in ("INSERT", "UPDATE OF collection_batch_id,collection_source_id"):
        conn.execute(f"""CREATE TRIGGER sw_memberships_public_source_{'insert' if verb == 'INSERT' else 'update'}
            BEFORE {verb} ON sw_memberships
            WHEN (NEW.collection_batch_id IS NULL) != (NEW.collection_source_id IS NULL)
              OR (NEW.collection_source_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id
                  JOIN sw_imports i ON i.id=NEW.import_id WHERE s.id=NEW.collection_source_id
                  AND s.batch_id=NEW.collection_batch_id AND b.operation_id=json_extract(i.source_manifest_json,'$.collection_operation_id')
                  AND ((s.source_system='sse' AND s.dataset='market.listings.sse')
                    OR (s.source_system='szse' AND s.dataset='market.listings.szse'))))
            BEGIN SELECT RAISE(ABORT,'invalid public membership source pair'); END""")
    if view:
        sql = view[0].replace("i.source_id AS income_source_id, p.source_id AS performance_source_id,",
            "i.source_id AS income_source_id, p.source_id AS performance_source_id, "
            "i.collection_source_id AS income_collection_source_id, i.collection_batch_id AS income_collection_batch_id, "
            "p.collection_source_id AS performance_collection_source_id, p.collection_batch_id AS performance_collection_batch_id,")
        conn.execute(sql)

