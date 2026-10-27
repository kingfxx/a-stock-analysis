import math
from pathlib import Path

import pytest

from stock_analysis.domains.valuation.dcf import (calculate_beta, calculate_debt_cost, calculate_effective_tax,
                                     sum_interest_bearing_debt, read_risk_free_rate, calculate_dcf, calculate_growth_dcf, discount_cash_flows,
                                     forecast_standard, forecast_three_stage, estimate_wacc,
                                     read_dcf, _growth_recommendation)
from stock_analysis.domains.financials.fundamental_service import SOURCE
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult


def test_constant_cash_flow_and_terminal_year_eleven():
    result = calculate_dcf(100, 0, 10, 0, shares=10, price=50)
    assert len(result['rows']) == 10
    assert result['total_value'] == pytest.approx(1000)
    assert result['per_share'] == pytest.approx(100)
    assert result['reference_difference_pct'] == pytest.approx(50)
    growing = calculate_dcf(1e8, 3, 9, 2, shares=1e8)
    assert growing['terminal_value'] == pytest.approx(1e8 * 1.03**10 * 1.02 / .07)
    assert growing['terminal_present_value'] == pytest.approx(growing['terminal_value'] / 1.09**10)
    assert growing['per_share'] == pytest.approx(growing['total_value'] / 1e8)


@pytest.mark.parametrize('args', [(None,3,9,2),(math.nan,3,9,2),(100,3,2,2),(100,3,1,2),(100,-100,9,2),(100,3,9,-100),(1e308,100,9,2),(True,3,9,2)])
def test_invalid_dcf_inputs(args):
    with pytest.raises(ValueError):
        calculate_dcf(*args)


def test_negative_zero_cash_flows_and_missing_shares():
    negative = calculate_dcf(-100,3,9,2,shares=10,price=50)
    assert negative['total_value'] < 0
    assert negative['reference_difference_pct'] is None
    zero = calculate_dcf(0,3,9,2,shares=0)
    assert zero['total_value'] == 0 and zero['terminal_share_pct'] is None
    assert zero['per_share'] is None


def test_three_stage_compounds_at_boundaries_and_terminal_year_eleven():
    flows = forecast_three_stage(1e8, 15, 8, 4)
    assert flows[0] == 1e8 * 1.15
    assert flows[2] == pytest.approx(1e8 * 1.15**3)
    assert flows[3] == flows[2] * 1.08
    assert flows[6] == pytest.approx(1e8 * 1.15**3 * 1.08**4)
    assert flows[7] == flows[6] * 1.04
    assert flows[9] == pytest.approx(1e8 * 1.15**3 * 1.08**4 * 1.04**3)
    result = calculate_growth_dcf(1e8, 15, 8, 4, 9, 2, shares=1e8, price=10)
    assert [r['fcf'] for r in result['rows']] == flows
    assert result['explicit_value'] == sum(flow / 1.09**t for t, flow in enumerate(flows, 1))
    assert result['terminal_value'] == pytest.approx(flows[-1] * 1.02 / .07)
    assert result['terminal_present_value'] == pytest.approx(result['terminal_value'] / 1.09**10)
    assert result['per_share'] == result['total_value'] / 1e8
    assert result['reference_difference_pct'] == (1 - 10 / result['per_share']) * 100
    assert result['mode'] == 'simplified'


def test_standard_golden_result_is_unchanged():
    result = calculate_dcf(123456789, 3.25, 9.75, 1.85, shares=98765432, price=12.34)
    assert result['rows'][0] == dict(year=1, fcf=127469134.6425, present_value=116144997.39635536)
    assert result['rows'][-1] == dict(year=10, fcf=169986949.54722834, present_value=67045601.485568404)
    assert result['explicit_value'] == 896070017.0561643
    assert result['terminal_value'] == 2191540609.036102
    assert result['terminal_present_value'] == 864379052.063942
    assert result['total_value'] == 1760449069.1201062
    assert result['per_share'] == 17.824546842665622
    assert result['reference_difference_pct'] == 30.769628485239064
    assert result['terminal_share_pct'] == 49.09991815304087
    assert result['warnings'] == []


def test_growth_and_standard_use_same_engine_for_equal_growth():
    standard = calculate_dcf(100, 3, 9, 2, shares=10, price=50)
    growth = calculate_growth_dcf(100, 3, 3, 3, 9, 2, shares=10, price=50)
    for key in ('explicit_value', 'terminal_value', 'terminal_present_value', 'total_value',
                'per_share', 'reference_difference_pct', 'terminal_share_pct'):
        assert growth[key] == pytest.approx(standard[key])
    assert forecast_standard(100, 3) == [100 * 1.03**t for t in range(1, 11)]


