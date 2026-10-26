from stock_analysis.http import rendering
import json

import pytest

from stock_analysis.http import server
from stock_analysis.domains.notes.investment_notes import InvestmentNotes
from test_index_ui import index_server, db


def test_unified_note_tabs_from_both_pages(index_server, monkeypatch, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    base, calls, database = index_server
    notes = InvestmentNotes(database.path.parent)
    stock = notes.save({**notes.read('000001', '平安银行'), 'name': '平安银行',
                        'content': '# 平安银行\n\n## 2026-10-09\n\n仅个股正文。\n'})
    index = notes.save({**notes.read('index:cn:000300', '沪深300'), 'name': '沪深300',
                        'content': '# 沪深300\n\n## 2026-10-09\n\n仅指数正文。\n'})
    for code, name in [('300750','宁德时代'),('605499','东鹏饮料'),('601919','中远海控')]:
        notes.save({**notes.read(code, name), 'name': name, 'content': '# '+name+'\n其他记录。'})

    def stock_page(code, refresh):
        payload = dict(code=code, name='平安银行', views={}, valuation={'views': {}}, loading={},
                       warnings=[], price_version=0, price_needs_update=False, cached_stocks=[])
        return (rendering.TEMPLATE.replace('__RESEARCH_SIDEBAR__', rendering.research_sidebar('stock'))
                .replace('__PAYLOAD__', json.dumps(payload, ensure_ascii=False)).replace('__CODE__', code).replace('__ERROR__', ''))
    monkeypatch.setattr(rendering, 'render_page', stock_page)

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        try:
            common_style = None
            for kind in ('index', 'stock'):
                page = browser.new_page(viewport={'width':1440, 'height':1100})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base+('/indices' if kind == 'index' else '/?code=000001&tab=notes'), wait_until='networkidle')
                if kind == 'index':
                    page.locator('[data-view=notes]').click()
                page.wait_for_function('document.querySelector(".CodeMirror") && !document.querySelector(".CodeMirror").CodeMirror.getOption("readOnly")')
                page.locator('#notes-all').click()
                page.wait_for_selector('#notes-browser-list button')
                def geometry():
                    return page.evaluate('''()=>{
                        const root=document.getElementById('notes-browser').getBoundingClientRect();
                        return ['notes-browser','notes-browser-stock-tab','notes-browser-index-tab','notes-search','notes-browser-list'].map(id=>{
                            const r=document.getElementById(id).getBoundingClientRect();return [r.left-root.left,r.top-root.top,r.width,r.height];
                        });
                    }''')
                origin = geometry()
                style = page.evaluate('''()=>{
                    const ids=['#notes-browser','#notes-browser h3','#notes-browser-stock-tab','#notes-search','#notes-browser-list button','#notes-browser-list small'];
                    return ids.map(selector=>{const s=getComputedStyle(document.querySelector(selector));return [s.fontFamily,s.fontSize,s.fontWeight,s.lineHeight];});
                }''')
                if common_style is None:
                    common_style = style
                else:
                    assert style == common_style, 'host page styles do not change the shared manager typography'
                assert page.locator(f'#notes-browser-{kind}-tab').get_attribute('aria-selected') == 'true'
                page.locator('#notes-browser-stock-tab').click()
                assert geometry() == origin, 'four stock notes do not move the dialog or its controls'
                assert page.locator('#notes-browser-list').inner_text().count('平安银行') >= 1
                assert '沪深300' not in page.locator('#notes-browser-list').inner_text()
                page.locator('#notes-search').fill('仅指数正文')
                page.wait_for_function('document.getElementById("notes-browser-list").textContent.includes("没有匹配")')
                assert geometry() == origin, 'empty search does not shrink the dialog'
                page.locator('#notes-browser-index-tab').click()
                page.wait_for_selector('#notes-browser-list button')
                assert page.locator('#notes-search').input_value() == '仅指数正文'
                assert '沪深300' in page.locator('#notes-browser-list').inner_text()
                assert '平安银行' not in page.locator('#notes-browser-list').inner_text()
                assert geometry() == origin, 'one index note uses the same layout as four stock notes'
                page.locator('#notes-search').fill('')
                page.locator('#notes-browser-index-tab').focus()
                page.keyboard.press('ArrowLeft')
                assert page.locator('#notes-browser-stock-tab').get_attribute('aria-selected') == 'true'
                page.locator('#notes-browser-index-tab').click()
                page.locator('#notes-browser-list button').click()
                page.wait_for_function('!document.getElementById("notes-browser").open && document.getElementById("notes-company").textContent.includes("沪深300")')
                assert '仅指数正文' in page.evaluate('document.querySelector(".CodeMirror").CodeMirror.getValue()')
                assert page.locator('#notes-preview-button').get_attribute('aria-pressed') == 'true'
                assert page.locator('#notes-rendered').is_visible()
                assert not page.locator('#notes-editor-host').is_visible()
                if kind == 'stock':
                    assert page.locator('#notes-insert').is_disabled()
                page.locator('#notes-edit').click()
                page.evaluate('document.querySelector(".CodeMirror").CodeMirror.replaceRange("\\n统一管理编辑。\\n",{line:4,ch:0});window.investmentNotes.flush()')
                assert '统一管理编辑' in (notes.root()/index['file']).read_text(encoding='utf-8')
                page.locator('#notes-all').click()
                page.wait_for_selector('#notes-browser-list button')
                page.locator('#notes-browser-stock-tab').click()
                page.locator('#notes-browser-list button').filter(has_text='平安银行').click()
                page.wait_for_function('!document.getElementById("notes-browser").open && document.getElementById("notes-company").textContent.includes("平安银行")')
                assert '仅个股正文' in page.evaluate('document.querySelector(".CodeMirror").CodeMirror.getValue()')
                assert page.locator('#notes-preview-button').get_attribute('aria-pressed') == 'true'
                assert page.locator('#notes-rendered').is_visible()
                if kind == 'index':
                    assert page.locator('#notes-insert').is_disabled()
                assert '统一管理编辑' not in (notes.root()/stock['file']).read_text(encoding='utf-8')
                page.locator('#notes-all').click()
                page.wait_for_selector('#notes-browser-list button')
                page.screenshot(path=str(tmp_path/f'unified-notes-{kind}.png'))
                page.set_viewport_size({'width':390, 'height':844})
                mobile = geometry()
                page.locator('#notes-browser-index-tab').click()
                assert geometry() == mobile
                assert page.locator('#notes-browser').evaluate('(node)=>{const r=node.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}')
                assert not errors
                page.close()
            assert not calls
        finally:
            browser.close()
