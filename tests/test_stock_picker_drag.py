"""Exercise trusted mouse/touch input in Edge against an isolated stock library."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from threading import Thread

import pytest

from quarterly_dashboard import server
from quarterly_dashboard.stock_library import StockLibrary
from quarterly_dashboard.storage import Database, SyncKey, SyncResult


@pytest.mark.parametrize('script,expected', [
    ('stock_picker_drag.cjs','drag checks passed'),
    ('stock_unfollow.cjs','unfollow checks passed'),
    ('stock_cleanup.cjs','cleanup checks passed'),
])
def test_stock_picker_drag_with_real_browser(tmp_path, monkeypatch, script, expected):
    edge = shutil.which('msedge') or r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    node = shutil.which('node')
    if not Path(edge).is_file() or not node:
        pytest.skip('Edge and Node are needed for the browser drag regression')
    db = Database(tmp_path / 'stocks.sqlite3'); db.initialize()
    codes = [f'{index:06d}' for index in range(1,31)]
    for index, code in enumerate(codes):
        db.ensure_instrument(code, f'测试股票 {index+1}')
    library = StockLibrary(db)
    for name in ['重点关注','另一分组']:
        identity = library.change({'action':'create','name':name})['groups'][-1]['id']
        library.change({'action':'membership','id':identity,'codes':codes,'add':True})
    library.change({'action':'select','group':'group:1'})
    if script == 'stock_cleanup.cjs':
        key = SyncKey(db.ensure_instrument('000001'),'financial:merged','legacy')
        run = db.start_sync(key,parser_version='test',methodology_version='test')
        db.complete_sync(run,SyncResult(1,'2025-12-31','2025-12-31','2025-12-31'),lambda conn:db.upsert_financial_reports(
            conn,key,run,[{'period':'2025-12-31','raw_json':{'revenue_ytd':100,'profit_ytd':20}}]))
        cache = tmp_path/'fundamentals'/'000001.json';cache.parent.mkdir();cache.write_text('{"cache":1}',encoding='utf-8')
    monkeypatch.setattr(server, 'DATABASE_PATH', db.path)
    def page(code, refresh):
        data = library.read()
        payload = {'code':code,'views':{},'warnings':[],'valuation':{'views':{}},'loading':{},
                   'price_version':1,'price_needs_update':False,'stock_library':data,'unfollowed':library.is_unfollowed(code),
                   'cached_stocks':[{'code':stock['code'],'name':stock['name']} for stock in data['stocks']]}
        return server.TEMPLATE.replace('__PAYLOAD__',json.dumps(payload,ensure_ascii=False)).replace('__CODE__',code).replace('__ERROR__','')
    monkeypatch.setattr(server, 'render_page', page)
    httpd = server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    httpd.restore_manager = server.RestoreManager(db)
    httpd.request_gate = server.RequestGate()
    thread = Thread(target=httpd.serve_forever,daemon=True);thread.start()
    profile = tmp_path/'browser'
    browser = None
    try:
        with (tmp_path/'edge.log').open('w',encoding='utf-8') as log:
            browser = subprocess.Popen([edge,'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
                '--remote-debugging-port=0',f'--user-data-dir={profile}','about:blank'],stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            endpoint = profile/'DevToolsActivePort'
            deadline=time.monotonic()+15
            while not endpoint.exists():
                if browser.poll() is not None or time.monotonic()>deadline:
                    pytest.fail('Headless browser failed to start: '+(tmp_path/'edge.log').read_text(encoding='utf-8'))
                time.sleep(.05)
            debug_port = endpoint.read_text().splitlines()[0]
            result = subprocess.run([node,str(Path(__file__).with_name(script)),debug_port,
                f'http://127.0.0.1:{httpd.server_port}',str(tmp_path/'drag-desktop.png'),str(tmp_path/'drag-mobile.png')],
                capture_output=True,text=True,encoding='utf-8',timeout=55)
            assert result.returncode == 0, result.stdout+'\n'+result.stderr
            assert expected in result.stdout
    finally:
        if browser and browser.poll() is None:
            browser.terminate()
            try: browser.wait(timeout=5)
            except subprocess.TimeoutExpired: browser.kill();browser.wait(timeout=5)
        httpd.shutdown();httpd.server_close();thread.join(timeout=2)
