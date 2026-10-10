import copy

import pytest

from industry_financial_fixture import PublishedIndustryService, publish_financials
from quarterly_dashboard.industry_service import company_valuation
from quarterly_dashboard.storage import Database
from test_industries import fixture_bundle


@pytest.mark.parametrize('cap,profit,close,bps,expected',[
    (100,10,12,2,{'pe':10,'pb':6}),
    (100,0,12,0,{'pe':None,'pb':None}),
    (100,-10,12,-2,{'pe':None,'pb':None}),
    (None,10,None,2,{'pe':None,'pb':None}),
    (100,None,12,2,{'pe':None,'pb':6}),
    (100,10,12,None,{'pe':10,'pb':None}),
])
def test_valuation_missing_and_nonpositive(cap,profit,close,bps,expected):
    assert company_valuation(cap,profit,close,bps)==expected


def test_quarter_end_valuation_same_date_and_ttm_across_modes(tmp_path):
    db=Database(tmp_path/'valuation.sqlite3');db.initialize()
    service=PublishedIndustryService(db)
    bundle=fixture_bundle()
    bundle['financials'].append({'stock_code':'300750','period':'2024-12-31','revenue':30,
        'parent_profit':3,'notice_date':None,'provenance':{'source':'test'}})
    quote=['']*47
    quote[2]='300750';quote[30]='20250630150000';quote[46]='2.35'
    bundle['caps']=[{'stock_code':'300750','trade_date':'2025-06-30','total_cap':120,
                     'provenance':{'source':'tencent:qt.gtimg.cn','field':'45','raw':'~'.join(quote)}}]
    for fact in bundle['financials']:
        fact['bps']=2
    # TTM profit = 1.5 + 3 - 1 = 3.5; quarterly missing Q1 must not affect PE.
    service.import_bundle(copy.deepcopy(bundle),capture_local=False)
    with db.connection(write=True) as conn:
        member_id=conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]
        conn.execute("INSERT INTO sw_cap_quarter_rosters VALUES('2025Q2','2025-06-30','current_constituents_backfill',?)",(member_id,))
    from quarterly_dashboard.market_price_backfill import publish
    publish(db,{'member_import_id':member_id,'sources':[{'source':'bigquant:cn_stock_real_bar1d',
        'dataset':'cn_stock_real_bar1d','params':{},'file_path':'test.json.gz','content_hash':'b'*64,
        'obtained_at':'2026-10-10'}], 'rows':[{'security_code':'300750','exchange':'sz',
        'quarter_end':'2025-06-30','trade_date':'2025-06-30','close':12,'source_index':0}],
        'coverage':[{'quarter_end':'2025-06-30','expected':1,'valid':1,'missing':0}]},'c'*64)
    for mode in ('ttm','ytd','quarter'):
        result=service.read(code='300750',period='2025-06-30',mode=mode)
        company=next(c for c in result['companies'] if c['code']=='300750')
        assert company['pe']==pytest.approx(120/3.5)
        assert company['pb']==6
        assert company['bps']==2 and company['pb_trade_date']=='2025-06-30'
        assert company['cap_trade_date']=='2025-06-30'
    earlier=service.read(code='300750',period='2024-06-30')
    assert all(c['pe'] is None and c['pb'] is None for c in earlier['companies'])
    # Publish a genuine BPS revision: warm financial cache must expire.
    for fact in bundle['financials']:
        if fact['period']=='2025-06-30':fact['bps']=4
    publish_financials(db,bundle['financials'])
    company=lambda:next(c for c in service.read(code='300750',period='2025-06-30')['companies'] if c['code']=='300750')
    assert company()['pb']==3
    for bps in (None,0,-1):
        for fact in bundle['financials']:
            if fact['period']=='2025-06-30':fact['bps']=bps
        publish_financials(db,bundle['financials'])
        assert company()['pb'] is None  # no old-quarter or Tencent source fallback
    for fact in bundle['financials']:
        if fact['period']=='2025-06-30':fact['bps']=4
    publish_financials(db,bundle['financials'])
    with db.connection(write=True) as conn:
        conn.execute("UPDATE market_quarterly_prices SET trade_date='2025-06-27'")
    assert company()['pb'] is None
    with db.connection(write=True) as conn:
        conn.execute("UPDATE market_quarterly_prices SET trade_date='2025-06-30'")
    assert company()['pb']==3
    with db.connection(write=True) as conn:
        conn.execute('DELETE FROM market_quarterly_prices')
    assert company()['pb'] is None
