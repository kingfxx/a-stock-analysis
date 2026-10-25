import pytest

from stock_analysis.domains.notes.investment_notes import InvestmentNotes, NoteConflict
from stock_analysis.domains.notes.note_snapshots import catalog as note_catalog
from stock_analysis.domains.indices.index_repository import publish, catalog
from stock_analysis.domains.indices.index_prices import publish_prices
from test_index_prices import point
from test_index_collection import db, source
from test_index_ui import index_server


def snapshot(db, **options):
    return note_catalog(db, dict(code='index:cn:000300', group='index_valuation', provider='csindex',
                                series='history.rolling', definition='v1', **options), None, None, None)


def test_index_and_stock_documents_are_separate_and_keep_conflict_protection(tmp_path):
    notes = InvestmentNotes(tmp_path)
    stock = notes.read('000001', '平安银行')
    index = notes.read('index:cn:000001', '上证指数')
    notes.save({**stock, 'content': '# 平安银行'})
    saved = notes.save({**index, 'name': '上证指数', 'content': '# 上证指数\n\n## 2026-10-09 观察\n'})
    assert saved['file'] == 'index_cn000001_上证指数.md'
    assert notes.read('000001')['content'] == '# 平安银行'
    assert notes.read('index:cn:000001')['content'].startswith('# 上证指数')
    assert {item['code'] for item in notes.listing()} == {'000001', 'index:cn:000001'}
    with pytest.raises(NoteConflict):
        notes.save({**index, 'content': 'stale'})
    for code in ('index:000001', 'index:cn:../000001', 'index:sw:123', 'index:hk:000001'):
        with pytest.raises(ValueError):
            notes.read(code)


def test_index_snapshot_uses_one_series_historical_day_and_no_network(db, monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('source collection triggered'))
    _, _, row = source(db)
    with db.connection(write=True) as conn:
        publish(conn, [dict(row, observation_date='2026-10-08', pe_rolling=10), row])
    result = snapshot(db, as_of='2026-10-08', years=3)
    values = {item['id']: item for item in result['items']}
    assert result['code'] == 'index:cn:000300'
    assert values['pe_rolling']['value'] == '10.00倍'
    assert values['pe_rolling_percentile']['position']['label'] == '极低'
    assert 'pb' not in values  # Other providers / series must not be mixed in.
    assert all(item['date'] == '2026-10-08' for item in result['items'])
    assert snapshot(db)['as_of'] == '2026-10-09'
    assert all(not item['available'] for item in snapshot(db, as_of='2026-10-07')['items'])
    with pytest.raises(ValueError, match='日期'):
        snapshot(db, as_of='2099-01-01')


def test_index_crowding_and_spread_keep_zero_negative_and_same_day(db):
    batch, sid, row = source(db)
    with db.connection(write=True) as conn:
        publish(conn, [row])
        publish_prices(conn, [point(batch, sid, observation_date='2026-10-08', close=100),
                              point(batch, sid, close=101)])
        conn.execute('UPDATE index_price_observations SET amount_ratio=CASE close WHEN 100 THEN 0 ELSE 3 END')
        conn.execute('INSERT INTO sovereign_yield_observations '
                     '(observation_date,definition_version,china_10y,content_hash,version,is_latest,batch_id,source_id,updated_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?)', ('2026-10-09', 'v1', '10', 'a'*64, 1, 1, batch, sid, '2026-10-10'))
    def read(group, day):
        return {item['id']: item for item in note_catalog(db, dict(code='index:cn:000300',group=group,as_of=day),None,None,None)['items']}
    assert read('index_crowding', '2026-10-08')['amount_ratio']['value'] == '0.00%'
    assert read('index_spread', '2026-10-09')['spread']['value'].startswith('-2.37')
    assert not read('index_spread', '2026-10-08')['spread']['available']


