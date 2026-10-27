"""Explicit UI acceptance against a chosen running loopback backend, no model calls.

python tests/industry_browser.py --port 8765 --output <temporary screenshot folder>
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    errors=[];requests=[];actions=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='msedge',headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1100})
        page=context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:requests.append({'url':r.url,'method':r.method}))
        def guard(route):
            url=route.request.url
            if route.request.method!='GET' or any('/api/'+p+'?' in url for p in ['financial','dividends','valuation','shareholders','financing','prices']):
                route.abort()
            else:
                route.continue_()
        page.route('**/api/**',guard)
        page.add_init_script("sessionStorage.setItem('investment-industry-selection-v1',JSON.stringify({industry:'630701',level:'3'}))")
        page.goto(f'http://127.0.0.1:{args.port}/?code=300750',wait_until='domcontentloaded')
        page.locator('#industry-tab').click()
        page.wait_for_function("document.getElementById('sw-third').value==='630701' && document.getElementById('sw-chart')._fullLayout")
        assert page.locator('#trend-panel').is_hidden()
        assert page.locator('#statements-panel').is_hidden()
        assert page.locator('#latest-period-label').inner_text()=='全市场报告期'
        assert page.locator('#updated-label').inner_text()=='全市场数据更新'
        assert page.locator('#latest-period').inner_text()==page.locator('#sw-period').input_value()
        assert page.locator('#updated').inner_text() in page.locator('#sw-status').inner_text()
        assert page.locator('#sw-path').inner_text().endswith('电力设备 → 电池 → 锂电池')
        history = page.locator('#sw-period option[value="2016-09-30"]').count()==1
        assert page.locator('#sw-ranking tbody tr').count()>2 if history else page.locator('#sw-ranking tbody tr').count()==2
        assert page.locator('#sw-metrics').inner_text().count('27/27')>=3
        assert page.locator('#sw-saved').inner_text().find('300750')>=0
        market_dates = page.evaluate("document.getElementById('sw-chart').data[1].x")
        market_points = len(market_dates)
        if history:
            assert market_points >= 32 and market_dates[0].startswith('2018-')
            assert page.locator('#sw-period option[value="2016-09-30"]').count()==1
        else:
            assert market_dates == ['2025-12-31','2026-03-31','2026-06-30','2026-09-30']
        assert page.evaluate("document.getElementById('sw-chart').data[1].mode")=='lines+markers'
        assert f'{market_points} 个季度点' in page.locator('#sw-chart-note').inner_text()
        assert '2026Q3' in page.locator('#sw-market').inner_text()
        all_market = '97/97' in page.locator('#sw-metrics').inner_text()
        assert page.locator('#sw-refresh-financial,#sw-refresh-cap,#sw-recheck-cap').count()==0
        assert page.locator('#industry-panel a[href="/?tab=maintenance"]').count()==0
        before=sum('/api/industry?' in r['url'] for r in requests)
        page.evaluate("window.dispatchEvent(new CustomEvent('financial-facts-updated',{detail:{code:'300750'}}))")
        page.wait_for_timeout(250)
        assert sum('/api/industry?' in r['url'] for r in requests)==before
        # Individual refresh requests are aborted by the guard: verify their routing without writing daily data.
        page.locator('#refresh').click()
        page.wait_for_function("document.getElementById('refresh').getAttribute('aria-disabled')==='false'")
        assert any('/api/financial?' in r['url'] and 'refresh=1' in r['url'] for r in requests)
        assert sum('/api/industry?' in r['url'] for r in requests)==before
        assert not any('/api/industry/refresh' in r['url'] for r in requests)
        # Business pages are read-only; collection UI is tested in collection management.
        assert not actions
        page.locator('#industry-panel').screenshot(path=str(args.output/'desktop.png'))
        page.select_option('#sw-mode','quarter')
        page.wait_for_function("document.getElementById('sw-chart-note').textContent.includes('单季度')")
        page.select_option('#sw-second','630700')
        page.wait_for_function("document.getElementById('sw-title').textContent==='电力设备 → 电池 · 营收与季度市值'")
        if all_market:
            page.wait_for_function("document.getElementById('sw-metrics').textContent.includes('97/97')")
            page.select_option('#sw-mode','ytd')
            page.wait_for_function("document.getElementById('sw-chart-note').textContent.includes('本年累计')")
            assert page.evaluate("document.getElementById('sw-chart').data[1].x.length")==market_points
        else:
            page.wait_for_function("document.getElementById('sw-metrics').textContent.includes('27/97')")
            assert '已覆盖合计' in page.locator('#sw-metrics').inner_text()
        page.select_option('#sw-first','340000')
        page.wait_for_function("document.getElementById('sw-first').value==='340000' && document.getElementById('sw-second').options.length>1")
        page.select_option('#sw-second','340700')
        page.wait_for_function("document.getElementById('sw-third').options.length>1")
        page.select_option('#sw-third','340702')
        page.wait_for_function("document.getElementById('sw-title').textContent.includes('乳品') && document.getElementById('sw-metrics').textContent.includes('18/18')")
        assert '600887' in page.locator('#sw-saved').inner_text()
        assert '当前股票不属于所选行业' in page.locator('#sw-metrics').inner_text()
        page.locator('#business-tab').click()
        page.wait_for_function("document.getElementById('bs-company').textContent.includes('300750')")
        assert page.locator('#latest-period-label').inner_text()=='主营构成报告期'
        assert page.locator('#updated-label').inner_text()=='主营构成更新'
        business_state=page.evaluate("async()=>await (await fetch('/api/business-segments?code=300750')).json()")
        expected_business_update=page.evaluate("value=>DashboardTime.format(value,'未记录')",business_state['sync'].get('succeeded_at'))
        assert page.locator('#updated').inner_text()==expected_business_update
        page.locator('#trend-tab').click();assert page.locator('#industry-panel').is_hidden()
        assert page.locator('#latest-period-label').inner_text()=='最新报告期'
        assert page.locator('#updated-label').inner_text()=='财报缓存更新'
        page.locator('#industry-tab').click();assert page.locator('#trend-panel').is_hidden()
        assert page.locator('#latest-period-label').inner_text()=='全市场报告期'
        assert page.locator('#updated-label').inner_text()=='全市场数据更新'
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_function("document.getElementById('sw-chart')._fullLayout.width<=document.getElementById('sw-chart').clientWidth+1")
        page.locator('#industry-panel').screenshot(path=str(args.output/'mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        assert not errors,errors
        assert not any(r['method']=='POST' and '/api/analysis' in r['url'] for r in requests)
        browser.close()
    result={'checks':'desktop, mobile, hierarchy, quarter mode, coverage, current-stock comparison, chart, lazy reads, stock refresh isolation',
            'page_errors':errors,'model_calls':0,'industry_requests':sum('/api/industry?' in r['url'] for r in requests),
            'mocked_industry_actions':actions}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
