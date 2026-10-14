import pytest

from quarterly_dashboard.market_suspended_price import validate_reference
from quarterly_dashboard.collection import begin_batch,archive_source
from quarterly_dashboard.market_trade_snapshot import normalized,FIELDS,publish
from quarterly_dashboard.storage import Database

def anchor():return dict(instrument='000001.SZ',price_date='2015-03-20',target_date='2015-03-31',pre_close=10,anchor_close=10,anchor_total_shares=100)
def events():return [dict(change_date='2015-01-01',total_shares=100)]

def test_unchanged_suspension_allows_reference():
    assert validate_reference(anchor(),{'total_shares':100},events(),[],[{'date':'2015-03-31'}])==[]

@pytest.mark.parametrize('change,reason',[
    ({'pre_close':9.5},'preclose_differs_from_last_raw_close'),
    ({'anchor_total_shares':99},'daily_total_shares_changed_or_missing'),
    ({'price_date':None},'invalid_anchor_or_preclose'),
    ({'pre_close':-1},'invalid_anchor_or_preclose'),
])
def test_bad_anchor_or_changed_daily_shares_stays_missing(change,reason):
    a=anchor();a.update(change)
    assert reason in validate_reference(a,{'total_shares':100},events(),[],[{}])

def test_capital_change_then_reversal_is_not_silently_accepted():
    es=events()+[dict(change_date='2015-03-25',total_shares=110),dict(change_date='2015-03-30',total_shares=100)]
    assert 'total_shares_event_during_suspension' in validate_reference(anchor(),{'total_shares':100},es,[],[{}])

def test_same_day_conflicting_events_reject_reference():
    es=events()+[dict(change_date='2015-03-31',total_shares=100),dict(change_date='2015-03-31',total_shares=101)]
    assert 'capital_event_daily_mismatch_or_missing' in validate_reference(anchor(),{'total_shares':100},es,[],[{}])

def test_cash_dividend_and_missing_suspension_evidence_rejected():
    reasons=validate_reference(anchor(),{'total_shares':100},events(),[dict(ex_date='2015-03-25')],[])
    assert 'ex_dividend_event_during_suspension' in reasons and 'no_same_day_suspension_evidence' in reasons

def test_event_on_anchor_date_already_in_anchor_and_target_day_included():
    assert validate_reference(anchor(),{'total_shares':100},events(),[dict(ex_date='2015-03-20')],[{}])==[]
    assert 'ex_dividend_event_during_suspension' in validate_reference(anchor(),{'total_shares':100},events(),[dict(ex_date='2015-03-31')],[{}])

def test_suspended_series_priority_and_original_retention(tmp_path):
    db=Database(tmp_path/'test.sqlite3');db.initialize()
    batch=begin_batch(db,'test','market.trade_snapshot','composite');source=archive_source(db,batch,'composite','market.trade_snapshot',{},1,raw=b'{}')
    row={f:None for f in FIELDS};row.update(SECURITY_CODE='000001',SECUCODE='000001.SZ',TRADE_DATE='2015-03-31')
    with db.connection(write=True) as c:
        publish(c,batch,[(normalized(row,'2015-03-31'),source,'now')],definition='historical-composite-v1')
        row.update(CLOSE_PRICE=10,TOTAL_SHARES=100,TOTAL_MARKET_CAP=1000)
        publish(c,batch,[(normalized(row,'2015-03-31'),source,'now')],definition='historical-suspended-preclose-v1')
        cap=c.execute('SELECT * FROM market_cap_latest').fetchone()
        assert cap['total_cap']==1000 and cap['source']=='composite:historical-suspended-preclose-v1'
        assert c.execute("SELECT close_price FROM market_trade_snapshot WHERE definition_version='historical-composite-v1'").fetchone()[0] is None
        row.update(CLOSE_PRICE=12,TOTAL_MARKET_CAP=1200)
        publish(c,batch,[(normalized(row,'2015-03-31'),source,'now')])
        caps=c.execute('SELECT * FROM market_cap_latest').fetchall()
        assert len(caps)==1 and caps[0]['total_cap']==1200 and caps[0]['source']=='eastmoney:market_trade_snapshot'
