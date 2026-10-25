"""Synthetic boundary cases and read-only isolated-store acceptance."""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from stock_analysis.domains.valuation import dividend_forecast as forecast; from stock_analysis.http import server
from stock_analysis.domains.stocks.dividend_service import DividendService, SOURCE as DIVIDEND_SOURCE
from stock_analysis.domains.financials.fundamental_service import SOURCE
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult
from test_dcf_server import request


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT/'tests/fixtures/dividend_forecast.json').read_text())


def browser(function, args):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for parity')
    script = "const d=require('./web/features/valuation/dividend-forecast.js');process.stdout.write(JSON.stringify(d[process.argv[1]](...JSON.parse(process.argv[2]))));"
    result = subprocess.run([node, '-e', script, function, json.dumps(args)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('case', CASES['cases'], ids=lambda c:c['name'])
def test_forecast_python_js_parity(case):
    args = [CASES['years'], case['parameters'], case['price']]
    assert browser('calculate_scenario', args) == forecast.calculate_scenario(*args)


@pytest.mark.parametrize('case',CASES['cases'],ids=lambda c:c['name'])
@pytest.mark.parametrize('factors',[
    dict(profit_factor=.8,payout_factor=1,shares_factor=1),
    dict(profit_factor=1.2,payout_factor=.9,shares_factor=1.1),
    dict(profit_factor=-.5,payout_factor=-1,shares_factor=0),
])
def test_linked_scenarios_python_js_parity(case,factors):
    args=[CASES['years'],case['parameters'],case['price'],factors]
    assert browser('derive_scenario',args)==forecast.derive_scenario(*args)


def test_linked_scenario_applies_coefficient_once_and_preserves_loss_direction():
    parameters=dict(mode='C',base_profit=100,rows=[dict(growth_rate=g,payout_ratio=.5,shares=100) for g in (.1,.2,-.1)])
    actual=forecast.derive_scenario([2026,2027,2028],parameters,10,dict(profit_factor=.8,payout_factor=1,shares_factor=1))
    assert [r['profit'] for r in actual['rows']]==pytest.approx([88,105.6,95.04])
    assert [r['dps'] for r in actual['rows']]==pytest.approx([.44,.528,.4752])
    parameters['rows'][1]['profit']=-100
    parameters['rows'][1]['cash']=20
    pessimistic=forecast.derive_scenario([2026,2027,2028],parameters,10,dict(profit_factor=.8,payout_factor=1,shares_factor=1))
    optimistic=forecast.derive_scenario([2026,2027,2028],parameters,10,dict(profit_factor=1.2,payout_factor=1,shares_factor=1))
    assert pessimistic['rows'][1]['profit']==-120
    assert optimistic['rows'][1]['profit']==-80
    assert pessimistic['rows'][1]['cash']==16
    assert optimistic['rows'][1]['cash']==24
    # An amount override uses both factors once, without multiplying by profit twice.
    parameters['rows'][0].update(profit=200,cash=100)
    derived=forecast.derive_scenario([2026,2027,2028],parameters,10,dict(profit_factor=.8,payout_factor=.5,shares_factor=2))
    assert derived['rows'][0]['profit']==160 and derived['rows'][0]['cash']==40
    assert derived['rows'][0]['shares']==200 and derived['rows'][0]['dps']==.2


@pytest.mark.parametrize('mode',['A','B','C'])
@pytest.mark.parametrize('method',['median','mean'])
def test_scenario_factor_defaults_match_browser_and_identify_assumptions(mode,method):
    args=[profits(),2026,mode,3,method]
    defaults=forecast.scenario_factors(*args)
    assert defaults==browser('scenario_factors',args)
    assert defaults['pessimistic']['profit_factor']<1
    assert defaults['optimistic']['profit_factor']>1
    for entry in defaults.values():
        assert entry['payout_factor']==entry['shares_factor']==1
        assert entry['source']


def test_scenario_factor_fallback_does_not_claim_historical_forecast():
    args=[[],2026]
    result=forecast.scenario_factors(*args)
    assert result==browser('scenario_factors',args)
    assert result['pessimistic']['profit_factor']==.8
    assert result['optimistic']['profit_factor']==1.2
    assert '情景假设' in result['optimistic']['source']


def test_formula_units_growth_overrides_and_cumulative():
    stable = forecast.calculate_scenario(CASES['years'], CASES['cases'][0]['parameters'], 10)
    assert stable['rows'][0]['cash'] == 5e9
    assert stable['rows'][0]['dps'] == .5
    assert stable['rows'][0]['yield_rate'] == .05
    assert stable['cumulative_dps'] == 1.5 and stable['cumulative_yield'] == .15
    growth = forecast.calculate_scenario(CASES['years'], CASES['cases'][1]['parameters'], 10)
    assert [r['profit'] for r in growth['rows']] == pytest.approx([110, 132, 118.8])
    assert growth['rows'][1]['dps'] == pytest.approx(132*.6/200)
    assert growth['rows'][2]['warnings'] == ['分红超过归母净利润']
    overridden = forecast.calculate_scenario(CASES['years'], CASES['cases'][4]['parameters'], 20)
    assert overridden['rows'][1]['profit'] == 240
    assert overridden['rows'][1]['cash'] == 0
    assert overridden['rows'][2]['profit'] == 0
    assert overridden['rows'][2]['cash'] == 3
    assert browser('target_price', [.5, .04]) == 12.5
    for args in [[None,.04],[.5,0],[.5,-.1],[-1,.04],[0,.04]]:
        assert browser('target_price', args) is None


@pytest.mark.parametrize('value', [None, '', True, float('nan'), float('inf'), -float('inf')])
def test_finite_keeps_invalid_out(value):
    assert forecast.finite(value) is None


def profits(quarter='06-30'):
    rows = []
    for y, profit, interim in [(2023,100,40),(2024,200,80),(2025,-50,10)]:
        rows += [dict(period=f'{y}-12-31',profit=profit), dict(period=f'{y}-{quarter}',profit=interim)]
    return rows+[dict(period=f'2026-{quarter}',profit=30)]


@pytest.mark.parametrize('quarter', ['06-30', '09-30'])
@pytest.mark.parametrize('method', ['median', 'mean'])
@pytest.mark.parametrize('scenario', forecast.SCENARIOS)
def test_cumulative_remaining_and_normalization_parity(quarter, method, scenario):
    args = [profits(quarter),2026,'A',3,method,scenario]
    actual = forecast.profit_basis(*args)
    assert actual == browser('profit_basis', args)
    assert actual['samples'] == [60,120,-60]
    if scenario == 'neutral':
        assert actual['value'] == (70 if method == 'mean' else 90)


def test_missing_window_not_skipped_and_no_current_report():
    rows = profits()
    assert forecast.profit_basis(rows[:-1],2026)['value'] is None
    assert forecast.profit_basis(rows[1:],2026)['value'] is None
    assert forecast.profit_basis(rows,2026,window=5)['value'] is None
    assert forecast.profit_basis(rows,2026,mode='B')['value'] == 100
    rows.append(dict(period='2026-12-31',profit=80))
    assert forecast.profit_basis(rows,2026)['value'] == 80


def event(key, period='2025-12-31', cash=20, dps=.2, state='paid'):
    return dict(id=key,report_period=period,cash=cash,dps=dps,state=state)


def confirmation(year=2025, **kwargs):
    return {str(year):dict(complete=True,note='合成完整性证据',**kwargs)}


def test_history_events_fiscal_matching_special_zero_pending_and_negative_fcf():
    reports = [dict(period='2025-12-31',profit=100,fcf=-10)]
    events = [event('interim','2025-06-30',10,.1),event('annual'),event('special',cash=50,dps=.5)]
    edits = {'interim':dict(kind='regular',note='合成证据'), 'annual':dict(kind='regular',note='合成证据'), 'special':dict(kind='special',note='合成证据')}
    args = [reports,events,2026,edits,confirmation(),True]
    rows = forecast.build_history(*args)
    assert rows == browser('build_history', args)
    row = rows[-2]
    assert row['cash'] == 30 and row['dps'] == pytest.approx(.3)
    assert row['payout_ratio'] == .3 and row['coverage'] == -10/30 and row['complete']
    assert forecast.payout_basis(rows,2026,1) == .3
    assert forecast.payout_basis(rows,2026,3) is None
    assert browser('payout_basis',[rows,2026,1]) == .3
    pending = forecast.build_history(reports,events+[event('pending',state='pending')],2026,edits,confirmation())[-2]
    assert pending['pending'] and not pending['complete'] and pending['cash'] == 30
    unknown = forecast.build_history(reports,events,2026,{},confirmation())[-2]
    assert unknown['unknown'] and not unknown['complete'] and unknown['cash'] == 80
    no_events = forecast.build_history(reports,[],2026)[-2]
    assert no_events['cash'] is None
    zero = forecast.build_history(reports,[],2026,{},confirmation(zero=True))[-2]
    assert zero['cash'] == 0 and zero['payout_ratio'] == 0 and zero['coverage'] is None


def test_cash_override_needs_evidence_and_does_not_readjust_dps():
    reports=[dict(period='2025-12-31',profit=-10,fcf=100)]
    events=[event('annual')]
    for edits,expected in [({'annual':dict(cash=99)},20),({'annual':dict(cash=99,note='公告金额')},99)]:
        args=[reports,events,2026,edits,confirmation(),False]
        rows=forecast.build_history(*args)
        assert rows == browser('build_history',args)
        assert rows[-2]['cash']==expected and rows[-2]['dps']==.2
        assert rows[-2]['payout_ratio'] is None
    assert forecast.build_history(reports,[event('missing',cash=None)],2026)[-2]['cash'] is None
    conflict=event('conflict');conflict['conflict']=True
    assert forecast.build_history(reports,[conflict],2026)[-2]['cash'] is None


def test_three_year_ratio_uses_sum_not_mean_of_ratios():
    history=[dict(year=2023,complete=True,cash=10,profit=100),dict(year=2024,complete=True,cash=90,profit=300),dict(year=2025,complete=True,cash=0,profit=-10)]
    assert forecast.payout_basis(history,2026,3)==100/390
    assert browser('payout_basis',[history,2026,3])==100/390
    history[-1]['profit']=-1000
    assert forecast.payout_basis(history,2026,3) is None
    for row in history:row['profit']=1e308
    assert forecast.payout_basis(history,2026,3) is None
    assert browser('payout_basis',[history,2026,3]) is None


def test_known_dividends_seed_forecast_without_manual_confirmation():
    reports=[dict(period=f'{y}-12-31',profit=p,fcf=100) for y,p in [(2023,100),(2024,300),(2025,200)]]
    events=[event('a','2023-06-30',10,.1),event('b','2023-12-31',20,.2),event('c','2024-12-31',120,1.2),event('d','2025-12-31',90,.9)]
    for exclude in (False,True):
        args=[reports,events,2026,{}, {},exclude]
        history=forecast.build_history(*args)
        assert history==browser('build_history',args)
        assert not any(r['complete'] for r in history)
        assert forecast.payout_basis(history,2026,3)==.4
        assert browser('payout_basis',[history,2026,3])==.4
        parameters=dict(mode='B',first_profit=200,rows=[dict(growth_rate=0,payout_ratio=.4,shares=100) for _ in range(3)])
        assert forecast.calculate_scenario([2026,2027,2028],parameters,10)['cumulative_yield']==pytest.approx(.24)
    for changed in [[*events,event('pending','2025-12-31',state='pending')],events[:-1],[*events,event('missing','2025-06-30',cash=None)]]:
        history=forecast.build_history(reports,changed,2026)
        assert forecast.payout_basis(history,2026,3) is None
        assert browser('payout_basis',[history,2026,3]) is None


@pytest.fixture
def store(tmp_path,monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return cls(2026,10,21,12,tzinfo=tz)
    monkeypatch.setattr(forecast,'datetime',Clock)
    db=Database(tmp_path/'forecast.sqlite3');db.initialize()
    db.ensure_instrument('601919','合成航运企业')
    return db


def save_reports(db,kind,records):
    identity=db.ensure_instrument('601919')
    key=SyncKey(identity,'financial:'+kind,SOURCE)
    run=db.start_sync(key,parser_version='test',methodology_version='test',trigger_reason='test')
    db.complete_sync(run,SyncResult(len(records),min(r['period'] for r in records),max(r['period'] for r in records),max(r['period'] for r in records)),
                     lambda conn:db.upsert_financial_reports(conn,key,run,records))


def report(period,kind,fields,published='2026-08-20',currency='CNY'):
    return dict(period=period,publish_date=published,raw_json=dict(rCurrency=currency,rType='合并期末',data=[dict(item_source=kind,item_field=k,item_title=k,item_value=v) for k,v in fields.items()]))


def save_events(db,source,records):
    key=SyncKey(db.ensure_instrument('601919'),'dividends',source)
    run=db.start_sync(key,parser_version='test',methodology_version='test',trigger_reason='test')
    db.complete_sync(run,SyncResult(len(records),'2025-12-31','2026-06-30','2026-06-30'),lambda conn:db.upsert_dividend_events(conn,key,run,records))


def candidate(proposal='2026-03-20',ex_date='2026-06-26'):
    return DividendService._candidate('601919',dict(SECURITY_CODE='601919',REPORT_DATE='2025-12-31',PLAN_NOTICE_DATE=proposal,NOTICE_DATE='2026-06-18',EX_DIVIDEND_DATE=ex_date,ASSIGN_PROGRESS='实施分配',PRETAX_BONUS_RMB=4,TOTAL_SHARES=100))


def test_reader_gjzb_priority_disclosure_units_and_no_writes(store,monkeypatch):
    save_reports(store,'lrb',[report('2025-12-31','lrb',{'PARENETP':100}),report('2026-06-30','lrb',{'PARENETP':40}),report('2026-09-30','lrb',{'PARENETP':999},published='2026-10-30')])
    # Exact field in gjzb must use its original item_source.
    save_reports(store,'gjzb',[report('2025-12-31','lrb',{'PARENETP':120})])
    save_reports(store,'llb',[report('2025-12-31','llb',{'MANANETR':40,'ACQUASSETCASH':50})])
    save_reports(store,'fzb',[report('2026-06-30','fzb',{'PAIDINCAPI':100,'CURFDS':300,'LONGBORR':10})])
    before=store.path.stat().st_mtime_ns
    def forbidden(*a,**k):raise AssertionError('read-only forecast must not mutate or collect')
    monkeypatch.setattr(Database,'initialize',forbidden)
    monkeypatch.setattr(DividendService,'update',forbidden)
    import requests
    monkeypatch.setattr(requests.Session,'request',forbidden)
    result=forecast.read_dividend_forecast(store,'601919')
    assert result['years']==[2026,2027,2028]
    assert [r['profit'] for r in result['reports']]==[120,40]
    assert result['reports'][0]['fcf']==-10
    assert result['reports'][0]['sources']['profit']['source']=='gjzb'
    assert result['reports'][0]['sources']['profit']['provenance']['content_hash']
    assert result['market']['shares'] is None  # paid-in capital is not a share count
    assert result['sustainability']['debt']==10
    assert result['sustainability']['net_cash'] is None
    assert result['sustainability']['missing_debt']
    assert store.path.stat().st_mtime_ns==before


def test_reader_missing_publish_foreign_currency_and_financial_company(store):
    save_reports(store,'lrb',[report('2024-12-31','lrb',{'PARENETP':100},published=None),report('2025-12-31','lrb',{'PARENETP':100},currency='USD')])
    result=forecast.read_dividend_forecast(store,'601919')
    assert len(result['reports'])==1 and result['reports'][0]['profit'] is None
    with store.connection(write=True) as conn:
        conn.execute("UPDATE instruments SET name='合成银行' WHERE code='601919'")
    assert forecast.read_dividend_forecast(store,'601919')['sustainability']['financial_company']


def test_reader_legacy_dedup_future_exdate_and_identity_conflicts(store):
    paid=candidate()
    future=candidate('2026-08-29','2026-10-23');future['report_period']='2026-06-30'
    save_events(store,DIVIDEND_SOURCE,[paid,future])
    legacy=dict(paid,event_key='legacy-event')
    save_events(store,'legacy',[legacy])
    result=forecast.read_dividend_forecast(store,'601919')
    assert len(result['events'])==2
    assert [e['state'] for e in result['events']]==['paid','pending']
    assert result['events'][0]['cash']==40
    assert result['history'][-2]['cash']==40
    # Another proposal with same report period and ex date cannot safely be summed.
    conflict=candidate('2026-03-21')
    save_events(store,DIVIDEND_SOURCE,[conflict])
    result=forecast.read_dividend_forecast(store,'601919')
    assert result['history'][-2]['cash'] is None
    assert any(w['code']=='event_identity_conflict' for w in result['warnings'])


def test_endpoint_and_assets_are_local_and_no_history_backtest(store,monkeypatch):
    monkeypatch.setattr(server,'DATABASE_PATH',store.path)
    status,headers,body=request('/api/dividend-forecast?code=601919')
    assert status==200 and headers['Cache-Control']=='no-store'
    assert json.loads(body)['methodology_version']==forecast.VERSION
    for query in ['code=bad','code=601919&as_of=2025-01-01']:
        assert request('/api/dividend-forecast?'+query)[0]==400
    for asset in ['dividend-forecast.js','dividend-forecast.css']:
        assert request('/'+asset)[0]==200


def test_absent_stock_and_cross_year(store,monkeypatch):
    result=forecast.read_dividend_forecast(store,'601318')
    assert result['reports']==[] and result['events']==[] and result['market']['price'] is None
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2027,1,1,tzinfo=tz)
    monkeypatch.setattr(forecast,'datetime',Clock)
    assert forecast.read_dividend_forecast(store,'601919')['years']==[2027,2028,2029]
