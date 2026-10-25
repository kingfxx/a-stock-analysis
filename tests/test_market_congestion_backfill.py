import json

import pytest

from stock_analysis.domains.market import market_congestion as m
from stock_analysis.domains.market import market_congestion_backfill as backfill
from stock_analysis.collection.collection import archive_source,begin_batch,finish_batch,parsed_source,read_source
from stock_analysis.infrastructure.storage import Database


@pytest.fixture
def db(tmp_path_factory):
    result=Database(tmp_path_factory.mktemp('bk')/'db.sqlite3');result.initialize()
    batch=begin_batch(result,'seed',m.DATASETS['api_history'],'legu')
    sid=archive_source(result,batch,'legu',m.DATASETS['api_history'],{},1,raw=b'{}');parsed_source(result,sid)
    rows=m.parse_api(dict(items=[dict(date='2026-07-30',congestion=.5),dict(date='2026-07-31'),dict(date='2026-08-03')]))
    with result.connection(write=True) as conn:m.publish(conn,rows,'api_history',batch,sid)
    finish_batch(result,batch,'complete')
    return result


def records(day='2026-07-31'):
    return [dict(date=day,instrument=f'{600000+i:06d}.SH',amount=float(i),volume=i) for i in range(21)]


def evidence():
    rows,stats=backfill.calculate(records()+records('2026-08-03'),{'2026-07-31':3832.26,'2026-08-03':3800})
    return dict(rows=rows,statistics=stats,price_facts=[],upstream={},definition=m.DEFINITIONS[m.CALCULATED_IDENTITY])


def test_calculation_ceil_zero_and_missing():
    data=records()+[dict(date='2026-07-31',instrument='920001.BJ',amount=None,volume=0)]
    rows,stats=backfill.calculate(data,{'2026-07-31':3832.26})
    assert stats['2026-07-31']['eligible']==21
    assert stats['2026-07-31']['top_count']==2
    assert stats['2026-07-31']['excluded_null_amount']==1
    assert rows[0]['value']==pytest.approx(39/210)
    assert rows[0]['total_amount_yi']==pytest.approx(210/1e8)
    assert rows[0]['top5_amount_yi']==pytest.approx(39/1e8)
    assert rows[0]['close']==3832.26


@pytest.mark.parametrize('change', [dict(amount=-1),dict(amount=float('nan')),dict(amount=None,volume=1),dict(instrument='900001.SH')])
def test_invalid_inputs_rejected(change):
    data=records();data[0].update(change)
    with pytest.raises(ValueError):backfill.calculate(data,{'2026-07-31':3832.26})


def test_duplicate_and_missing_close_rejected():
    with pytest.raises(ValueError):backfill.calculate(records()*2,{'2026-07-31':3832.26})
    with pytest.raises(ValueError):backfill.calculate(records(),{})
    with pytest.raises(ValueError):backfill.calculate(records(),{'2026-07-31':None})


def test_apply_source_separation_read_and_idempotency(db):
    with db.connection() as conn:
        before=[dict(r) for r in conn.execute("SELECT * FROM market_indicator_observations WHERE provider='legu'")]
        budget=[dict(r) for r in conn.execute('SELECT * FROM collection_provider_state')]
    data=evidence();backup=db.path.parent/'backup.sqlite3'
    result=backfill.apply(db,data,backup_path=backup)
    assert result['inserted']==2 and backup.exists()
    raw,source=read_source(db,result['source_id'])
    assert source['hash_basis']=='derived_json / gzip_file'
    assert json.loads(raw)['rows']==data['rows']
    assert json.loads(source['request_json'])['network_request'] is False
    read=m.read(db)
    assert read['valid_dates']==3 and read['missing_dates']==[]
    assert read['supplemented_dates']==read['source_missing_dates']==['2026-07-31','2026-08-03']
    assert read['channels'][m.CALCULATED_CHANNEL]['valid']==2
    with db.connection() as conn:
        assert [dict(r) for r in conn.execute("SELECT * FROM market_indicator_observations WHERE provider='legu'")]==before
        assert [dict(r) for r in conn.execute('SELECT * FROM collection_provider_state')]==budget
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        source_count=conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]
    second_backup=db.path.parent/'unused.sqlite3'
    assert backfill.apply(db,data,backup_path=second_backup)['status']=='already_present'
    assert not second_backup.exists()
    with db.connection() as conn:assert conn.execute('SELECT count(*) FROM collection_sources').fetchone()[0]==source_count


def test_observed_legu_value_prevents_whole_segment(db):
    data=evidence()
    with db.connection(write=True) as conn:
        conn.execute("UPDATE market_indicator_observations SET value=.6,value_status='observed' WHERE observation_date='2026-08-03'")
    with pytest.raises(ValueError,match='已有Legu'):
        backfill.apply(db,data,backup_path=db.path.parent/'backup.sqlite3')
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM market_indicator_observations WHERE provider='bigquant'").fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE dataset=? AND status='failed'",(m.CALCULATED_DATASET,)).fetchone()[0]==1


def test_write_transaction_rolls_back_partial_insert(db,monkeypatch):
    original=m.publish
    def partial(conn,rows,channel,batch,source):
        original(conn,rows[:1],channel,batch,source)
        raise ValueError('fixture second-row failure')
    monkeypatch.setattr(m,'publish',partial)
    with pytest.raises(ValueError,match='second-row'):
        backfill.apply(db,evidence(),backup_path=db.path.parent/'backup.sqlite3')
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM market_indicator_observations WHERE provider='bigquant'").fetchone()[0]==0


def test_stale_price_reference_is_rejected(db):
    data=evidence();data['price_facts']=[dict(id=-1,content_hash='old')]
    with pytest.raises(ValueError,match='点位已修订'):
        backfill.apply(db,data,backup_path=db.path.parent/'backup.sqlite3')
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM market_indicator_observations WHERE provider='bigquant'").fetchone()[0]==0
