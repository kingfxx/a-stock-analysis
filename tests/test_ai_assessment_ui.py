"""Browser acceptance against an isolated local server and simulated inference."""
from pathlib import Path
import json
import re
from threading import Thread
from http.server import ThreadingHTTPServer

import pytest

from quarterly_dashboard import server
from test_ai_assessment import database, service


@pytest.mark.parametrize('fresh_cache',[False,True])
def test_ai_drawer_history_settings_keyboard_and_width(database, monkeypatch, tmp_path, fresh_cache):
    playwright = pytest.importorskip('playwright.sync_api')
    item=service(database)
    monkeypatch.setattr(server,'DATABASE_PATH',database.path)
    monkeypatch.setattr(server,'ai_service',lambda:item)
    if fresh_cache:
        original_render = server.render_page
        def cached_page(code, refresh):
            html = original_render(code, refresh)
            def replace_payload(match):
                payload = json.loads(match.group(2))
                payload['loading'] = {key:False for key in payload['loading']}
                payload['price_needs_update'] = False
                return match.group(1)+json.dumps(payload,ensure_ascii=False)+match.group(3)
            return re.sub(r'(<script id="payload" type="application/json">)(.*?)(</script>)',replace_payload,html)
        monkeypatch.setattr(server,'render_page',cached_page)
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    thread=Thread(target=http.serve_forever,daemon=True); thread.start()
    base=f'http://127.0.0.1:{http.server_port}'
    try:
        with playwright.sync_playwright() as p:
            try:
                browser=p.chromium.launch(channel='msedge',headless=True)
            except playwright.Error as error:
                pytest.skip('Headless Edge unavailable: '+str(error).splitlines()[0])
            with browser:
                page=browser.new_page(viewport={'width':1280,'height':900})
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                # Automatic source updates must not contact external data providers.
                def route(request):
                    path=request.request.url.split(base)[-1]
                    if path.startswith(('/api/analysis','/api/ai/','/api/stock-groups')):
                        request.continue_()
                    else:
                        request.fulfill(status=503,content_type='application/json',body='{"error":"isolated source unavailable"}')
                page.route('**/api/*',route)
                page.goto(base+'/?code=600900',wait_until='networkidle')
                entry=page.get_by_role('button',name='AI 研判',exact=True)
                assert entry.count()==1
                assert item.provider.calls==0
                if fresh_cache:
                    assert '数据更新中' not in page.locator('.ai-summary').inner_text()
                entry.click()
                drawer=page.get_by_role('dialog',name='AI 综合研判',exact=True)
                drawer.wait_for(state='visible')
                page.wait_for_function("document.querySelector('.ai-summary').textContent.includes('经营稳定')")
                assert item.provider.calls==1
                assert '报告数据与当前资料已不同' not in drawer.inner_text()
                drawer.get_by_role('button',name='历史记录',exact=True).click()
                drawer.get_by_role('button',name='本次报告',exact=True).wait_for()
                assert item.provider.calls==1
                drawer.get_by_role('button',name='本次报告',exact=True).click()
                drawer.get_by_role('button',name='依据',exact=True).first.click()
                assert drawer.locator('details[open]').count()>=1
                for width in (1280,1600,390):
                    page.set_viewport_size({'width':width,'height':900})
                    page.wait_for_timeout(100)
                    dimensions=drawer.bounding_box()
                    assert dimensions['width']<=width
                    assert dimensions['width'] == pytest.approx(width if width<800 else min(720,width*.52),abs=2)
                    # The AI components must fit even when the report is lengthy.
                    assert drawer.evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
                    assert page.locator('.ai-summary').evaluate('(node)=>node.getBoundingClientRect().right<=window.innerWidth')
                    if width in (1280,390):
                        page.screenshot(path=str(tmp_path/f'ai-{width}.png'))
                page.keyboard.press('Tab')
                assert page.evaluate("document.querySelector('dialog[open]').contains(document.activeElement)")
                page.keyboard.press('Escape')
                assert not drawer.is_visible()
                assert entry.evaluate('(node)=>document.activeElement===node')
                page.get_by_role('button',name='AI 设置',exact=True).click()
                settings=page.get_by_role('dialog',name='AI 设置',exact=True)
                settings.get_by_label('选择默认模型').wait_for()
                assert item.provider.calls==1
                settings.get_by_role('button',name='关闭',exact=True).click()
                page.get_by_role('button',name='查看报告',exact=True).click()
                assert item.provider.calls==1
                assert not errors, errors
    finally:
        if item.worker: item.worker.join(3)
        http.shutdown(); http.server_close(); thread.join(3)
