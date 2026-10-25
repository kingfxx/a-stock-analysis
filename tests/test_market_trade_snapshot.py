import json
import sqlite3

import pytest
import requests

from stock_analysis.domains.market import market_trade_snapshot as m
from stock_analysis.collection.collection import RequestCollector, begin_batch, read_source
from stock_analysis.infrastructure.storage import Database
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.maintenance.maintenance import MaintenanceService


@pytest.fixture
def db(tmp_path):
    db=Database(tmp_path/'snapshot.sqlite3');db.initialize();return db


def row(code='000001'):
    result={f:None for f in m.FIELDS}
    result.update(SECURITY_CODE=code,SECUCODE=code+'.SZ',SECURITY_NAME_ABBR='测试公司',
                  TRADE_DATE='2025-12-31 00:00:00',TOTAL_MARKET_CAP=1000,TOTAL_SHARES=100,
                  CLOSE_PRICE=10,PE_TTM=-2,CHANGE_RATE=0)
    return result


class Clock:
    now=10000
    def time(self):return self.now
    def sleep(self,s):self.now+=s


class Session:
    def __init__(self,payloads):self.payloads=list(payloads);self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(kwargs)
        r=requests.Response();r.status_code=200;r.encoding='utf-8'
        r.headers['Content-Type']='application/json';r._content=json.dumps(self.payloads.pop(0)).encode()
        return r


def http(db,rows,count=None,pages=None):
    count=len(rows) if count is None else count
    pages=(count+499)//500 if pages is None else pages
    session=Session([dict(success=True,code=0,result=dict(count=count,pages=pages,data=rows))])
    clock=Clock()
    return RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a),session


def test_full_fields_negative_zero_idempotent_and_true_revision(db):
    client,s=http(db,[row()])
    first=m.collect_day(db,'test','2025-12-31',quarter='2025Q4',http=client)
    assert s.calls[0]['params']['columns']=='ALL'
    assert len(m.FIELDS)==22 and first['inserted']==1
    with db.connection() as c:
        r=c.execute('SELECT * FROM market_trade_snapshot').fetchone()
        assert r['pe_ttm']==-2 and r['change_rate']==0 and r['pb_mrq'] is None
        assert r['total_shares']==100 and r['source_trade_date'].endswith('00:00:00')
        original=r['source_id']
        assert c.execute('SELECT count(*) FROM sw_cap_facts').fetchone()[0]==0
    repeated=m.collect_day(db,'test2','2025-12-31',quarter='2025Q4',http=client)
    assert repeated['skipped'] and len(s.calls)==1
    same,_=http(db,[row()])
    assert m.collect_day(db,'review','2025-12-31',quarter='2025Q4',http=same,recheck=True)['unchanged']==1
    changed=row();changed['PE_TTM']=-3
    another,_=http(db,[changed])
    assert m.collect_day(db,'review2','2025-12-31',quarter='2025Q4',http=another,recheck=True)['revised']==1
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==2
        assert c.execute('SELECT source_id FROM market_trade_snapshot WHERE version=1').fetchone()[0]==original
    assert m.audit(db,[{**first,'sources':m.collect_day(db,'a','2025-12-31',http=http(db,[changed])[0])['sources']}])


@pytest.mark.parametrize('change',[
    {'TOTAL_SHARES':100.1},{'NEW_FIELD':1},{'TRADE_DATE':'2024-12-31'},
    {'TOTAL_MARKET_CAP':999999},{'PE_TTM':'NaN'}])
def test_invalid_rows_are_archived_but_never_published(db,change):
    data=row();data.update(change);client,_=http(db,[data])
    with pytest.raises(ValueError):m.collect_day(db,'bad','2025-12-31',http=client)
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==0
        source=c.execute('SELECT * FROM collection_sources').fetchone()
        assert source['parse_status']=='failed'
        assert c.execute('SELECT status FROM collection_batches').fetchone()[0]=='failed'
    assert read_source(db,source['id'])[0]


def test_truncated_pagination_and_null_withdrawal_rollback(db):
    client,_=http(db,[row()],count=501,pages=2)
    with pytest.raises(ValueError,match='页长'):m.collect_day(db,'bad','2025-12-31',http=client)
    client,_=http(db,[row()]);m.collect_day(db,'ok','2025-12-31',http=client)
    lost=row();lost['PE_TTM']=None
    with pytest.raises(ValueError,match='有值变空'):
        m.collect_day(db,'review','2025-12-31',http=http(db,[lost])[0],recheck=True)
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==1


