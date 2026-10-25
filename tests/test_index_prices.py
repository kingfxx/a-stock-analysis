import json
import sqlite3

import pytest

from stock_analysis.collection.collection import archive_source, begin_batch, finish_batch, parsed_source
from stock_analysis.domains.indices.index_collection import publish_segment
from stock_analysis.domains.indices.index_prices import parse_prices, publish_prices, read_overlay, read_prices
from stock_analysis.domains.indices.index_repository import catalog
from stock_analysis.domains.indices.index_price_replay import replay
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.infrastructure.storage import Database
from test_index_collection import response, http
from stock_analysis.domains.indices.index_price_codec import PriceCodec


def price_fact(conn,sql,params=()):
    row=conn.execute(sql,params).fetchone()
    return PriceCodec(conn).decode_rows([dict(row)])[0] if row else None


@pytest.fixture
def db(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    return db


def seed(db, *, provider='csindex', code='000300', kind='broad', rows=None, dataset='index.cs.history'):
    batch=begin_batch(db,'test',dataset,provider,'2026-10-09','2026-10-09',scope={'code':code,'level':1 if kind=='industry' else None})
    sid=archive_source(db,batch,provider,dataset,dict(url='https://example.test',params={'indexCode':code,'startDate':'20261009','endDate':'20261009'}),1,
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
        assert c.execute('select obtained_at from index_price_observations_read limit 1').fetchone()[0]=='2026-10-11T01:00:00Z'
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
        assert c.execute('select count(*) from index_price_observations_read').fetchone()[0]==2
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
        assert c.execute('select count(*) from index_price_observations_read').fetchone()[0]==0


def test_future_sw_publication_waits_for_all_pages(db):
    from stock_analysis.domains.indices.index_collection import IndexCollector
    base=dict(swindexcode='801010',swindexname='industry',bargaindate='2026-10-09',pe=10,pb=2,dp=1,closeindex=2000)
    client,session,_=http(db,[response({'data':{'count':2,'results':[base]}}),response({'data':{'count':2,'results':[]}})])
    collector=IndexCollector(db,'test',request_collector=client,sw_page_size=1);collector.trading_days={'2026-10-09'}
    with pytest.raises(ValueError,match='截断'):collector.collect_sw('2026-10-09','2026-10-09','full')
    with db.connection() as c:
        assert c.execute('select count(*) from index_price_observations_read').fetchone()[0]==0


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
    from stock_analysis.domains.indices.index_read_service import IndexReadService
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
    monkeypatch.setattr('stock_analysis.domains.indices.index_collection.IndexCollector.decode_calendar',lambda self,text:{'2026-10-09'})
    monkeypatch.setattr(requests.Session,'get',lambda *a,**k:pytest.fail('replay must not request sources'))
    first=replay(db,start='2026-10-09',end='2026-10-09')
    assert first['network_requests']==0 and first['results'][0]['price_counts']['inserted']==1
    from stock_analysis.domains.indices.index_price_audit import audit
    assert audit(db)['status']=='passed'
    second=replay(db,start='2026-10-09',end='2026-10-09')
    assert second['skipped_batches']==1 and not second['results']
    with db.connection() as c:
        row=c.execute('select * from index_price_observations_read').fetchone()
        reused=c.execute('select * from collection_sources where id=?',(row['source_id'],)).fetchone()
        original=c.execute('select * from collection_sources where id=?',(sid,)).fetchone()
        assert row['obtained_at']==original['obtained_at']
        assert reused['reused_source_id']==sid and reused['file_path']==original['file_path']
        assert c.execute('select count(*) from index_valuation_observations').fetchone()[0]==0


def test_sina_archive_replay_uses_klc_decoder_without_payload_branch_leak(db,monkeypatch):
    import py_mini_racer
    batch=begin_batch(db,'test','index.price.range_probe','sina','2026-10-09','2026-10-09',scope={'code':'000300'})
    sid=archive_source(db,batch,'sina','index.price.range_probe',
        dict(url='https://quotes.sina.cn/company/sh000300/history'),1,raw=b'var hq="encoded";',response=response(),
        obtained_at='2026-10-11T01:00:00Z')
    parsed_source(db,sid,fields=['open','high','low','close']);finish_batch(db,batch,'complete')
    with db.connection(write=True) as conn:catalog(conn,'cn:000300','沪深300','broad',provider='sina',provider_code='000300',source_id=sid)
    cal=begin_batch(db,'test','index.calendar','sina')
    cid=archive_source(db,cal,'sina','index.calendar',{},1,raw=b'calendar',response=response())
    parsed_source(db,cid);finish_batch(db,cal,'complete')
    monkeypatch.setattr('stock_analysis.domains.indices.index_collection.IndexCollector.decode_calendar',lambda self,text:{'2026-10-09'})
    class Decoder:
        def eval(self,source):pass
        def call(self,name,text):return [dict(date='2026-10-09',open=99,high=102,low=98,close=101)]
    monkeypatch.setattr(py_mini_racer,'MiniRacer',Decoder)
    result=replay(db,start='2026-10-09',end='2026-10-09')
    assert result['results'][0]['price_observations']==1
    with db.connection() as conn:
        row=price_fact(conn,'SELECT * FROM index_price_observations_read WHERE is_latest=1')
        assert row['close']==101 and row['obtained_at']=='2026-10-11T01:00:00Z'


def test_replay_cancellation_keeps_first_segment_and_resume_skips_it(db,monkeypatch):
    from stock_analysis.domains.indices import index_price_replay as replay_module
    import requests
    for value in (101,102):
        batch,sid,_=seed(db,rows=[dict(indexCode='000300',tradeDate='20261009',open=100,high=value,low=99,close=value)])
        finish_batch(db,batch,'complete')
    cal=begin_batch(db,'test','index.calendar','sina')
    calendar_source=archive_source(db,cal,'sina','index.calendar',{},1,raw=b'calendar',response=response())
    parsed_source(db,calendar_source);finish_batch(db,cal,'complete')
    monkeypatch.setattr('stock_analysis.domains.indices.index_collection.IndexCollector.decode_calendar',lambda self,text:{'2026-10-09'})
    monkeypatch.setattr(requests.Session,'get',lambda *a,**k:pytest.fail('replay must stay offline'))
    original=replay_module.publish_segment;calls={'count':0}
    def cancel_after_commit(*args,**kwargs):
        result=original(*args,**kwargs);calls['count']+=1
        if calls['count']==1:
            with db.connection(write=True) as conn:
                conn.execute("UPDATE index_collection_operations SET cancel_requested=1 WHERE status='running'")
        return result
    monkeypatch.setattr(replay_module,'publish_segment',cancel_after_commit)
    from stock_analysis.collection.collection import Cancelled
    with pytest.raises(Cancelled):replay(db,start='2026-10-09',end='2026-10-09')
    monkeypatch.setattr(replay_module,'publish_segment',original)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE dataset='index.points.replay' AND status='complete'").fetchone()[0]==1
        assert conn.execute("SELECT count(*) FROM index_price_observations_read WHERE is_latest=1").fetchone()[0]==1
    resumed=replay(db,start='2026-10-09',end='2026-10-09')
    assert resumed['skipped_batches']==1 and resumed['network_requests']==0
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE dataset='index.points.replay' AND status='complete'").fetchone()[0]==2
        assert conn.execute("SELECT count(*) FROM index_price_observations_read WHERE is_latest=1").fetchone()[0]==1


def test_sws_replay_rejects_request_level_mismatch(db,monkeypatch):
    batch=begin_batch(db,'test','index.sw.analysis','sws','2026-10-09','2026-10-09',scope={'level':1})
    params={'page':1,'page_size':1,'type':'DAY','swindexcode':'all','index_type':'二级行业',
        'start_date':'2026-10-09','end_date':'2026-10-09'}
    sid=archive_source(db,batch,'sws','index.sw.analysis',{'params':params},1,
        raw=json.dumps({'code':'200','data':{'count':1,'results':[{'swindexcode':'801010','swindexname':'行业',
            'bargaindate':'20261009','closeindex':1000,'index_type':'二级行业'}]}}).encode(),response=response(),
        obtained_at='2026-10-11T01:00:00Z')
    parsed_source(db,sid,fields=['closeindex']);finish_batch(db,batch,'complete')
    cal=begin_batch(db,'test','index.calendar','sina')
    calendar_source=archive_source(db,cal,'sina','index.calendar',{},1,raw=b'calendar',response=response())
    parsed_source(db,calendar_source);finish_batch(db,cal,'complete')
    monkeypatch.setattr('stock_analysis.domains.indices.index_collection.IndexCollector.decode_calendar',lambda self,text:{'2026-10-09'})
    with pytest.raises(ValueError,match='明确层级'):
        replay(db,start='2026-10-09',end='2026-10-09')


def test_maintenance_empty_and_populated_times(db):
    names={'index_price_series':'created_at','index_price_definitions':'updated_at','index_price_observations':'updated_at / obtained_at'}
    before=MaintenanceService(db).read()
    for name in names:
        item=next(t for t in before['tables'] if t['name']==name)
        assert item['category']=='指数数据'
        if name=='index_price_definitions':
            assert item['rows']==5 and item['updated_at']
        else:
            assert item['rows']==0 and item['updated_at'] is None
        assert '尚未登记' not in item['description'] and item['total_bytes']>0
    batch,sid,code=seed(db)
    with db.connection(write=True) as c:publish_prices(c,[point(batch,sid)])
    after=MaintenanceService(db).read()
    for name in names:
        item=next(t for t in after['tables'] if t['name']==name)
        assert item['rows']>0 and item['updated_at'] and item['time_basis'] in ('created_at','updated_at','updated_at, obtained_at','updated_at（整数微秒解码）, collection_sources.obtained_at')


def test_future_collection_writes_points_without_extra_requests(db):
    from stock_analysis.domains.indices.index_collection import IndexCollector
    raw=dict(indexCode='000300',indexNameCn='CSI300',tradeDate='20261009',peg=13,open=100,high=102,low=99,close=101)
    client,session,_=http(db,[response({'data':[raw]})])
    collector=IndexCollector(db,'test',request_collector=client);collector.trading_days={'2026-10-09'}
    collector.collect_cs_codes('2026-10-09','2026-10-09','full',{'000300':'CSI300'})
    assert len(session.calls)==1
    with db.connection() as c:
        val=c.execute('select source_id,batch_id from index_valuation_observations').fetchone()
        point=c.execute('select source_id,batch_id,close from index_price_observations_read').fetchone()
        assert (val['source_id'],val['batch_id'])==(point['source_id'],point['batch_id']) and point['close']==101


def test_market_fields_mapping_partial_updates_and_constraints(db):
    from stock_analysis.domains.indices.index_prices import FIELDS, validate_values
    batch,sid,code=seed(db)
    raw=dict(indexCode='000300',tradeDate='20261009',open=100,high=102,low=99,close=101,
        tradingVol=123456, tradingValue=2.5, change=-1.25, changePct=-0.2, consNumber=300)
    rows,bad=parse_prices([raw],provider='csindex',series_key='history.points',code_key='indexCode',date_key='tradeDate',
        start='2026-10-09',end='2026-10-09',trading_days={'2026-10-09'},batch_id=batch,source_id=sid)
    assert not bad and rows[0]['volume']==123456 and rows[0]['amount']==250_000_000
    assert rows[0]['change_points']==-1.25 and rows[0]['change_pct']==-.2 and rows[0]['constituent_count']==300
    assert set(rows[0]['present_fields'])=={'open','high','low','close','volume','amount','change_points','change_pct','constituent_count'}
    with db.connection(write=True) as conn: publish_prices(conn,rows)
    sparse,bad=parse_prices([dict(indexCode='000300',tradeDate='20261009',close=101)],provider='csindex',series_key='history.points',
        code_key='indexCode',date_key='tradeDate',start='2026-10-09',end='2026-10-09',trading_days={'2026-10-09'},batch_id=batch,source_id=sid)
    with db.connection(write=True) as conn:
        assert publish_prices(conn,sparse)['unchanged']==1
    with db.connection() as conn:
        stored=price_fact(conn,'SELECT * FROM index_price_observations_read WHERE is_latest=1')
        assert stored['amount']==250_000_000 and stored['change_pct']==-.2
    for values in ({'volume':float('nan')},{'amount':float('inf')},{'amount_ratio':100.01},{'constituent_count':1.5}):
        with pytest.raises(ValueError):validate_values(values)
    with db.connection(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE index_price_observations SET constituent_count=1.5 WHERE id=?',(stored['id'],))


def test_derived_amount_ratio_tracks_parent_return_type_and_withdraws_missing_upstream(db):
    from stock_analysis.domains.market import market_congestion as congestion
    from stock_analysis.domains.indices.index_price_derived import recompute
    batch,sid,_=seed(db,code='000680')
    rows,bad=parse_prices([dict(indexCode='000680',tradeDate='20261009',close=100,tradingValue=10)],provider='csindex',
        series_key='history.points',code_key='indexCode',date_key='tradeDate',start='2026-10-09',end='2026-10-09',
        trading_days={'2026-10-09'},batch_id=batch,source_id=sid)
    assert not bad and rows[0]['return_type']=='total_return'
    with db.connection(write=True) as conn:publish_prices(conn,rows)
    from test_market_congestion import seed as market_seed
    market_batch,market_source=market_seed(db,'api_history')
    market_rows=congestion.parse_api({'items':[dict(date='2026-10-09',congestion=.25,close=3800,top5AmountYi=5,totalAmountYi=20)]})
    with db.connection(write=True) as conn:congestion.publish(conn,market_rows,'api_history',market_batch,market_source)
    finish_batch(db,market_batch,'complete')
    result=recompute(db,'test',through='2026-10-09')
    assert result['facts']==1 and result['status']=='complete'
    with db.connection() as conn:
        row=price_fact(conn,"SELECT f.*,s.return_type FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE s.provider='csindex'")
        assert row['return_type']=='total_return' and row['amount_ratio']==50
        assert row['amount']==1_000_000_000 and row['volume'] is None
        assert json.loads(row['field_lineage_json'])['estimates']['amount_ratio']['kind']=='estimated'
        source=conn.execute('SELECT * FROM collection_sources WHERE id=?',(row['source_id'],)).fetchone()
        assert source['id']==sid and row['batch_id']==batch
        original_id=row['id']
    assert recompute(db,through='2026-10-09')['facts']==0
    revise=dict(rows[0],amount=None,present_fields=['amount'],withdrawal_reason='source withdrew')
    with db.connection(write=True) as conn:publish_prices(conn,[revise])
    result=recompute(db,'test',through='2026-10-09')
    assert result['facts']==1 and result['status']=='partial'
    with db.connection() as conn:
        row=price_fact(conn,"SELECT f.*,s.return_type FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE s.provider='csindex' AND f.is_latest=1")
        assert row['amount_ratio'] is None and row['id']==original_id
        assert json.loads(row['field_lineage_json'])['estimates']['amount_ratio']['upstream_status']=='upstream_missing:index raw field amount'
    zero=dict(rows[0],amount=0,present_fields=['amount'])
    with db.connection(write=True) as conn:publish_prices(conn,[zero])
    checks={'count':0}
    def cancel_before_publish():
        checks['count']+=1
        return checks['count']>1
    from stock_analysis.collection.collection import Cancelled
    with pytest.raises(Cancelled):recompute(db,'test',through='2026-10-09',cancelled=cancel_before_publish)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM collection_batches WHERE dataset='index.price.derived'").fetchone()[0]==0
    result=recompute(db,'test',through='2026-10-09')
    assert result['facts']==1 and result['status']=='complete'
    with db.connection() as conn:
        row=price_fact(conn,"SELECT f.*,s.return_type FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE s.provider='csindex' AND f.is_latest=1")
        assert row['amount_ratio']==0
        assert conn.execute('SELECT count(*) FROM index_price_observations_read').fetchone()[0]==1


def test_derived_recompute_checks_cancellation_by_chunks_not_per_fact(db):
    from stock_analysis.domains.market import market_congestion as congestion
    from stock_analysis.domains.indices.index_price_derived import recompute
    batch,sid,_=seed(db)
    rows=[]
    with db.connection(write=True) as conn:
        for offset in range(300):
            code=f'{900000+offset:06d}'
            identity='cn:'+code
            catalog(conn,identity,'synthetic','broad',provider='csindex',provider_code=code,source_id=sid)
            rows.append(point(batch,sid,code=identity,amount=10))
        publish_prices(conn,rows)
    from test_market_congestion import seed as market_seed
    market_batch,market_source=market_seed(db,'api_history')
    market_rows=congestion.parse_api({'items':[dict(date='2026-10-09',congestion=.25,close=3800,top5AmountYi=5,totalAmountYi=20)]})
    with db.connection(write=True) as conn:congestion.publish(conn,market_rows,'api_history',market_batch,market_source)
    finish_batch(db,market_batch,'complete')
    checks={'count':0}
    def count_checks():checks['count']+=1;return False
    result=recompute(db,'test-chunked-cancel',through='2026-10-09',cancelled=count_checks)
    assert result['facts']==300
    assert checks['count']<=4


def test_missing_response_key_inherits_previous_value_but_null_is_withdrawal(db):
    from stock_analysis.domains.indices.index_prices import parse_prices
    first,sid,_=seed(db)
    rows,_=parse_prices([dict(indexCode='000300',tradeDate='20261009',close=100,tradingValue=2)],provider='csindex',
        series_key='history.points',code_key='indexCode',date_key='tradeDate',start='2026-10-09',end='2026-10-09',
        trading_days={'2026-10-09'},batch_id=first,source_id=sid)
    with db.connection(write=True) as conn:publish_prices(conn,rows)
    second,sid2,_=seed(db)
    sparse,_=parse_prices([dict(indexCode='000300',tradeDate='20261009',close=101)],provider='csindex',
        series_key='history.points',code_key='indexCode',date_key='tradeDate',start='2026-10-09',end='2026-10-09',
        trading_days={'2026-10-09'},batch_id=second,source_id=sid2)
    with db.connection(write=True) as conn:publish_prices(conn,sparse)
    with db.connection() as conn:
        row=price_fact(conn,'SELECT * FROM index_price_observations_read WHERE is_latest=1')
        assert row['close']==101 and row['amount']==200_000_000
    third,sid3,_=seed(db)
    explicit_null,_=parse_prices([dict(indexCode='000300',tradeDate='20261009',close=101,tradingValue=None)],provider='csindex',
        series_key='history.points',code_key='indexCode',date_key='tradeDate',start='2026-10-09',end='2026-10-09',
        trading_days={'2026-10-09'},batch_id=third,source_id=sid3)
    with pytest.raises(ValueError,match='撤回'):
        with db.connection(write=True) as conn:publish_prices(conn,explicit_null)


def test_sw_field_units_and_derived_amount_revision_are_independent(db):
    from stock_analysis.domains.market import market_congestion as congestion
    from stock_analysis.domains.indices.index_price_derived import recompute
    batch,sid,_=seed(db,provider='sws',code='801010',kind='industry',dataset='index.sw.analysis')
    raw=dict(swindexcode='801010',bargaindate='2026-10-09',closeindex=2000,bargainamount=1.25,markup=-1.2,
        turnoverrate=120,bargainsumrate=0,meanprice=0,negotiablessharesum1=2.2,negotiablessharesum2=3.3)
    rows,bad=parse_prices([raw],provider='sws',series_key='analysis.points',code_key='swindexcode',date_key='bargaindate',
        start='2026-10-09',end='2026-10-09',trading_days={'2026-10-09'},batch_id=batch,source_id=sid)
    assert not bad
    assert (rows[0]['volume'],rows[0]['change_pct'],rows[0]['turnover_rate'],rows[0]['amount_ratio'])==(125_000_000,-1.2,120,0)
    assert (rows[0]['mean_price'],rows[0]['float_market_cap'],rows[0]['mean_float_market_cap'])==pytest.approx((0,220_000_000,330_000_000))
    with db.connection(write=True) as conn:publish_prices(conn,rows)
    from test_market_congestion import seed as market_seed
    market_batch,market_source=market_seed(db,'api_history')
    market_rows=congestion.parse_api({'items':[dict(date='2026-10-09',congestion=.25,close=3800,top5AmountYi=5,totalAmountYi=20)]})
    with db.connection(write=True) as conn:congestion.publish(conn,market_rows,'api_history',market_batch,market_source)
    finish_batch(db,market_batch,'complete')
    result=recompute(db,'test',through='2026-10-09')
    assert result['facts']==1
    with db.connection() as conn:
        derived=conn.execute("SELECT f.amount,f.version FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE s.provider='sws' AND f.is_latest=1").fetchone()
        before=(conn.execute("SELECT count(*) FROM collection_batches WHERE dataset='index.price.derived'").fetchone()[0],
                conn.execute("SELECT count(*) FROM collection_sources WHERE dataset='index.price.derived'").fetchone()[0])
    assert derived['amount']==0
    result=recompute(db,'test',through='2026-10-09')
    assert result['facts']==0 and result['status']=='current'
    with db.connection() as conn:
        after=(conn.execute("SELECT count(*) FROM collection_batches WHERE dataset='index.price.derived'").fetchone()[0],
               conn.execute("SELECT count(*) FROM collection_sources WHERE dataset='index.price.derived'").fetchone()[0])
    assert after==before
    revised_batch,revised_source,_=seed(db,provider='sws',code='801010',kind='industry',dataset='index.sw.analysis')
    changed=dict(rows[0],amount_ratio=50,present_fields=['amount_ratio'],batch_id=revised_batch,source_id=revised_source)
    with db.connection(write=True) as conn:publish_prices(conn,[changed])
    assert recompute(db,'test',through='2026-10-09')['facts']==1
    with db.connection() as conn:
        derived=conn.execute("SELECT f.amount,f.version FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE s.provider='sws' AND f.is_latest=1").fetchone()
        assert derived['amount']==1_000_000_000 and derived['version']==4


def test_duplicate_new_source_keeps_row_and_field_provenance(db):
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:
        publish_prices(conn,[point(batch,sid,amount=10)])
        before=price_fact(conn,'SELECT * FROM index_price_observations_read')
    other,other_sid,_=seed(db)
    with db.connection(write=True) as conn:
        assert publish_prices(conn,[point(other,other_sid,amount=10)])['unchanged']==1
        assert price_fact(conn,'SELECT * FROM index_price_observations_read')==before


def test_estimate_denominator_revision_withdrawal_and_official_priority(db):
    from stock_analysis.domains.market import market_congestion as congestion
    from stock_analysis.domains.indices.index_price_derived import recompute
    from stock_analysis.domains.indices.index_prices import price_hash,DEFINITIONS,FIELDS
    from test_market_congestion import seed as market_seed
    batch,sid,_=seed(db)
    with db.connection(write=True) as conn:publish_prices(conn,[point(batch,sid,amount=1_000_000_000)])
    def denominator(total):
        b,s=market_seed(db)
        rows=congestion.parse_api({'items':[dict(date='2026-10-09',congestion=.25,close=3800,top5AmountYi=total*.25 if total else None,totalAmountYi=total)]})
        with db.connection(write=True) as conn:
            if total is None:
                # Publish an invalid zero denominator as a new revision; keep referenced history immutable.
                rows=congestion.parse_api({'items':[dict(date='2026-10-09',congestion=0,close=3800,top5AmountYi=0,totalAmountYi=0)]})
            congestion.publish(conn,rows,'api_history',b,s)
        finish_batch(db,b,'complete')
    denominator(20)
    assert recompute(db)['facts']==1
    with db.connection() as conn:first=price_fact(conn,'SELECT * FROM index_price_observations_read')
    assert recompute(db)['facts']==0
    denominator(40)
    assert recompute(db)['facts']==1
    with db.connection() as conn:
        row=price_fact(conn,'SELECT * FROM index_price_observations_read')
        assert row['amount_ratio']==25 and row['id']==first['id'] and row['source_id']==sid
    denominator(None)
    assert recompute(db)['facts']==1
    assert recompute(db)['facts']==0
    with db.connection(write=True) as conn:
        row=price_fact(conn,'SELECT * FROM index_price_observations_read')
        assert row['amount_ratio'] is None
        # Simulate a later provider definition delivering an official target value.
        lineage=json.loads(row['field_lineage_json']);lineage['estimates'].pop('amount_ratio')
        values={k:row[k] for k in FIELDS};values['amount_ratio']=33
        conn.execute('UPDATE index_price_observations SET amount_ratio=33,field_lineage_json=?,content_hash=?',
            (json.dumps({'fallback':lineage}),bytes.fromhex(price_hash(values,DEFINITIONS['csindex','history.points'],None,lineage))))
    assert recompute(db)['facts']==0
    with db.connection() as conn:assert conn.execute('SELECT amount_ratio FROM index_price_observations_read').fetchone()[0]==33


def test_v36_migration_preserves_latest_id_and_is_immediately_idempotent(tmp_path,monkeypatch):
    from stock_analysis.infrastructure import storage
    from stock_analysis.collection.collection import encoded
    from stock_analysis.domains.indices.index_prices import legacy_definition
    db=Database(tmp_path/'upgrade.sqlite3')
    migrations=storage.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(storage,'MIGRATIONS',tuple(m for m in migrations if m[0]<=36));db.initialize()
        batch,sid,_=seed(db)
        with db.connection(write=True) as conn:
            conn.execute('INSERT INTO index_price_definitions VALUES(?,?,?,?,?)',('csindex','history.points','v1',encoded(legacy_definition('csindex','history.points')),'2026-10-01T00:00:00Z'))
            conn.execute("INSERT INTO index_price_series VALUES(17,'cn:000300','csindex','history.points','daily','unspecified','v1','2026-10-01T00:00:00Z')")
            conn.execute("INSERT INTO index_price_observations VALUES(41,17,'2026-10-09',NULL,NULL,NULL,99,NULL,'old',1,0,?,?,?,?)",(batch,sid,'2026-10-11T01:00:00Z','2026-10-11T01:00:00Z'))
            conn.execute("INSERT INTO index_price_observations VALUES(42,17,'2026-10-09',NULL,NULL,NULL,100,NULL,'latest',2,1,?,?,?,?)",(batch,sid,'2026-10-11T01:00:00Z','2026-10-11T01:00:00Z'))
    db.initialize()
    with db.connection(write=True) as conn:
        row=price_fact(conn,'SELECT * FROM index_price_observations_read')
        assert row['id']==42 and row['close']==100 and row['version']==2
        assert conn.execute('SELECT definition_version FROM index_price_series').fetchone()[0]=='v2'
        assert publish_prices(conn,[point(batch,sid)])['unchanged']==1
        assert conn.execute('SELECT count(*) FROM index_price_observations_read').fetchone()[0]==1
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(sqlite3.IntegrityError,match='identity'):
        with db.connection(write=True) as conn:conn.execute("UPDATE index_price_series SET return_type='price'")


def test_bigquant_gap_denominator_fallback_priority_and_roundtrip(db):
    from stock_analysis.domains.market import market_congestion as m
    from stock_analysis.domains.indices.index_price_derived import recompute
    from test_market_congestion import seed as market_seed
    day='2026-07-31'
    b,s,_=seed(db)
    with db.connection(write=True) as c:
        publish_prices(c,[point(b,s,observation_date=day,amount=1_000_000_000),
                          point(b,s,observation_date='2026-08-28',amount=1_000_000_000)])
    def bigquant(total):
        batch=begin_batch(db,'test',m.CALCULATED_DATASET,'bigquant',domain='market')
        sid=archive_source(db,batch,'bigquant',m.CALCULATED_DATASET,dict(url='local:test'),1,raw=b'{}')
        parsed_source(db,sid,fields=['total_amount_yi'])
        rows=[dict(observation_date=d,value=.5,close=3800,top5_amount_yi=total/2,total_amount_yi=total,
                   value_status='observed',precision_digits=None) for d in (day,'2026-08-28')]
        with db.connection(write=True) as c:m.publish(c,rows,m.CALCULATED_CHANNEL,batch,sid)
        finish_batch(db,batch,'complete')
    swb,sws,swcode=seed(db,provider='sws',code='801010',kind='industry')
    with db.connection(write=True) as c:
        publish_prices(c,[point(swb,sws,swcode,provider='sws',key='analysis.points',observation_date=day,amount_ratio=5)])
    bigquant(20)
    assert recompute(db)['facts']==2
    with db.connection() as c:
        row=price_fact(c,'SELECT * FROM index_price_observations_read ORDER BY observation_date,id LIMIT 1')
        estimate=json.loads(row['field_lineage_json'])['estimates']['amount_ratio']
        assert row['amount_ratio']==50 and estimate['parent_market']['provider']=='bigquant'
        assert 'BigQuant' in estimate['formula'] and 'BigQuant' in estimate['market_basis']
        assert isinstance(json.loads(c.execute('SELECT field_lineage_json FROM index_price_observations ORDER BY observation_date,id LIMIT 1').fetchone()[0]),list)
        assert c.execute('SELECT amount_ratio FROM index_price_observations_read ORDER BY observation_date DESC LIMIT 1').fetchone()[0] is None
    assert recompute(db)['facts']==0
    with db.connection() as c:
        industry=price_fact(c,"SELECT f.* FROM index_price_observations_read f JOIN index_price_series s ON s.id=f.series_id WHERE s.provider='sws'")
        assert industry['amount']==100_000_000 and industry['amount_ratio']==5
        assert 'BigQuant' in json.loads(industry['field_lineage_json'])['estimates']['amount']['formula']
    bigquant(40)
    assert recompute(db)['facts']==2
    with db.connection() as c:assert price_fact(c,'SELECT * FROM index_price_observations_read ORDER BY observation_date,id LIMIT 1')['amount_ratio']==25
    with db.connection(write=True) as c:
        c.execute("UPDATE market_indicator_observations SET is_latest=0 WHERE provider='bigquant'")
    assert recompute(db)['facts']==2
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM index_price_observations').fetchone()[0]==3
        assert c.execute("SELECT count(*) FROM index_price_observations WHERE amount_ratio IS NOT NULL AND series_id=(SELECT id FROM index_price_series WHERE provider='csindex')").fetchone()[0]==0
    with db.connection(write=True) as c:
        c.execute("UPDATE market_indicator_observations SET is_latest=1 WHERE provider='bigquant' AND version=2")
    assert recompute(db)['facts']==2
    batch,sid=market_seed(db)
    rows=m.parse_api({'items':[dict(date=day,congestion=.5,close=3800,top5AmountYi=50,totalAmountYi=100)]})
    with db.connection(write=True) as c:m.publish(c,rows,'api_history',batch,sid)
    finish_batch(db,batch,'complete')
    assert recompute(db)['facts']==2
    with db.connection() as c:
        row=price_fact(c,'SELECT * FROM index_price_observations_read ORDER BY observation_date,id LIMIT 1')
        assert row['amount_ratio']==10
        assert json.loads(row['field_lineage_json'])['estimates']['amount_ratio']['parent_market']['provider']=='legu'
    assert recompute(db)['facts']==0
