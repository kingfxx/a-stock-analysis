import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest

from quarterly_dashboard import server
from quarterly_dashboard.fundamental_service import SOURCE
from quarterly_dashboard.statements import read_statements
from quarterly_dashboard.storage import Database, SyncKey, SyncResult


def raw(kind, values, **meta):
    return {'rCurrency':'CNY','rType':'合并期末','data':[
        {'item_field':code,'item_source':kind,'item_title':code,'item_value':value}
        for code,value in values.items()], **meta}


def save(db, kind, records, code='600519', name='测试企业'):
    identity = db.ensure_instrument(code, name)
    key = SyncKey(identity,'financial:'+kind,SOURCE)
    run = db.start_sync(key, parser_version='test',methodology_version='test',trigger_reason='test')
    rows = [{'period':period,'raw_json':record} for period,record in sorted(records.items())]
    db.complete_sync(run,SyncResult(len(rows),rows[0]['period'],rows[-1]['period'],rows[-1]['period']),
                     lambda conn:db.upsert_financial_reports(conn,key,run,rows))


@pytest.fixture
def facts(tmp_path):
    db=Database(tmp_path/'statements.sqlite3');db.initialize()
    profit={'BIZINCO':200,'BIZTOTINCO':210,'BIZCOST':100,'BIZTOTCOST':150,
            'PERPROFIT':65,'TOTPROFIT':64,'INCOTAXEXPE':14,'NETPROFIT':50,
            'PARENETP':45,'MINYSHARRIGH':5,'BASICEPS':1.5,'FINEXPE':-2}
    balance={'TOTASSET':500,'TOTLIAB':200,'RIGHAGGR':300,'CURFDS':140,
             'ACCORECE':30,'INVE':40,'SHORTTERMBORR':0,'SHORTTERMBDSPAYA':0,
             'DUENONCLIAB':0,'LONGBORR':25,'BDSPAYA':0,'LEASELIAB':0}
    cash={'INICASHBALA':100,'FINALCASHBALA':140,'MANANETR':60,'INVNETCASHFLOW':-20,
          'FINNETCFLOW':0,'CHGEXCHGCHGS':0,'CASHNETR':40,'ACQUASSETCASH':15}
    for kind,values in [('lrb',profit),('fzb',balance),('llb',cash)]:
        prior={k:v/2 for k,v in values.items()}
        if kind=='llb':prior.update(INICASHBALA=100,FINALCASHBALA=120)
        save(db,kind,{'2025-06-30':raw(kind,values),'2025-03-31':raw(kind,prior),
                      '2024-06-30':raw(kind,prior),'2024-12-31':raw(kind,prior)})
    return db


def test_balance_items_use_the_correct_side_total(facts):
    section=read_statements(facts,'600519',period='2025-06-30')['sections']['fzb']
    rows={row['field']:row for row in section['full_items']}
    assert rows['CURFDS']['balance_side']=='assets'
    assert rows['LONGBORR']['balance_side']=='liabilities'
    assert rows['RIGHAGGR']['balance_side']=='equity'


def test_priority_is_exact_and_falls_back_per_field(facts):
    gjzb=raw('lrb',{'NETPROFIT':'52','BIZTOTCOST':'150','BIZINCO':'','PARENETP':'0'})
    gjzb['data'].append({'item_field':'NPCUT','item_source':'ysb','item_title':'扣非净利润','item_value':'42'})
    save(facts,'gjzb',{'2025-06-30':gjzb})
    data=read_statements(facts,'600519',period='2025-06-30')
    values=data['values']
    assert values['net_profit']['value']==52 and values['net_profit']['source']=='gjzb'
    assert values['parent_profit']['value']==0
    assert values['revenue']['value']==200 and values['revenue']['source']=='lrb'
    assert values['cost']['value']==100 and values['cost']['field']=='BIZCOST'
    assert values['gross_margin']['value']==50
    assert values['deducted_profit']['value']==42