def test_growth_accepts_negative_fractional_rates_and_zero_discount():
    result = calculate_growth_dcf(100, -1.25, -2.5, -3.75, -.5, -4)
    assert result['rows'][0]['fcf'] == 98.75
    assert result['rows'][0]['present_value'] == pytest.approx(98.75 / .995)
    assert any('负折现率' in w for w in result['warnings'])
    assert calculate_growth_dcf(100, 1, 0, -1, 0, -2)['total_value'] > 0
    with pytest.raises(ValueError):
        calculate_dcf(100, 1, 0, -2)  # Original standard validation remains in effect.
    with pytest.raises(ValueError):
        calculate_dcf(100, 1, -.5, -2)


@pytest.mark.parametrize('args', [
    (None, 15, 8, 4, 9, 2), (True, 15, 8, 4, 9, 2), (100, math.nan, 8, 4, 9, 2),
    (100, 15, -100, 4, 9, 2), (100, 15, 8, math.inf, 9, 2),
    (100, 15, 8, 4, 2, 2), (100, 15, 8, 4, -100, -101),
    (100, 15, 8, 4, 9, -100), (100, 15, 8, 4, None, 2),
    (1e308, 100, 8, 4, 9, 2), (100, 15, 8, 4, 1e308, 2),
])
def test_growth_invalid_inputs_and_overflow(args):
    with pytest.raises(ValueError):
        calculate_growth_dcf(*args)


@pytest.mark.parametrize('flows', [None, [], [100] * 9, [100] * 11,
                                    [100] * 9 + [None], [100] * 9 + [math.nan],
                                    [100] * 9 + [True], [1e308] * 10])
def test_shared_discount_engine_rejects_invalid_sequences(flows):
    with pytest.raises(ValueError):
        discount_cash_flows(flows, 9, 2)


@pytest.mark.parametrize('base', [0, -100])
def test_growth_nonpositive_base_remains_visible_with_warning(base):
    result = calculate_growth_dcf(base, 15, 8, 4, 9, 2, shares=10, price=50)
    assert result['total_value'] <= 0
    assert any('基准FCF非正' in w for w in result['warnings'])
    assert result['reference_difference_pct'] is None
    if base == 0:
        assert result['terminal_share_pct'] is None


def test_growth_risk_warnings_do_not_clip_assumptions():
    result = calculate_growth_dcf(100, 31, 32, -1, 2.01, 2)
    assert result['rows'][0]['fcf'] == 131
    assert result['rows'][3]['fcf'] == result['rows'][2]['fcf'] * 1.32
    assert any('超过30%' in w for w in result['warnings'])
    assert any('阶段增长率上升' in w for w in result['warnings'])
    assert any('永续增长率高于' in w for w in result['warnings'])
    assert any('占比超过75%' in w for w in result['warnings'])
    assert not any('超过30%' in w for w in calculate_growth_dcf(100, 30, 8, 4, 9, 2)['warnings'])


def test_wacc_matches_weighted_cost_and_missing_is_not_zero():
    assert estimate_wacc(2,1.2,5,4,25,20)['wacc_pct'] == pytest.approx(7)
    assert estimate_wacc(2,-0.5,5,4,25,20)['equity_cost_pct'] == pytest.approx(-0.5)
    with pytest.raises(ValueError):
        estimate_wacc(2,None,5,4,25,20)
    with pytest.raises(ValueError):
        estimate_wacc(2,1,5,4,25,101)


def test_beta_uses_aligned_weekly_returns_and_preserves_negative_beta():
    from datetime import date, timedelta
    end=date(2025,12,31); stock=[]; market=[]; m=s=100.0
    for i in range(60):
        day=(end-timedelta(days=(59-i)*7)).isoformat()
        stock.append({'date':day,'close':s});market.append({'date':day,'close':m})
        change=.01 if i%2 else -.01
        m*=1+change;s*=1-.5*change
    result=calculate_beta(stock,market,years=2,as_of='2025-12-31',min_samples=50)
    assert result['value']==pytest.approx(-.5,abs=1e-8)
    assert result['sample_count']==59


def test_beta_marks_short_history_unavailable():
    rows=[{'date':f'2025-01-{day:02d}','close':100+day} for day in range(1,6)]
    result=calculate_beta(rows,rows,years=5,as_of='2025-12-31')
    assert result['value'] is None and result['status']=='unavailable'


