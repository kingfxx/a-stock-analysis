"""Real-browser interaction over synthetic, intercepted local facts; no server."""
import copy
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def test_three_year_controls_scenarios_storage_and_switching():
    reports=[]
    for year in range(2021,2026):
        reports += [dict(period=f'{year}-12-31',profit=1e10,fcf=6e9),dict(period=f'{year}-06-30',profit=4e9)]
    reports.append(dict(period='2026-06-30',profit=4e9,fcf=1e9))
    payload=dict(code='601919',name='合成周期企业',years=[2026,2027,2028],as_of_date='2026-10-21',calculated_at='2026-10-21',methodology_version='test',
                 market=dict(price=10,price_date='2026-10-21',shares=1e10,shares_date='2026-09-30'),reports=reports,
                 events=[dict(id='event1',report_period='2025-12-31',proposal_date='2026-03-01',notice_date='2026-06-01',ex_date='2026-06-10',state='paid',cash=5e9,dps=.5,shares=1e10)] +
                        [dict(id=f'event{y}',report_period=f'{y}-12-31',ex_date=f'{y+1}-06-10',state='paid',cash=5e9,dps=.5,shares=1e10) for y in (2023,2024)],
                 history=[dict(year=y) for y in range(2021,2027)],sustainability=dict(financial_company=False,period=None,cash=None,debt=None,net_cash=None,missing_debt=[]),warnings=[])
    html='<button data-valuation-metric="dividend_yield" class="active">股息率</button><script id="payload" type="application/json">{"code":"601919"}</script><details id="dividend-forecast" hidden><summary>未来三年股息率预测计算器</summary><div id="dividend-forecast-body"></div></details><script src="/dividend-forecast.js"></script><link rel="stylesheet" href="/dividend-forecast.css">'
    requests=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page(viewport=dict(width=1280,height=960))
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            def route(r):
                url=r.request.url
                requests.append(url)
                if '/api/dividend-forecast' in url:
                    data=copy.deepcopy(payload)
                    if '600900' in url:data.update(code='600900',name='合成稳定企业')
                    r.fulfill(json=data)
                elif url.endswith('.js'):r.fulfill(body=(ROOT/'web/dividend-forecast.js').read_text(encoding='utf-8'),content_type='text/javascript; charset=utf-8')
                elif url.endswith('.css'):r.fulfill(body=(ROOT/'web/dividend-forecast.css').read_text(),content_type='text/css; charset=utf-8')
                else:r.fulfill(body=html,content_type='text/html; charset=utf-8')
            page.route('**/*',route)
            page.goto('http://forecast.test/')
            try:
                page.locator('#df-price').wait_for(state='attached',timeout=5000)
            except Exception:
                print('UI errors',errors,'body',page.locator('body').inner_text(),'requests',requests)
                raise
            page.locator('#dividend-forecast > summary').click()
            assert page.locator('#df-results').inner_text().count('三财年累计DPS')==3
            def field(s,i,name):return page.locator(f'[data-s="{s}"][data-i="{i}"][data-field="{name}"]')
            assert '50.00%' in page.locator('#df-ratio-source').inner_text()
            assert '2023—2025' in page.locator('#df-ratio-source').inner_text()
            assert page.locator('[data-scenario]').count()==1
            assert page.locator('[data-scenario="pessimistic"]').count()==0
            assert page.locator('[data-scenario="optimistic"]').count()==0
            for s in ('neutral',):
                for i in range(3):
                    for name,expected in [('profit',100),('cash',50),('payout_ratio',50),('shares',100),('growth_rate',0)]:
                        assert float(field(s,i,name).input_value())==expected
                        assert 'df-auto-input' in field(s,i,name).get_attribute('class')
            pess=page.locator('[data-factor-s="pessimistic"][data-factor="profit_factor"]')
            opt=page.locator('[data-factor-s="optimistic"][data-factor="profit_factor"]')
            assert pess.input_value()=='0.8' and opt.input_value()=='1.2'
            def result(s,i,column):
                return page.locator('#df-results table').nth({'pessimistic':0,'neutral':1,'optimistic':2}[s]).locator('tbody tr').nth(i).locator('td').nth(column).inner_text()
            assert result('pessimistic',0,1)=='80.00'
            assert result('optimistic',0,1)=='120.00'
            assert '1.20' in page.locator('#df-sustainability').inner_text()
            assert page.evaluate('localStorage.length')==0  # Displayed defaults never become manual overrides.
            # Automatic cells stay linked; editing a dividend ratio updates cash,
            # and deleting an override restores the formula without persisting it.
            field('neutral',0,'payout_ratio').fill('60')
            assert field('neutral',0,'cash').input_value()=='60'
            field('neutral',0,'payout_ratio').fill('')
            page.locator('#df-price').focus()
            assert field('neutral',0,'payout_ratio').input_value()=='50'
            assert field('neutral',0,'cash').input_value()=='50'
            stored=page.evaluate("JSON.parse(localStorage.getItem('dividend-forecast:v1:601919:2026')).parameters")
            assert stored['scenarios']['neutral']['rows'][0]=={}
            assert 'price' not in stored
            # One input supplies all three years, while preserving their original
            # settings so clearing can restore individual overrides/defaults.
            field('neutral',1,'payout_ratio').fill('55')
            uniform=page.locator('#df-uniform-payout')
            assert uniform.input_value()==''
            uniform.fill('60')
            for i in range(3):
                assert field('neutral',i,'payout_ratio').input_value()=='60'
                assert field('neutral',i,'payout_ratio').is_disabled()
                assert field('neutral',i,'cash').input_value()=='60'
                assert result('pessimistic',i,4)=='48.00'
                assert result('optimistic',i,4)=='72.00'
            field('neutral',0,'cash').fill('80')
            assert result('neutral',0,3)=='80.00%'
            assert result('pessimistic',0,4)=='64.00'
            field('neutral',0,'cash').fill('');page.locator('#df-price').focus()
            assert field('neutral',0,'cash').input_value()=='60'
            page.reload()
            page.locator('#df-price').wait_for(state='attached')
            page.locator('#dividend-forecast > summary').click()
            assert uniform.input_value()=='60'
            assert field('neutral',2,'payout_ratio').input_value()=='60'
            uniform.fill('')
            page.locator('#df-price').focus()
            assert uniform.input_value()==''
            assert [field('neutral',i,'payout_ratio').input_value() for i in range(3)]==['50','55','50']
            assert all(field('neutral',i,'payout_ratio').is_enabled() for i in range(3))
            restored=page.evaluate("JSON.parse(localStorage.getItem('dividend-forecast:v1:601919:2026')).parameters.scenarios.neutral")
            assert 'uniform_payout_ratio' not in restored
            field('neutral',1,'payout_ratio').fill('');page.locator('#df-price').focus()
            uniform.fill('0')
            assert all(field('neutral',i,'cash').input_value()=='0' for i in range(3))
            uniform.fill('-1')
            assert '分红不可用' in result('neutral',0,8)
            uniform.fill('');page.locator('#df-price').focus()
            initial_requests=len([r for r in requests if '/api/' in r])
            field('neutral',0,'growth_rate').fill('10')
            field('neutral',1,'growth_rate').fill('20')
            field('neutral',2,'growth_rate').fill('-10')
            for i in range(3):field('neutral',i,'payout_ratio').fill('50')
            assert page.locator('[data-s="neutral"][data-config="mode"]').input_value()=='C'
            assert result('pessimistic',1,1)=='105.60'
            assert result('optimistic',1,1)=='158.40'
            pess.fill('.7')
            assert result('pessimistic',1,1)=='92.40'
            assert result('optimistic',1,1)=='158.40'
            opt.fill('1.3')
            assert result('optimistic',1,1)=='171.60'
            neutral=page.locator('#df-results table').nth(1)
            assert neutral.locator('tbody tr').nth(0).locator('td').nth(1).inner_text()=='110.00'
            assert neutral.locator('tbody tr').nth(1).locator('td').nth(1).inner_text()=='132.00'
            assert neutral.locator('tbody tr').nth(2).locator('td').nth(1).inner_text()=='118.80'
            assert neutral.locator('tbody tr').nth(0).locator('td').nth(7).inner_text()=='5.50%'
            assert '13.75' in page.locator('#df-target').inner_text()
            assert len([r for r in requests if '/api/' in r])==initial_requests
            field('neutral',0,'profit').fill('200')
            assert neutral.locator('tbody tr').nth(1).locator('td').nth(1).inner_text()=='240.00'
            assert 'df-manual-input' in field('neutral',0,'profit').get_attribute('class')
            assert result('pessimistic',0,1)=='140.00'
            assert result('optimistic',0,1)=='260.00'
            page.reload()
            page.locator('#df-price').wait_for(state='attached')
            page.locator('#dividend-forecast > summary').click()
            assert field('neutral',1,'growth_rate').input_value()=='20'
            assert field('neutral',0,'profit').input_value()=='200'
            assert pess.input_value()=='0.7' and opt.input_value()=='1.3'
            assert result('pessimistic',1,1)=='168.00'
            page.locator('#df-reset').click()
            assert field('neutral',0,'profit').input_value()=='100'
            assert page.locator('[data-s="neutral"][data-config="mode"]').input_value()=='A'
            assert pess.input_value()=='0.8' and opt.input_value()=='1.2'
            # Coefficient reset preserves neutral parameters and all linked values.
            field('neutral',0,'profit').fill('200')
            pess.fill('.6')
            page.locator('#df-reset-factors').click()
            assert field('neutral',0,'profit').input_value()=='200'
            assert result('pessimistic',0,1)=='160.00'
            field('neutral',0,'profit').fill('');page.locator('#df-price').focus()
            assert result('neutral',0,1)=='100.00'
            # Completeness notes are optional; known dividends already seed the forecast.
            page.locator('#df-events').locator('..').locator('summary').click()
            page.locator('[data-year="2025"][data-confirm="note"]').fill('合成财报完整性证据')
            page.locator('[data-year="2025"][data-confirm="complete"]').check()
            assert '50.00%' in page.locator('#df-ratio-source').inner_text()
            page.locator('#df-payout').select_option('1')
            page.locator('#df-exclude').check()
            assert '50.00%' in page.locator('#df-ratio-source').inner_text()
            page.locator('[data-event="event1"][data-edit="note"]').fill('合成普通年度分红公告')
            page.locator('[data-event="event1"][data-edit="kind"]').select_option('regular')
            assert '50.00%' in page.locator('#df-ratio-source').inner_text()
            page.locator('[data-event="event1"][data-edit="kind"]').select_option('special')
            assert '0.00%' in page.locator('#df-ratio-source').inner_text()
            # Hide, refresh facts, then return: re-read and preserve hand edits.
            field('neutral',1,'growth_rate').fill('12')
            page.evaluate("window.dispatchEvent(new CustomEvent('dividend-forecast-context',{detail:{code:'601919',metric:'pe'}}))")
            assert page.locator('#dividend-forecast').is_hidden()
            page.evaluate("window.dispatchEvent(new CustomEvent('dividend-forecast-facts',{detail:{code:'601919'}}))")
            with page.expect_response('**/api/dividend-forecast?code=601919'):
                page.evaluate("window.dispatchEvent(new CustomEvent('dividend-forecast-context',{detail:{code:'601919',metric:'dividend_yield'}}))")
            assert field('neutral',1,'growth_rate').input_value()=='12'
            # A new forecast year resets rates, retaining only matching event notes.
            payload['years']=[2027,2028,2029]
            payload['as_of_date']='2027-01-01'
            payload['history']=[dict(year=y) for y in range(2022,2028)]
            with page.expect_response('**/api/dividend-forecast?code=601919'):
                page.locator('#df-reload').click()
            page.wait_for_function("""document.querySelector('[data-s="neutral"][data-i="1"][data-field="growth_rate"]')?.value==='0' """)
            assert page.locator('[data-event="event1"][data-edit="kind"]').input_value()=='special'
            with page.expect_response('**/api/dividend-forecast?code=600900'):
                page.evaluate("window.dispatchEvent(new CustomEvent('dividend-forecast-context',{detail:{code:'600900',metric:'dividend_yield'}}))")
            page.wait_for_function("document.querySelector('#df-status')?.textContent.includes('本地读取时间')")
            assert field('neutral',1,'growth_rate').input_value()=='0'
            page.set_viewport_size(dict(width=390,height=844))
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert not errors,errors
        finally:
            browser.close()