def test_index_notes_real_browser(index_server, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    base, calls, database = index_server
    _, sid, row = source(database)
    with database.connection(write=True) as conn:
        catalog(conn, 'cn:000016', '上证50', 'broad', provider='csindex', provider_code='000016', source_id=sid)
        publish(conn, [dict(row, index_code='cn:000016', pe_rolling=20)])
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base+'/indices', wait_until='networkidle')
            page.locator('#valuation-panel').scroll_into_view_if_needed()
            page.wait_for_function('window.investmentNotes && !document.getElementById("notes-floating").hidden')
            assert page.locator('#notes-floating button').count() == 2
            page.locator('#notes-floating-write').click()
            page.wait_for_function('document.getElementById("notes-quick").open && !document.querySelector(".CodeMirror").CodeMirror.getOption("readOnly")')
            text = '# 沪深300 · 投资笔记\n\n## 2026-10-09 观察\n\n准备研究指数。\n'
            page.evaluate('(text)=>{const editor=document.querySelector(".CodeMirror").CodeMirror;editor.setValue(text);editor.setCursor(editor.lastLine(),0);}', text)
            page.mouse.click(1, 1)
            page.wait_for_function('!document.getElementById("notes-quick").open')
            path = database.path.parent/'investment_notes'/'index_cn000300_沪深300.md'
            assert path.read_text(encoding='utf-8') == text
            page.locator('#notes-floating-insert').click()
            page.wait_for_selector('#notes-pick-list input[value=pe_rolling]')
            assert page.locator('#notes-pick-group').input_value() == 'index_valuation'
            assert page.locator('#notes-pick-date').input_value() == '2026-10-09'
            assert page.locator('#notes-pick-index-source').input_value() == 'csindex|history.rolling|v1'
            page.locator('#notes-pick-list input[value=pe_rolling_percentile]').check()
            assert '🟢 极低' in page.locator('#notes-pick-preview').inner_text()
            assert page.locator('#notes-pick-preview').inner_text().startswith('- ')
            page.locator('#notes-pick-list input[value=pe_rolling]').check()
            page.locator('#notes-pick-format').select_option('table')
            preview = page.locator('#notes-pick-preview').inner_text()
            assert '滚动 PE/近10年历史分位 | 13.10倍/0.00%（🟢 极低）' in preview
            assert len(preview.strip().splitlines()) == 3
            page.screenshot(path=str(tmp_path/'index-notes-data.png'))
            page.locator('#notes-pick-confirm').click()
            page.locator('#notes-outline').click()
            assert page.locator('#notes-toc button').count() == 2
            page.locator('#notes-close').click()
            page.wait_for_function('!document.getElementById("notes-quick").open')
            assert '滚动 PE/近10年历史分位' in path.read_text(encoding='utf-8')
            page.wait_for_function('document.getElementById("chart").layout.annotations.some(x=>x.name==="investment-note:2026-10-09")')
            page.locator('#chart .annotation').filter(has_text='✎').click()
            page.wait_for_function('document.getElementById("notes-quick").open && document.activeElement.textContent==="2026-10-09 观察"')
            page.locator('#notes-full').click()
            assert page.locator('#notes-panel').is_visible()
            assert not page.locator('#valuation-panel').is_visible()
            page.locator('.research-tabs a[aria-current="page"]').click()
            assert page.locator('#valuation-panel').is_visible()
            before = path.read_text(encoding='utf-8')
            page.locator('#code').select_option('cn:000016')
            page.wait_for_function('document.getElementById("current-index-name").textContent==="上证50"')
            page.locator('#valuation-panel').scroll_into_view_if_needed()
            page.locator('#notes-floating-write').click()
            page.wait_for_function('document.getElementById("notes-company").textContent.includes("上证50") && !document.querySelector(".CodeMirror").CodeMirror.getOption("readOnly")')
            assert page.evaluate('document.querySelector(".CodeMirror").CodeMirror.getValue()') == ''
            page.evaluate('document.querySelector(".CodeMirror").CodeMirror.setValue("# 上证50\\n\\n观察。")')
            page.locator('#notes-close').click()
            page.wait_for_function('!document.getElementById("notes-quick").open')
            assert path.read_text(encoding='utf-8') == before
            assert (path.parent/'index_cn000016_上证50.md').exists()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
            assert not calls and not errors
            page.screenshot(path=str(tmp_path/'index-notes.png'))
        finally:
            browser.close()