def test_local_risk_free_latest_valid_before_asof_and_revision(tmp_path,monkeypatch):
    from test_bond_yields import collect_year, response, payload, raw_row
    import requests
    def forbidden(*args,**kwargs):raise AssertionError('WACC must not request provider data')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    db=Database(tmp_path/'rf.sqlite3');db.initialize()
    collect_year(db,[response(payload([
        raw_row('2026-10-07',EMM00166466='1.7'),
        raw_row('2026-10-06',EMM00166466=None),
        raw_row('2026-10-05',EMM00166466='1.678'),
    ]))])
    result=read_risk_free_rate(db,'2026-10-06')
    assert result['value']==1.678 and result['as_of_date']=='2026-10-05'
    assert result['status']=='observed' and not result['stale']
    assert 'sovereign_yield_observations.china_10y' in result['source']
    assert result['source_id'] and result['batch_id'] and result['observation_id']
    collect_year(db,[response(payload([raw_row('2026-10-05',EMM00166466='1.5')]))])
    updated=read_risk_free_rate(db,'2026-10-06')
    assert updated['value']==1.5 and updated['version']==2
    assert updated['observation_id']!=result['observation_id']
    assert read_risk_free_rate(db,'2026-10-07')['value']==1.7
    assert read_risk_free_rate(db,'2026-10-15')['stale']
    assert read_risk_free_rate(db,'2026-10-01')['value'] is None


@pytest.mark.parametrize('value',[0,-0.25])
def test_local_risk_free_preserves_zero_and_negative(tmp_path,value):
    from test_bond_yields import collect_year, response, payload, raw_row
    db=Database(tmp_path/'rf.sqlite3');db.initialize()
    collect_year(db,[response(payload([raw_row('2026-10-05',EMM00166466=value)]))])
    assert read_risk_free_rate(db,'2026-10-06')['value']==value


def test_local_risk_free_empty_invalid_and_no_future_values(tmp_path):
    db=Database(tmp_path/'rf.sqlite3');db.initialize()
    for as_of in ['2026-10-06','invalid',None]:
        result=read_risk_free_rate(db,as_of)
        assert result['value'] is None and result['status']=='unavailable'
        assert result['reason']


def test_interest_bearing_debt_sum_reports_partial_fields_without_zero_filling():
    fields=('SHORTTERMBORR','SHORTTERMBDSPAYA','DUENONCLIAB','LONGBORR','BDSPAYA','LEASELIAB')
    raw={'data':[{'item_field':key,'item_value':value} for key,value in zip(fields,[10,2,3,20,4,1])]
         +[{'item_field':'TOTLIAB','item_value':999}]}
    result=sum_interest_bearing_debt(raw)
    assert result['value']==40
    assert result['missing']==[]
    partial=sum_interest_bearing_debt({'data':raw['data'][:-2]})
    assert partial['value']==39
    assert partial['complete'] is False
    assert partial['missing']==['LEASELIAB']
    assert sum_interest_bearing_debt({'data':[]})['value'] is None


