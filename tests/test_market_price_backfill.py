import copy
import sqlite3

import pytest

from quarterly_dashboard.market_price_backfill import periods, positive, publish
from quarterly_dashboard.maintenance import MaintenanceService, table_time
from quarterly_dashboard.storage import Database
from quarterly_dashboard.stock_library import StockLibrary


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'prices.sqlite3');db.initialize()
    with db.connection(write=True) as conn:
        member=conn.execute("INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status) VALUES('2026-10-10','members','{}','complete')").lastrowid
    candidate={'member_import_id':member,'sources':[{'source':'bigquant:cn_stock_real_bar1d',
        'dataset':'cn_stock_real_bar1d','params':{'sql':'SELECT date,instrument,close'},
        'file_path':'data/verification/samples/test.json.gz','content_hash':'a'*64,'obtained_at':'2026-10-10T00:00:00+00:00'}],
        'rows':[{'security_code':'600000','exchange':'sh','quarter_end':'2026-06-30',
                 'trade_date':'2026-06-30','close':12.3,'source_index':0}],
        'coverage':[{'quarter_end':'2026-06-30','expected':2,'valid':1,'missing':1}]}
    return db,candidate


def test_publish_is_sparse_idempotent_and_does_not_follow_stocks(setup):
    db,candidate=setup
    result=publish(db,candidate,'a'*64)
    assert result['inserted']==1
    assert publish(db,candidate,'a'*64)['reused']
    assert publish(db,candidate,'b'*64)['skipped']==1
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM raw_daily_prices').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM instruments').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM market_quarterly_prices').fetchone()[0]==1
    assert not StockLibrary(db).read()['stocks']
    db.check()


def test_conflict_and_invalid_source_roll_back_batch(setup):
    db,candidate=setup;publish(db,candidate,'a'*64)
    changed=copy.deepcopy(candidate);changed['rows'][0]['close']=13
    with pytest.raises(ValueError,match='冲突'):publish(db,changed,'b'*64)
    changed=copy.deepcopy(candidate)
    changed['rows'][0]['quarter_end']='2026-09-30'
    changed['sources'][0]['dataset']='calendar'
    with pytest.raises(sqlite3.IntegrityError,match='source batch'):publish(db,changed,'c'*64)
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM market_price_batches').fetchone()[0]==1
        assert conn.execute('SELECT close FROM market_quarterly_prices').fetchone()[0]==12.3


def test_maintenance_registration_and_empty_timestamps(setup):
    db,candidate=setup
    names=('market_quarterly_prices','market_price_sources','market_price_batches')
    empty={r['name']:r for r in MaintenanceService(db).read()['tables']}
    for name in names:
        assert empty[name]['rows']==0
        assert '尚未登记' not in empty[name]['description']
        with db.connection() as conn:
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+name+')')]
            assert table_time(conn,name,columns)[0] is None
    publish(db,candidate,'a'*64)
    tables={r['name']:r for r in MaintenanceService(db).read()['tables']}
    for name in names:
        assert tables[name]['rows']==1
        assert tables[name]['total_bytes']>0
        with db.connection() as conn:
            columns=[r['name'] for r in conn.execute('PRAGMA table_info('+name+')')]
            value,basis=table_time(conn,name,columns)
            assert value and '未记录' not in basis


def test_quarters_and_invalid_prices():
    assert len(periods('2014-09-30','2026-09-30'))==49
    with pytest.raises(ValueError):periods('2014-09-29','2026-09-30')
    assert positive(1) and not any(positive(v) for v in (None,0,-1,float('nan'),float('inf'),True,'12'))


def test_preparation_preserves_missing_and_excludes_prelisting(tmp_path,monkeypatch):
    from quarterly_dashboard import market_price_backfill as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    folder=tmp_path/'data/verification/samples/prices';folder.mkdir(parents=True)
    inventory={'member_import_id':1,'calendar_sources':[],
        'members':[{'stock_code':'600000','listing_date':'2014-01-01'},
                   {'stock_code':'300750','listing_date':'2026-07-01'}],
        'targets':[{'quarter_end':'2026-06-30','trade_date':'2026-06-30'},
                   {'quarter_end':'2026-09-30','trade_date':'2026-09-30'}]}
    module.save(folder/'inventory.json',inventory)
    records=[{'date':'2026-06-30','instrument':'600000.SH','close':12},
             {'date':'2026-09-30','instrument':'600000.SH','close':None},
             {'date':'2026-09-30','instrument':'300750.SZ','close':40}]
    source=module.archive(folder,'prices-2026',records,module.SOURCE,'cn_stock_real_bar1d',{})
    module.save(folder/'prices-2026.json',{'source':source})
    result=module.prepare(folder)
    assert result['rows']==2 and result['missing']==1
    coverage=module.load(folder/'candidate.json')['coverage']
    assert coverage[0]['expected']==1 and coverage[0]['not_listed']==1
    assert coverage[1]['expected']==2 and coverage[1]['missing']==1
    (tmp_path/source['file_path']).write_bytes(b'corrupted')
    with pytest.raises(ValueError,match='哈希'):module.prepare(folder)
