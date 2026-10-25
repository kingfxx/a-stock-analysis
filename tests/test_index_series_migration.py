"""v24→v25 preserves full observations, revisions and source constraints."""
import sqlite3

import pytest

from stock_analysis.infrastructure import storage
from stock_analysis.domains.indices.index_definitions import METRICS
from stock_analysis.domains.indices.index_repository import install_definitions, publish, read_indices
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.infrastructure.storage import Database, instance_lock
from test_index_collection import source


def test_upgrade_preserves_revisions_and_sources(tmp_path, monkeypatch):
    database = Database(tmp_path/'indices.sqlite3')
    migrations = storage.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(storage, 'MIGRATIONS', tuple(m for m in migrations if m[0]<25))
        database.initialize()
        batch, sid, row = source(database)
        month_batch, month_sid, month_row = source(database, 'legu', 'pe.ordinary')
        with database.connection(write=True) as conn:
            install_definitions(conn)
            columns = [r['name'] for r in conn.execute('PRAGMA table_info(index_valuation_observations)')]
            for ident, version, value, latest in [(5,1,13.1,0),(9,2,-2,0),(10,3,None,1)]:
                values = {**row, **{metric:None for metric in METRICS}, 'id':ident,
                          'pe_rolling':value, 'definition_version':'v1', 'version':version,
                          'is_latest':latest,'content_hash':f'hash-{ident}',
                          'withdrawal_reason':'provider withdrew' if value is None else None,
                          'obtained_at':'2026-10-11T01:00:00+00:00', 'updated_at':'2026-10-11T02:00:00+00:00'}
                conn.execute('INSERT INTO index_valuation_observations VALUES('+','.join('?' for _ in columns)+')',
                             tuple(values.get(field) for field in columns))
            for ident, day, value in [(11,'2026-10-01',0),(12,'2026-10-09',15)]:
                values = {**month_row, **{metric:None for metric in METRICS},'id':ident,
                          'observation_date':day,'frequency':'monthly','pe_ttm':value,
                          'definition_version':'v1','version':1,'is_latest':1,'content_hash':f'hash-{ident}',
                          'obtained_at':'2026-10-11T01:00:00+00:00','updated_at':'2026-10-11T02:00:00+00:00'}
                conn.execute('INSERT INTO index_valuation_observations VALUES('+','.join('?' for _ in columns)+')',
                             tuple(values.get(field) for field in columns))
            before = [dict(r) for r in conn.execute('SELECT * FROM index_valuation_observations ORDER BY id')]
    with instance_lock(database.path):
        database.initialize()
    with database.connection() as conn:
        expanded = 'f.id,s.index_code,s.provider,s.series_key,f.observation_date,s.frequency,'+','.join('f.'+m for m in METRICS)+',f.withdrawal_reason,s.definition_version,f.content_hash,f.version,f.is_latest,f.batch_id,f.source_id,f.obtained_at,f.updated_at'
        after = [dict(r) for r in conn.execute('SELECT '+expanded+' FROM index_valuation_observations f JOIN index_valuation_series s ON s.id=f.series_id ORDER BY f.id')]
        assert after == before
        assert conn.execute('SELECT count(*) FROM index_valuation_series').fetchone()[0] == 2
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='index_valuation_dates'").fetchone() is None
        assert conn.execute('SELECT created_at FROM index_valuation_series LIMIT 1').fetchone()[0]
    rows = read_indices(database,code='cn:000300',provider='legu',series='pe.ordinary')['rows']
    assert [r['id'] for r in rows] == [12]
    assert rows[0]['pe_ttm'] == 15
    _, _, new_row = source(database)
    with database.connection(write=True) as conn:
        assert publish(conn,[new_row])['revised'] == 1
    with database.connection(write=True) as conn:
        assert publish(conn,[new_row])['unchanged'] == 1
    with database.connection() as conn:
        versions = conn.execute('SELECT version,is_latest FROM index_valuation_observations WHERE series_id=(SELECT id FROM index_valuation_series WHERE provider=\'csindex\') ORDER BY version').fetchall()
        assert [r['version'] for r in versions] == [1,2,3,4]
        assert sum(r['is_latest'] for r in versions) == 1
        latest = dict(conn.execute('SELECT * FROM index_valuation_observations WHERE id=(SELECT max(id) FROM index_valuation_observations)').fetchone())
    for change in ({'id':None},{'id':None,'version':5},{'id':None,'version':5,'series_id':999999},
                   {'id':None,'version':5,'is_latest':0,'batch_id':month_batch}):
        values = {**latest, **change}
        with pytest.raises(sqlite3.IntegrityError):
            with database.connection(write=True) as conn:
                conn.execute('INSERT INTO index_valuation_observations('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
    with pytest.raises(sqlite3.IntegrityError,match='series identity'):
        with database.connection(write=True) as conn:
            conn.execute("UPDATE index_valuation_series SET frequency='monthly'")
    inventory = MaintenanceService(database).read()
    entry = next(r for r in inventory['tables'] if r['name']=='index_valuation_series')
    assert entry['category']=='指数数据' and entry['rows']==2
    assert entry['table_bytes']>0 and entry['index_bytes']>0
    assert entry['updated_at'] and entry['time_basis']=='created_at'
    backups = list((tmp_path/'backups').glob('pre-migration-*.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==24
        assert conn.execute('SELECT count(*) FROM index_valuation_observations').fetchone()[0]==5


def test_empty_series_inventory(tmp_path):
    database = Database(tmp_path/'empty.sqlite3')
    database.initialize()
    entry = next(r for r in MaintenanceService(database).read()['tables'] if r['name']=='index_valuation_series')
    assert entry['rows']==0 and entry['updated_at'] is None
    assert entry['category']=='指数数据' and entry['time_basis']=='created_at'