def test_auto_wacc_uses_annual_interest_tax_latest_interim_debt_and_same_day_market_cap(facts,monkeypatch):
    import json
    from stock_analysis.domains.valuation import dcf
    six=('SHORTTERMBORR','SHORTTERMBDSPAYA','DUENONCLIAB','LONGBORR','BDSPAYA','LEASELIAB')
    def balance(values): return raw('fzb',list(zip(six,six,values)))
    identity=facts.ensure_instrument('600519','测试企业')
    def put_report(conn,kind,period,document):
        run_id=conn.execute('SELECT id FROM sync_runs WHERE instrument_id=? AND dataset=? ORDER BY id DESC LIMIT 1',(identity,'financial:'+kind)).fetchone()[0]
        payload=json.dumps(document)
        existing=conn.execute('SELECT 1 FROM financial_reports WHERE instrument_id=? AND source=? AND report_type=? AND period=?',(identity,SOURCE,kind,period)).fetchone()
        if existing:
            conn.execute('UPDATE financial_reports SET raw_json=?,obtained_at=? WHERE instrument_id=? AND source=? AND report_type=? AND period=?',(payload,'2026-10-15',identity,SOURCE,kind,period))
        else:
            conn.execute('INSERT INTO financial_reports(instrument_id,source,report_type,period,raw_json,content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?)',(identity,SOURCE,kind,period,payload,'test-'+period,'2026-10-15',run_id))
    with facts.connection(write=True) as conn:
        put_report(conn,'fzb','2024-12-31',balance([10,None,0,20,5,1]))
        put_report(conn,'fzb','2025-12-31',balance([12,None,0,25,7,1]))
        put_report(conn,'fzb','2026-06-30',balance([15,None,0,30,10,5]))
        put_report(conn,'lrb','2025-12-31',raw('lrb',[
            ('INTERESTEXPENSE','利息费用',8),('INCOTAXEXPE','所得税费用',20),('TOTPROFIT','利润总额',100)]))
    with facts.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_batches(id,legacy_operation_ref,domain,dataset,source_system,trigger_type,status,started_at) VALUES (1,'wacc','market','snapshot','test','manual','complete','2026-06-30')")
        conn.execute("INSERT INTO collection_sources(id,batch_id,source_system,dataset,request_key,attempt_no,request_json) VALUES (1,1,'test','snapshot','wacc',1,'{}')")
        conn.execute("INSERT INTO market_trade_snapshot(exchange,trade_date,version,is_latest,definition_version,batch_id,source_id,content_hash,obtained_at,updated_at,security_code,total_market_cap) VALUES ('sh','2026-06-30',1,1,'em-valueanalysis-v1',1,1,'hash','2026-06-30','2026-06-30','600519',900)")
        conn.execute("INSERT INTO market_trade_snapshot(exchange,trade_date,version,is_latest,definition_version,batch_id,source_id,content_hash,obtained_at,updated_at,security_code,total_market_cap) VALUES ('sh','2026-09-30',1,1,'em-valueanalysis-v1',1,1,'hash-q3','2026-09-30','2026-09-30','600519',1200)")
    result=dcf.build_auto_wacc(facts,'600519',as_of='2026-10-15')
    params=result['parameters']
    assert params['kd']['value']==pytest.approx(8/((36+45)/2)*100)
    assert params['kd']['partial'] is True
    assert params['tax']['history_value']==20
    assert params['tax']['value']==20
    assert params['tax']['status']=='estimated'
    assert params['weight']['value']==pytest.approx(60/960*100)
    assert params['weight']['as_of_date']=='2026-06-30'
    assert '同季度不晚于报告期末的最后交易日 2026-06-30' in params['weight']['method']
    assert params['weight']['partial'] is True
    assert any('部分估算' in warning for warning in result['warnings'])


def test_cost_of_debt_and_effective_tax_reject_invalid_denominators_and_extremes():
    assert calculate_debt_cost(10,90,110)==pytest.approx(10)
    assert calculate_debt_cost(10,0,0) is None
    assert calculate_debt_cost(100,1,1) is None
    assert calculate_effective_tax(25,100)==25
    assert calculate_effective_tax(25,0) is None
    assert calculate_effective_tax(25,-100) is None


def save(db, kind, records):
    identity = db.ensure_instrument('600519', '测试企业')
    key = SyncKey(identity,'financial:'+kind,SOURCE)
    run = db.start_sync(key,parser_version='test',methodology_version='test',trigger_reason='test')
    rows = [{'period':p,'raw_json':r} for p,r in records.items()]
    db.complete_sync(run,SyncResult(len(rows),min(records),max(records),max(records)),lambda conn:db.upsert_financial_reports(conn,key,run,rows))


def raw(kind, pairs, currency='CNY'):
    return dict(rCurrency=currency,rType='合并期末',data=[dict(item_field=code,item_source=kind,item_title=title,item_value=v) for code,title,v in pairs])


@pytest.fixture
def facts(tmp_path):
    db=Database(tmp_path/'dcf.sqlite3');db.initialize()
    income={};cash={};balance={}
    for year in (2021,2022,2023,2024,2025):
        period=f'{year}-12-31'
        income[period]=raw('lrb',[('BIZINCO','营业收入',1000),('PARENETP','归属于母公司股东的净利润',100)])
        cash[period]=raw('llb',[('MANANETR','经营活动产生的现金流量净额',200),('ACQUASSETCASH','购建固定资产、无形资产和其他长期资产支付的现金',50)])
        balance[period]=raw('fzb',[('SHARECAP','股本',100)])
    for kind,records in [('lrb',income),('llb',cash),('fzb',balance)]:save(db,kind,records)
    return db


