import json
import sqlite3
from datetime import date,timedelta

import pytest
from quarterly_dashboard.index_price_codec import PriceCodec,encode_day,decode_day,encode_time,decode_time
from quarterly_dashboard.index_prices import publish_prices
from quarterly_dashboard.maintenance import table_time
from test_index_prices import db,seed,point,price_fact


@pytest.mark.parametrize('value',['0001-01-01','1969-12-31','2000-02-29','2026-10-18','9999-12-31'])
def test_date_encoding_is_exact(value):
    assert decode_day(encode_day(value))==value


@pytest.mark.parametrize('value',['1969-12-31T23:59:59.999999+00:00','2026-10-18T02:00:00.123456+00:00','2026-10-11T01:00:00Z'])
def test_time_encoding_is_exact(value):
    assert decode_time(encode_time(value))==value


@pytest.mark.parametrize('value',['1969-12-31T23:59:59.999999+00:00','1970-01-01T00:00:00.000000+00:00','2026-10-18T02:00:00.123456+00:00','2026-10-11T01:00:00Z'])
def test_logical_view_restores_exact_timestamp(db,value):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid)])
        conn.execute('UPDATE index_price_observations SET updated_at=?',(encode_time(value),))
        assert conn.execute('SELECT updated_at FROM index_price_observations_read').fetchone()[0]==value


def test_physical_constraints_and_indexed_date_filter(db):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid,volume=0,change_pct=-2)])
        row=conn.execute('SELECT * FROM index_price_observations').fetchone()
        assert isinstance(row['observation_date'],int) and isinstance(row['updated_at'],int)
        assert isinstance(row['content_hash'],bytes) and len(row['content_hash'])==32
        assert isinstance(json.loads(row['field_lineage_json']),list)
        columns={r['name'] for r in conn.execute('PRAGMA table_info(index_price_observations)')}
        assert not columns & {'batch_id','obtained_at','is_latest'}
        assert len(conn.execute('PRAGMA index_list(index_price_observations)').fetchall())==2
        plan=' '.join(r['detail'] for r in conn.execute('EXPLAIN QUERY PLAN SELECT close FROM index_price_observations WHERE series_id=? AND observation_date BETWEEN ? AND ?',(row['series_id'],encode_day('2026-10-01'),encode_day('2026-10-18'))))
        assert 'SEARCH' in plan and 'index_price_one_row_per_date' in plan
        with pytest.raises(sqlite3.IntegrityError):conn.execute("UPDATE index_price_observations SET content_hash='short'")
        with pytest.raises(sqlite3.IntegrityError):conn.execute('UPDATE index_price_observations SET source_id=999999')


def test_context_reads_are_batched_not_per_fact(db):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        rows=[point(batch,sid,observation_date=(date(2025,1,1)+timedelta(days=i)).isoformat()) for i in range(200)]
        publish_prices(conn,rows)
        physical=conn.execute('SELECT * FROM index_price_observations ORDER BY id').fetchall()
        statements=[];conn.set_trace_callback(statements.append)
        decoded=PriceCodec(conn).decode_rows(physical)
        conn.set_trace_callback(None)
        assert len(decoded)==200 and decoded[0]['obtained_at']=='2026-10-11T01:00:00Z'
        assert len([q for q in statements if 'FROM collection_sources' in q])==1
        assert len([q for q in statements if 'FROM index_price_series' in q])==1


def test_boolean_cannot_bypass_validation_as_unchanged_one(db):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:publish_prices(conn,[point(batch,sid,constituent_count=1)])
    with pytest.raises(ValueError):
        with db.connection(write=True) as conn:publish_prices(conn,[point(batch,sid,constituent_count=True)])


def test_maintenance_mixed_integer_and_text_times_uses_actual_latest(db):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid),point(batch,sid,observation_date='2026-10-10')])
        conn.execute("UPDATE index_price_observations SET updated_at='2026-10-01T00:00:00Z' WHERE observation_date=?",(encode_day('2026-10-09'),))
        conn.execute('UPDATE index_price_observations SET updated_at=? WHERE observation_date=?',(encode_time('2026-10-18T02:00:00.000001+00:00'),encode_day('2026-10-10')))
        value,basis=table_time(conn,'index_price_observations',[r['name'] for r in conn.execute('PRAGMA table_info(index_price_observations)')])
        assert value=='2026-10-18T02:00:00.000001+00:00' and '微秒' in basis