def test_quarter_cash_opening_is_prior_closing_not_difference(facts):
    data=read_statements(facts,'600519',period='2025-06-30',mode='quarter')
    v=data['values']
    assert v['cash_start']['value']==120
    assert v['cash_end']['value']==140
    assert v['cfo']['value']==30
    assert v['revenue']['value']==100
    assert v['assets']['value']==500
    assert v['gross_margin']['value']==50
    assert next(x for x in data['sections']['lrb']['full_items'] if x['field']=='BASICEPS')['value'] is None
    assert v['cfo']['baseline_period']=='2025-03-31'


def test_quarter_fallback_is_per_stock_and_period(facts):
    save(facts,'gjzb',{'2025-06-30':raw('lrb',{'NETPROFIT':52})})
    d=read_statements(facts,'600519',period='2025-06-30',mode='quarter')
    value=d['values']['net_profit']
    assert value['value']==27
    assert value['source']=='gjzb' and value['baseline_source']=='lrb'
    assert [x['period'] for x in value['inputs']]==['2025-06-30','2025-03-31']


def test_missing_previous_and_invalid_zero_are_not_fabricated(facts):
    with facts.connection(write=True) as c:
        c.execute("DELETE FROM financial_reports WHERE period='2025-03-31' AND report_type='llb'")
    d=read_statements(facts,'600519',period='2025-06-30',mode='quarter')
    assert d['values']['cfo']['value'] is None
    assert d['values']['cash_start']['value'] is None
    assert d['values']['cash_end']['value']==140
    assert d['values']['debt']['value']==25
    with facts.connection(write=True) as c:
        row=c.execute("SELECT raw_json FROM financial_reports WHERE report_type='fzb' AND period='2025-06-30'").fetchone()
        body=json.loads(row[0]);next(x for x in body['data'] if x['item_field']=='BDSPAYA')['item_value']=None
        c.execute("UPDATE financial_reports SET raw_json=? WHERE report_type='fzb' AND period='2025-06-30'",(json.dumps(body),))
    assert read_statements(facts,'600519',period='2025-06-30')['values']['debt']['value'] is None


def test_year_end_comparison_applies_only_to_balance(facts):
    d=read_statements(facts,'600519',period='2025-06-30',comparison='year_end')
    assert d['sections']['fzb']['baseline']=='2024-12-31'
    assert d['sections']['lrb']['baseline']=='2024-06-30'
    assert d['sections']['llb']['baseline']=='2024-06-30'


def test_currency_scope_and_nonpositive_profit(facts):
    save(facts,'gjzb',{'2025-06-30':raw('lrb',{'NETPROFIT':999},rCurrency='USD')})
    d=read_statements(facts,'600519',period='2025-06-30')
    assert d['values']['net_profit']['value']==50
    with facts.connection(write=True) as c:
        c.execute("UPDATE financial_reports SET raw_json=? WHERE report_type='lrb' AND period='2025-06-30'",
                  (json.dumps(raw('lrb',{'NETPROFIT':-1})),))
    d=read_statements(facts,'600519',period='2025-06-30')
    assert d['values']['cash_profit']['value'] is None


def test_financial_template_and_bank_aliases(facts):
    save(facts,'lrb',{'2025-06-30':raw('lrb',{'BIZINCO':100,'OPERPROFIT':40,'TOTPROFIT':40,
         'INCOTAX':10,'NETPROFIT':30,'NETPARECOMPPROF':29})},'600036','招商银行')
    save(facts,'llb',{'2025-06-30':raw('llb',{'CASHEQUIOPENBALA':100,'CASHEQUFINBALA':110,
         'MANANETR':20,'INVNETCASHFLOW':-10,'FINNETCFLOW':0,'EXCHCHGCASHEFFE':0})},'600036','招商银行')
    d=read_statements(facts,'600036')
    assert d['financial_company']
    assert d['values']['gross_margin']['value'] is None
    assert d['values']['cash_after_capex']['value'] is None
    assert all(x['value'] is not None for x in d['charts']['lrb'])
    assert d['values']['cash_start']['value']==100


