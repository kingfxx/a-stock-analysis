import json
import sqlite3

import pytest

from quarterly_dashboard.collection import archive_source, begin_batch, finish_batch, parsed_source
from quarterly_dashboard.index_collection import publish_segment
from quarterly_dashboard.index_prices import parse_prices, publish_prices, read_overlay, read_prices
from quarterly_dashboard.index_repository import catalog
from quarterly_dashboard.index_price_replay import replay
from quarterly_dashboard.maintenance import MaintenanceService
from quarterly_dashboard.storage import Database
from test_index_collection import response, http


@pytest.fixture
def db(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    return db


def seed(db, *, provider='csindex', code='000300', kind='broad', rows=None, dataset='index.cs.history'):
    batch=begin_batch(db,'test',dataset,provider,'2026-10-09','2026-10-09')
    sid=archive_source(db,batch,provider,dataset,dict(url='https://example.test',params={'indexCode':code}),1,
        raw=json.dumps({'data':rows or []}).encode(),response=response(),obtained_at='2026-10-11T01:00:00Z')
    parsed_source(db,sid,fields=['close'])
    identity=('sw:' if provider=='sws' else 'cn:')+code
    with db.connection(write=True) as c:
        catalog(c,identity,'test',kind,provider=provider,provider_code=code,source_id=sid,system='sw' if kind=='industry' else None,level=1 if kind=='industry' else None)
    return batch,sid,identity


def point(batch,sid,code='cn:000300',provider='csindex',key='history.points',**values):
    result=dict(index_code=code,provider=provider,series_key=key,frequency='daily',return_type='unspecified',
        observation_date='2026-10-09',batch_id=batch,source_id=sid,close=100)
    result.update(values)
    return result


def test_revision_provenance_and_series_separation(db):
    batch,sid,code=seed(db)
    row=point(batch,sid)
    with db.connection(write=True) as c:
        assert publish_prices(c,[row])==dict(inserted=1,revised=0,unchanged=0)
        assert publish_prices(c,[row])['unchanged']==1
        assert publish_prices(c,[{**row,'close':101}])['revised']==1
        assert publish_prices(c,[{**row,'return_type':'total_return'}])['inserted']==1
    with pytest.raises(ValueError,match='撤回'):
        with db.connection(write=True) as c:publish_prices(c,[{**row,'close':None,'explicit_nulls':True}])
    with db.connection(write=True) as c:
        assert publish_prices(c,[{**row,'close':None,'withdrawal_reason':'source withdrew'}])['revised']==1
    with db.connection() as c:
        assert c.execute('select count(*) from index_price_series').fetchone()[0]==2
        assert c.execute('select obtained_at from index_price_observations limit 1').fetchone()[0]=='2026-10-11T01:00:00Z'
        assert not c.execute('pragma foreign_key_check').fetchall()
    other,other_sid,_=seed(db)
    with pytest.raises(ValueError,match='来源'):
        with db.connection(write=True) as c:publish_prices(c,[{**row,'batch_id':other}])
    with pytest.raises(sqlite3.IntegrityError,match='identity'):
        with db.connection(write=True) as c:c.execute("update index_price_series set return_type='price'")


def test_industry_close_only_and_strategy_share_storage(db):
    batch,sid,code=seed(db,provider='sws',code='801010',kind='industry')
    prices,bad=parse_prices([dict(swindexcode='801010',bargaindate='2026-10-09',closeindex='2582.15')],
        provider='sws',series_key='analysis.points',code_key='swindexcode',date_key='bargaindate',start='2026-10-09',end='2026-10-09',
        trading_days={'2026-10-09'},batch_id=batch,source_id=sid)
    assert not bad and prices[0]['open'] is None and prices[0]['close']==2582.15
    with db.connection(write=True) as c:publish_prices(c,prices)
    batch,sid,code=seed(db,code='000015',kind='strategy')
    with db.connection(write=True) as c:publish_prices(c,[point(batch,sid,code)])
    with db.connection() as c:
        assert c.execute('select count(*) from index_price_observations').fetchone()[0]==2
    fetched=read_prices(db,index_code='sw:801010',provider='sws',series_key='analysis.points',frequency='daily',return_type='unspecified')
    assert len(fetched)==1 and fetched[0]['close']==2582.15 and fetched[0]['high'] is None


def test_partial_ohlc_missing_rows_and_boundary_placeholder(db):
    batch,sid,code=seed(db)
    raw=dict(indexCode='000688',tradeDate='20161011',open=None,high=None,low=None,close=1000,peg=None,tradingVol=1)
    rows=[raw,{**raw,'tradeDate':'20200102'},dict(raw,tradeDate='20200103',close=1010)]
    prices,bad=parse_prices(rows,provider='csindex',series_key='history.points',code_key='indexCode',date_key='tradeDate',
        start='2016-10-11',end='2020-01-03',trading_days={'2016-10-11','2020-01-02','2020-01-03'},batch_id=batch,source_id=sid)
    assert len(prices)==2 and bad[0]['reason']=='boundary_placeholder_duplicate'
    assert prices[0]['close']==1000 and prices[0]['open'] is None
    prices,bad=parse_prices([dict(indexCode='000300',tradeDate='20261009',close=101)],provider='csindex',series_key='history.points',
        code_key='indexCode',date_key='tradeDate',start='2026-10-09',end='2026-10-09',trading_days={'2026-10-09'},batch_id=batch,source_id=sid)
    assert not bad and prices[0]['close']==101 and prices[0]['high'] is None


def test_atomic_price_failure_rolls_back_valuation_month(db):
    from test_index_collection import source
    batch,sid,val=source(db)
    invalid=point(batch,sid,high=90)
    with pytest.raises(ValueError,match='开高低收'):
        publish_segment(db,batch,[val],[],{},prices=[invalid])
    with db.connection() as c:
        assert c.execute('select count(*) from index_valuation_observations').fetchone()[0]==0
        assert c.execute('select count(*) from index_price_observations').fetchone()[0]==0


def test_future_sw_publication_waits_for_all_pages(db):
    from quarterly_dashboard.index_collection import IndexCollector
    base=dict(swindexcode='801010',swindexname='industry',bargaindate='2026-10-09',pe=10,pb=2,dp=1,closeindex=2000)
    client,session,_=http(db,[response({'data':{'count':2,'results':[base]}}),response({'data':{'count':2,'results':[]}})])
    collector=IndexCollector(db,'test',request_collector=client,sw_page_size=1);collector.trading_days={'2026-10-09'}
    with pytest.raises(ValueError,match='截断'):collector.collect_sw('2026-10-09','2026-10-09','full')
    with db.connection() as c:
        assert c.execute('select count(*) from index_price_observations').fetchone()[0]==0


def test_monthly_sampling_keeps_snapshots_and_selects_latest(db):
    batch,sid,code=seed(db,provider='legu',dataset='index.legu.pe')
    rows=[{**point(batch,sid,provider='legu',key='pe.points'),'frequency':'monthly','observation_date':d} for d in ('2026-10-01','2026-10-09')]
    with db.connection(write=True) as c:publish_prices(c,rows)
    assert len(read_prices(db,index_code=code,provider='legu',series_key='pe.points',frequency='daily',return_type='unspecified'))==0
    rows=read_prices(db,index_code=code,provider='legu',series_key='pe.points',frequency='monthly',return_type='unspecified')
    assert [r['observation_date'] for r in rows]==['2026-10-09']


def test_overlay_selects_one_daily_index_close_without_stitching(db):
    daily_batch,daily_source,code=seed(db)
    daily=[point(daily_batch,daily_source,observation_date=day,close=value) for day,value in
           [('2026-10-08',4550),('2026-10-09',4600)]]
    with db.connection(write=True) as c:publish_prices(c,daily)
    sina_batch,sina_source,_=seed(db,provider='sina',dataset='index.price.range_probe')
    sina=[point(sina_batch,sina_source,provider='sina',observation_date=day,close=value) for day,value in
          [('2026-10-07',4400),('2026-10-08',4500),('2026-10-09',4700)]]
    with db.connection(write=True) as c:publish_prices(c,sina)
    monthly_batch,monthly_source,_=seed(db,provider='legu',dataset='index.legu.pe')
    monthly=[{**point(monthly_batch,monthly_source,provider='legu',key='pe.points',observation_date=day),
              'frequency':'monthly','close':value} for day,value in [('2026-10-01',4300),('2026-10-09',4800)]]
    with db.connection(write=True) as c:publish_prices(c,monthly)
    selected=read_overlay(db,code)
    assert (selected['provider'],selected['frequency'],selected['return_type'])==('csindex','daily','unspecified')
    assert [(r['observation_date'],r['close']) for r in selected['rows']]==[('2026-10-08',4550),('2026-10-09',4600)]


def test_industry_overlay_prefers_sws_and_price_updates_expire_chart_cache(db):
    from quarterly_dashboard.index_read_service import IndexReadService
    batch,sid,code=seed(db,provider='sws',code='801010',kind='industry',dataset='index.sw.analysis')
    rows=[{**point(batch,sid,code,provider='sws',key='analysis.points',observation_date=day,close=value)}
          for day,value in [('2026-10-08',2580),('2026-10-09',2600)]]
    with db.connection(write=True) as c:publish_prices(c,rows)
    service=IndexReadService(db)
    try:
        initial=service.price_overlay(code)
        revision=service.status()
        assert b'"provider":"sws"' in initial
        with db.connection(write=True) as c:publish_prices(c,[{**rows[-1],'close':2610}])
        assert service.status()!=revision
        assert b'2610' in service.price_overlay(code)
    finally:service.close()


def test_archive_replay_no_network_idempotent_and_original_time(db,monkeypatch):
    import requests
    batch,sid,code=seed(db,rows=[dict(indexCode='000300',tradeDate='20261009',open=100,high=102,low=99,close=101)])
    finish_batch(db,batch,'complete')
    cal=begin_batch(db,'test','index.calendar','sina')
    # Fixed calendar decoder is independently tested; use a synthetic calendar for this replay test.
    cid=archive_source(db,cal,'sina','index.calendar',{},1,raw=b'calendar',response=response())
    parsed_source(db,cid);finish_batch(db,cal,'complete')
    monkeypatch.setattr('quarterly_dashboard.index_collection.IndexCollector.decode_calendar',lambda self,text:{'2026-10-09'})
    monkeypatch.setattr(requests.Session,'get',lambda *a,**k:pytest.fail('replay must not request sources'))
    first=replay(db,start='2026-10-09',end='2026-10-09')
    assert first['network_requests']==0 and first['results'][0]['price_counts']['inserted']==1
    from quarterly_dashboard.index_price_audit import audit
    assert audit(db)['status']=='passed'
    second=replay(db,start='2026-10-09',end='2026-10-09')
    assert second['skipped_batches']==1 and not second['results']
    with db.connection() as c:
        row=c.execute('select * from index_price_observations').fetchone()
        reused=c.execute('select * from collection_sources where id=?',(row['source_id'],)).fetchone()
        original=c.execute('select * from collection_sources where id=?',(sid,)).fetchone()
        assert row['obtained_at']==original['obtained_at']
        assert reused['reused_source_id']==sid and reused['file_path']==original['file_path']
        assert c.execute('select count(*) from index_valuation_observations').fetchone()[0]==0


def test_maintenance_empty_and_populated_times(db):
    names={'index_price_series':'created_at','index_price_definitions':'updated_at','index_price_observations':'updated_at / obtained_at'}
    before=MaintenanceService(db).read()
    for name in names:
        item=next(t for t in before['tables'] if t['name']==name)
        assert item['category']=='指数数据' and item['rows']==0 and item['updated_at'] is None
        assert '尚未登记' not in item['description'] and item['total_bytes']>0
    batch,sid,code=seed(db)
    with db.connection(write=True) as c:publish_prices(c,[point(batch,sid)])
    after=MaintenanceService(db).read()
    for name in names:
        item=next(t for t in after['tables'] if t['name']==name)
        assert item['rows']>0 and item['updated_at'] and item['time_basis'] in ('created_at','updated_at','updated_at, obtained_at')


def test_future_collection_writes_points_without_extra_requests(db):
    from quarterly_dashboard.index_collection import IndexCollector
    raw=dict(indexCode='000300',indexNameCn='CSI300',tradeDate='20261009',peg=13,open=100,high=102,low=99,close=101)
    client,session,_=http(db,[response({'data':[raw]})])
    collector=IndexCollector(db,'test',request_collector=client);collector.trading_days={'2026-10-09'}
    collector.collect_cs_codes('2026-10-09','2026-10-09','full',{'000300':'CSI300'})
    assert len(session.calls)==1
    with db.connection() as c:
        val=c.execute('select source_id,batch_id from index_valuation_observations').fetchone()
        point=c.execute('select source_id,batch_id,close from index_price_observations').fetchone()
        assert (val['source_id'],val['batch_id'])==(point['source_id'],point['batch_id']) and point['close']==101