def test_unknown_snapshot_members_survive_as_explicit_fallback(db):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid)])
        row=price_fact(conn,'SELECT * FROM index_price_observations_read')
        lineage=json.loads(row['field_lineage_json']);lineage['extra_evidence']={'verified':'keep exact'}
        codec=PriceCodec(conn)
        payload=codec.encode_payload({'field_lineage_json':json.dumps(lineage)},row)
        assert json.loads(payload['field_lineage_json'])=={'fallback':lineage}
        conn.execute('UPDATE index_price_observations SET field_lineage_json=? WHERE id=?',(payload['field_lineage_json'],row['id']))
        result=price_fact(conn,'SELECT * FROM index_price_observations_read')
        assert json.loads(result['field_lineage_json'])==lineage


def test_numeric_history_does_not_expand_provenance(db,monkeypatch):
    from quarterly_dashboard.index_prices import read_prices
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:publish_prices(conn,[point(batch,sid)])
    monkeypatch.setattr(PriceCodec,'decode_rows',lambda *a:pytest.fail('Numeric history must not expand provenance'))
    rows=read_prices(db,index_code='cn:000300',provider='csindex',series_key='history.points',frequency='daily',return_type='unspecified',fields=('close',))
    assert rows[0]['observation_date']=='2026-10-09' and rows[0]['close']==100
    assert 'field_lineage_json' not in rows[0]


def test_backup_check_rejects_unknown_lineage_version(db):
    from quarterly_dashboard.storage import StorageError
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid)])
        conn.execute("UPDATE index_price_observations SET field_lineage_json='[99,1,0,[],null]'")
    with pytest.raises(StorageError,match='存储格式'):db.check()


def test_failed_migration_rolls_back_and_can_resume_after_fix(tmp_path,monkeypatch):
    from quarterly_dashboard import storage
    from quarterly_dashboard.storage import Database
    from quarterly_dashboard.index_prices import price_hash,DEFINITIONS,FIELDS
    from quarterly_dashboard.collection import encoded
    database=Database(tmp_path/'old.sqlite3')
    with monkeypatch.context() as old:
        old.setattr(storage,'MIGRATIONS',tuple(m for m in storage.MIGRATIONS if m[0]<=37))
        database.initialize();batch,sid,_=seed(database)
        with database.connection(write=True) as conn:
            conn.execute("INSERT INTO index_price_series VALUES(1,'cn:000300','csindex','history.points','daily','unspecified','v2','2026-10-01T00:00:00Z')")
            lineage={'_raw':{'revision':1,'fields':{'close':{'source_id':sid,'batch_id':batch,'obtained_at':'2026-10-11T01:00:00Z'}}}}
            values={f:None for f in FIELDS};values['close']=100
            hashed=price_hash(values,DEFINITIONS['csindex','history.points'],None,lineage)
            conn.execute("INSERT INTO index_price_observations(id,series_id,observation_date,close,content_hash,version,is_latest,batch_id,source_id,obtained_at,updated_at,field_lineage_json) VALUES(1,1,'2026-10-09',100,?,1,1,?,?,'2020-01-01T00:00:00Z','2026-10-11T01:00:00Z',?)",(hashed,batch,sid,encoded(lineage)))
    with pytest.raises(ValueError,match='不一致'):database.initialize()
    with sqlite3.connect(database.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==37
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='index_price_observations_compact'").fetchone()
        conn.execute("UPDATE index_price_observations SET obtained_at='2026-10-11T01:00:00Z'")
    database.initialize()
    with database.connection() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==38
        row=price_fact(conn,'SELECT * FROM index_price_observations_read')
        assert row['close']==100 and row['id']==1 and row['content_hash']==hashed