def test_read_only_complete_windows_and_provenance(facts,tmp_path):
    before=facts.path.stat().st_mtime_ns
    payload=read_dcf(facts,'600519',tmp_path)
    assert payload['bases']['3']['value']==150
    assert payload['bases']['5']['value']==150
    assert payload['market']['total_shares'] is None  # monetary capital is not verified shares
    assert payload['professional']['available'] is False
    source=payload['history'][-1]['sources']['operating_cash_flow_ytd']
    assert source['field']=='MANANETR' and source['currency']=='CNY'
    assert source['provenance']['content_hash']
    assert facts.path.stat().st_mtime_ns==before


def test_missing_annual_fcf_not_skipped(facts,tmp_path):
    with facts.connection(write=True) as conn:
        conn.execute("DELETE FROM financial_reports WHERE report_type='llb' AND period='2024-12-31'")
    payload=read_dcf(facts,'600519',tmp_path)
    assert payload['bases']['1']['value']==150
    assert payload['bases']['3']['value'] is None
    assert payload['bases']['5']['value'] is None


def test_foreign_currency_not_valued(facts,tmp_path):
    import json
    foreign=raw('llb',[('MANANETR','经营活动产生的现金流量净额',200),('ACQUASSETCASH','购建固定资产、无形资产和其他长期资产支付的现金',50)],currency='USD')
    with facts.connection(write=True) as conn:conn.execute("UPDATE financial_reports SET raw_json=? WHERE report_type='llb' AND period='2025-12-31'",(json.dumps(foreign),))
    payload=read_dcf(facts,'600519',tmp_path)
    assert payload['bases']['1']['value'] is None


def test_growth_does_not_use_outlier_endpoint_cagr():
    history=[dict(period=f'{y}-12-31',revenue=v,profit=v,free_cash_flow=v) for y,v in zip(range(2021,2026),[100,103,106,109,10000])]
    recommendation=_growth_recommendation(history)
    assert recommendation['value_pct'] <= 3
    assert recommendation['metrics'][0]['sample_count']==4


def test_financial_company_blocked(facts,tmp_path):
    with facts.connection(write=True) as conn:conn.execute("UPDATE instruments SET name='测试银行' WHERE code='600519'")
    assert read_dcf(facts,'600519',tmp_path)['applicability']['blocked']


def test_override_retained_and_documented(facts,tmp_path):
    with facts.connection(write=True) as conn:
        identity=conn.execute("SELECT id FROM instruments WHERE code='600519'").fetchone()[0]
        conn.execute("INSERT INTO report_overrides(instrument_id,period,field_name,value_json,origin,updated_at) VALUES (?,?,?,?,?,?)",(identity,'2025-12-31','operating_cash_flow_ytd','300','manual','2026-01-01'))
    payload=read_dcf(facts,'600519',tmp_path)
    assert payload['bases']['1']['value']==250
    assert payload['history'][-1]['sources']['operating_cash_flow_ytd']['override']


def test_extreme_share_count_rejects_overflow():
    with pytest.raises(ValueError):calculate_dcf(100,3,9,2,shares=1e-320,price=1)


def test_market_definition_preference_missing_shares_and_newer_price(facts,tmp_path,monkeypatch):
    from stock_analysis.domains.stocks.price_service import PriceService
    monkeypatch.setattr(PriceService,'read',lambda *a,**k:[dict(date='2026-08-31',close=90)])
    with facts.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_batches(id,legacy_operation_ref,domain,dataset,source_system,trigger_type,status,started_at) VALUES (1,'test','market','snapshot','test','manual','complete','2026-01-01')")
        conn.execute("INSERT INTO collection_sources(id,batch_id,source_system,dataset,request_key,attempt_no,request_json) VALUES (1,1,'test','snapshot','test',1,'{}')")
        def snapshot(day,definition,shares,price):
            conn.execute("INSERT INTO market_trade_snapshot(exchange,trade_date,version,is_latest,definition_version,batch_id,source_id,content_hash,obtained_at,updated_at,security_code,total_shares,close_price) VALUES ('sh',?,1,1,?,1,1,'hash','2026-01-01','2026-01-01','600519',?,?)",(day,definition,shares,price))
        snapshot('2026-09-30','historical-composite-v1',100,11)
        snapshot('2026-09-30','em-valueanalysis-v1',200,12)
        snapshot('2026-10-01','em-valueanalysis-v1',None,13)
        snapshot('2099-01-01','em-valueanalysis-v1',999,999)
    p=read_dcf(facts,'600519',tmp_path)
    assert p['market']['total_shares']==200
    assert p['market']['shares_date']=='2026-09-30'
    assert p['market']['price']==13
    assert p['market']['price_date']=='2026-10-01'
    assert any('缺少有效总股数' in w for w in p['warnings'])