def test_q1_has_no_required_prior_and_read_does_not_write(facts):
    before=facts.path.read_bytes()
    d=read_statements(facts,'600519',period='2025-03-31',mode='quarter')
    assert d['values']['revenue']['value']==100
    assert facts.path.read_bytes()==before
    assert read_statements(facts,'000001')['periods']==[]


@pytest.mark.parametrize('kwargs',[{'period':'2025-02-28'},{'period':'2026-06-30'},{'mode':'ttm'},{'comparison':'invalid'}])
def test_reject_invalid_options(facts,kwargs):
    with pytest.raises(ValueError):read_statements(facts,'600519',**kwargs)


def test_api_is_read_only_and_static_assets_are_served(facts,monkeypatch):
    monkeypatch.setattr(server,'DATABASE_PATH',facts.path)
    monkeypatch.setattr(server,'load_chart_data',lambda *a,**k:pytest.fail('source refresh called'))
    httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    thread=Thread(target=httpd.serve_forever,daemon=True);thread.start()
    try:
        base=f'http://127.0.0.1:{httpd.server_port}'
        with urlopen(base+'/api/statements?code=600519&period=2025-06-30&mode=quarter') as response:
            payload=json.load(response)
        assert payload['values']['cash_start']['value']==120
        for file in ['financial-statements.js','financial-statements.css']:
            with urlopen(base+'/'+file) as response:assert response.status==200
    finally:httpd.shutdown();httpd.server_close();thread.join(timeout=2)


def test_frontend_formatting_handles_zero_units_and_escaping():
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    result=subprocess.run([node,str(Path(__file__).with_name('statements_ui.cjs'))],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr


def test_financial_tabs_in_real_browser(facts,tmp_path,monkeypatch):
    edge=shutil.which('msedge') or r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    node=shutil.which('node')
    if not Path(edge).is_file() or not node:pytest.skip('Edge and Node are needed')
    monkeypatch.setattr(server,'DATABASE_PATH',facts.path)
    def page(code,refresh):
        payload={'code':code,'name':'测试企业','views':{},'warnings':[],'valuation':{'views':{}},
                 'loading':{},'price_version':1,'price_needs_update':False,'cached_stocks':[]}
        return server.TEMPLATE.replace('__PAYLOAD__',json.dumps(payload,ensure_ascii=False)).replace('__CODE__',code).replace('__ERROR__','')
    monkeypatch.setattr(server,'render_page',page)
    httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    thread=Thread(target=httpd.serve_forever,daemon=True);thread.start()
    profile=tmp_path/'browser';browser=None
    try:
        with (tmp_path/'edge.log').open('w',encoding='utf-8') as log:
            browser=subprocess.Popen([edge,'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
                '--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'],stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            endpoint=profile/'DevToolsActivePort';deadline=time.monotonic()+15
            while not endpoint.exists():
                if browser.poll() is not None or time.monotonic()>deadline:pytest.fail('Headless browser failed to start')
                time.sleep(.05)
            result=subprocess.run([node,str(Path(__file__).with_name('statements_browser.cjs')),endpoint.read_text().splitlines()[0],
                f'http://127.0.0.1:{httpd.server_port}',str(tmp_path/'financial-desktop.png'),str(tmp_path/'financial-mobile.png')],
                capture_output=True,text=True,encoding='utf-8',timeout=45)
            assert result.returncode==0,result.stdout+'\n'+result.stderr
    finally:
        if browser and browser.poll() is None:
            browser.terminate()
            try:browser.wait(timeout=5)
            except subprocess.TimeoutExpired:browser.kill();browser.wait(timeout=5)
        httpd.shutdown();httpd.server_close();thread.join(timeout=2)