def test_old_independent_scenarios_migrate_without_losing_neutral_or_evidence():
    saved=dict(version=1,parameters=dict(window=3,method='median',payoutMode='auto',exclude=False,edits={},confirmations={},
        scenarios=dict(neutral=dict(mode='C',base=1e10,rows=[dict(profit=2e10,payout_ratio=.5),dict(growth_rate=.2,payout_ratio=.5),dict(growth_rate=0,payout_ratio=.5)]),
                       pessimistic=dict(mode='B',rows=[dict(profit=9e10),{},{}]),optimistic=dict(mode='A',rows=[dict(profit=10e10),{},{}]))))
    payload=dict(code='601919',name='合成迁移样例',years=[2026,2027,2028],as_of_date='2026-10-21',calculated_at='2026-10-21',methodology_version='test',
        reports=[],events=[],history=[dict(year=y) for y in range(2021,2027)],market=dict(price=10,shares=1e10,price_date='2026-10-21',shares_date='2026-10-21'),
        warnings=[],sustainability=dict(financial_company=False,period=None,cash=None,debt=None,net_cash=None,missing_debt=[]))
    html='<button data-valuation-metric="dividend_yield" class="active"></button><script id="payload" type="application/json">{"code":"601919"}</script><details id="dividend-forecast" hidden><summary>预测</summary><div id="dividend-forecast-body"></div></details><script src="/dividend-forecast.js"></script>'
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            page=browser.new_page()
            page.add_init_script("localStorage.setItem('dividend-forecast:v1:601919:2026',"+json.dumps(json.dumps(saved))+");")
            def route(r):
                if '/api/' in r.request.url:r.fulfill(json=payload)
                elif r.request.url.endswith('.js'):r.fulfill(body=(ROOT/'web/dividend-forecast.js').read_text(encoding='utf-8'),content_type='text/javascript; charset=utf-8')
                else:r.fulfill(body=html,content_type='text/html; charset=utf-8')
            page.route('**/*',route)
            page.goto('http://forecast.test/')
            page.locator('#df-price').wait_for(state='attached')
            page.locator('#dividend-forecast > summary').click()
            assert '旧版悲观/乐观独立参数已停用' in page.locator('#df-status').inner_text()
            assert page.locator('[data-scenario]').count()==1
            assert page.locator('[data-s="neutral"][data-i="0"][data-field="profit"]').input_value()=='200'
            assert page.locator('#df-results table').nth(0).locator('tbody tr').nth(0).locator('td').nth(1).inner_text()=='160.00'
            page.locator('[data-factor-s="pessimistic"][data-factor="profit_factor"]').fill('.9')
            persisted=page.evaluate("JSON.parse(localStorage.getItem('dividend-forecast:v1:601919:2026')).parameters")
            assert persisted['layout_version']==2
            assert persisted['scenarios']['neutral']['rows'][0]['profit']==2e10
            assert persisted['legacy_scenarios']['pessimistic']['rows'][0]['profit']==9e10
            assert persisted['factors']['pessimistic']['profit_factor']==.9
            assert page.locator('#df-results table').nth(0).locator('tbody tr').nth(0).locator('td').nth(1).inner_text()=='180.00'
        finally:
            browser.close()