def test_daily_and_quarterly_same_day_share_fact_and_source_foreign_key(db):
    a=m.collect_day(db,'quarter','2025-12-31',quarter='2025Q4',http=http(db,[row()])[0])
    b=m.collect_day(db,'daily','2025-12-31',http=http(db,[row()])[0])
    assert b['unchanged']==1
    with db.connection(write=True) as c:
        assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('UPDATE market_trade_snapshot SET batch_id=?',(b['batch_id'],))


def test_empty_source_is_known_gap_and_recovery_health_accepts_eastmoney(db):
    clock=Clock();session=Session([dict(success=False,code=9201,message='返回数据为空')])
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep)
    result=m.collect_day(db,'empty','2025-12-31',http=client)
    assert result['empty'] and result['coverage_status']=='empty'
    client,s=http(db,[row()])
    with db.connection(write=True) as c:
        c.execute("UPDATE collection_provider_state SET cooldown_until='1970-01-01T00:00:01+00:00',denials=1 WHERE source_system='eastmoney'")
    result=m.collect_day(db,'review','2025-12-31',http=client,recheck=True)
    assert result['rows']==1


def test_shared_metadata_versions_and_historical_range(db):
    b=begin_batch(db,'o','test','eastmoney',parser_version=m.PARSER,definition_version=m.DEFINITION,universe_version='42')
    with db.connection() as c:
        row=c.execute('SELECT * FROM collection_batches WHERE id=?',(b,)).fetchone()
        assert row['parser_version']==m.PARSER and row['universe_version']=='42'
    assert len(m.quarters('2015Q1','2026Q3'))==47


def test_failed_day_replays_archived_page_and_maintenance_reports_real_time(db):
    from datetime import datetime,timezone
    before=MaintenanceService(db).read()
    empty=next(t for t in before['tables'] if t['name']=='market_trade_snapshot')
    assert empty['rows']==0 and empty['updated_at'] is None and empty['category']=='行业数据'
    rows=[row(str(1000+n).zfill(6)) for n in range(500)]
    session=Session([dict(success=True,code=0,result=dict(count=501,pages=2,data=rows)),
                     dict(success=True,code=0,result=dict(count=501,pages=2,data=[]))])
    clock=Clock();clock.now=datetime.now(timezone.utc).timestamp()
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    with pytest.raises(ValueError,match='页长'):
        m.collect_day(db,'broken','2025-12-31',quarter='2025Q4',http=client)
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM market_trade_snapshot').fetchone()[0]==0
    session=Session([dict(success=True,code=0,result=dict(count=501,pages=2,data=[row('002000')]))])
    client=RequestCollector(db,session=session,clock=clock.time,sleep=clock.sleep,uniform=lambda a,b:a)
    result=m.collect_day(db,'resumed','2025-12-31',quarter='2025Q4',http=client)
    assert len(session.calls)==1 and session.calls[0]['params']['pageNumber']==2 and result['inserted']==501
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM collection_sources WHERE reused_source_id IS NOT NULL').fetchone()[0]==1
    assert m.audit(db,[result])[0]['source_rows']==501
    filled=next(t for t in MaintenanceService(db).read()['tables'] if t['name']=='market_trade_snapshot')
    assert filled['rows']==501 and filled['total_bytes']>0 and 'obtained_at' in filled['time_basis']


def test_new_snapshot_read_does_not_use_other_date_or_adjusted_price(db):
    m.collect_day(db,'q','2025-12-31',quarter='2025Q4',http=http(db,[row()])[0])
    with db.connection(write=True) as c:
        # A roster's date is all company_market_values needs; fixture has no stock focus.
        c.execute("INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status,member_import_id) VALUES('2026-01-01','fixture','{}','complete',NULL)")
        ident=c.execute('SELECT max(id) FROM sw_imports').fetchone()[0]
        c.execute("UPDATE sw_imports SET member_import_id=? WHERE id=?",(ident,ident))
        c.execute("INSERT INTO sw_cap_quarter_rosters VALUES('2025Q4','2025-12-31','current_constituents_backfill',?)",(ident,))
    result=IndustryService(db).company_market_values(['000001'],'2025-12-31')
    assert result['000001']['total_cap']==1000 and result['000001']['cap_trade_date']=='2025-12-31'
    assert result['000001']['cap_source']=='eastmoney:market_trade_snapshot'
