from historical_collection import begin_batch as historical_batch
"""v26 only adds transactional cache generations and registers maintenance metadata."""
import sqlite3

from stock_analysis.infrastructure import storage
from stock_analysis.domains.indices.index_repository import publish
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.infrastructure.storage import Database,instance_lock
from test_index_collection import source


def test_revision_migration_preserves_facts_and_tracks_transaction(tmp_path,monkeypatch):
    db=Database(tmp_path/'indices.sqlite3')
    migrations=storage.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(storage,'MIGRATIONS',tuple(m for m in migrations if m[0]<=25))
        db.initialize()
        _,_,row=source(db, batch_factory=historical_batch)
        with db.connection(write=True) as conn:publish(conn,[row])
        with db.connection() as conn:
            before=[tuple(r) for r in conn.execute('SELECT * FROM index_valuation_observations')]
    with instance_lock(db.path):db.initialize()
    with db.connection() as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM index_valuation_observations')]==before
        revision=conn.execute('SELECT revision,updated_at FROM index_read_revisions').fetchone()
        # v37 installs price definitions; v38 also invalidates after rebuilding facts.
        assert revision[0]==conn.execute('SELECT count(*) FROM index_price_definitions').fetchone()[0]+1 and revision[1]
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'index_read_%'").fetchone()[0]==24
    table=next(t for t in MaintenanceService(db).read()['tables'] if t['name']=='index_read_revisions')
    assert table['category']=='\u6307\u6570\u6570\u636e' and table['rows']==1
    assert table['time_basis']=='updated_at' and table['updated_at']==revision[1]
    backup=next((tmp_path/'backups').glob('pre-migration-*.sqlite3'))
    with sqlite3.connect(backup) as conn:assert conn.execute('PRAGMA user_version').fetchone()[0]==25
    with db.connection(write=True) as conn:conn.execute('DELETE FROM index_read_revisions')
    empty=next(t for t in MaintenanceService(db).read()['tables'] if t['name']=='index_read_revisions')
    assert empty['rows']==0 and empty['updated_at'] is None and empty['time_basis']=='updated_at'
