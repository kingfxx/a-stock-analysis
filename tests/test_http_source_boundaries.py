"""Behavior and isolation guarantees across the extracted application boundaries."""
import json
import ast
import inspect
from pathlib import Path
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace

import pytest
import requests

from stock_analysis import application
from stock_analysis.http import rendering, server
from stock_analysis.domains.stocks.data_service import StockDataService
from stock_analysis.infrastructure.app_settings import AppSettings


def test_parser_import_and_execution_need_no_network_or_storage():
    code = r'''
import sys
from stock_analysis.domains.stocks import source_parsers as parser
assert 'requests' not in sys.modules
assert 'sqlite3' not in sys.modules
assert 'stock_analysis.http.server' not in sys.modules
assert parser.parse_stock_name('v_sh601919="1~中远海控~601919";', 'sh601919') == '中远海控'
rows = parser.parse_price_history({'code': 0, 'data': {'sh601919': {'qfqday': [
    ['2026-01-02', '-1', '0', '-1', '0', '0']]}}}, 'sh601919', 'qfq')
assert rows[0]['open'] == -1 and rows[0]['close'] == 0 and rows[0]['volume'] == 0
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stock_service_uses_injected_settings_and_separate_state(tmp_path):
    first = StockDataService(AppSettings(CACHE=tmp_path / 'one', DATABASE_PATH=tmp_path / 'one.sqlite3'))
    second = StockDataService(AppSettings(CACHE=tmp_path / 'two', DATABASE_PATH=tmp_path / 'two.sqlite3'))
    first.settings.CACHE.mkdir()
    (first.settings.CACHE / '601919.json').write_text('{"code":"601919","name":"中远海控"}', encoding='utf-8')
    assert first.cached_stocks() == [{'code': '601919', 'name': '中远海控'}]
    assert second.cached_stocks() == []
    assert first._SERVICE_LOCK is not second._SERVICE_LOCK
    assert first._SERVICES is not second._SERVICES


def test_page_payload_escapes_script_end_without_changing_json(monkeypatch):
    name = '</script><script>alert(1)</script>'
    monkeypatch.setattr(application.data, 'page_payload', lambda *args: ('601919', {'name': name}, ''))
    page = rendering.render_page('601919', False)
    embedded = page.split('<script id="payload" type="application/json">', 1)[1].split('</script>', 1)[0]
    assert '<' not in embedded
    assert json.loads(embedded)['name'] == name


@pytest.fixture
def protocol_server():
    # Explicit checked port; no database initialization or scheduler.
    http = None
    for port in (8766, 8768, *range(18840, 18860)):
        try:
            http = server.ThreadingHTTPServer(('127.0.0.1', port), server.Handler)
            break
        except OSError:
            pass
    assert http is not None
    worker = Thread(target=http.serve_forever, daemon=True)
    worker.start()
    session = requests.Session()
    session.trust_env = False
    try:
        yield http, session, f'http://127.0.0.1:{http.server_port}'
    finally:
        session.close()
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)


def test_restore_status_bypasses_closed_gate_but_regular_routes_do_not(protocol_server):
    http, session, base = protocol_server
    calls = []
    http.request_gate = SimpleNamespace(enter=lambda: calls.append('enter') or False,
                                        leave=lambda: calls.append('leave'))
    http.restore_manager = SimpleNamespace(state={'phase': 'restoring'})
    response = session.get(base + '/api/maintenance/restore/status', timeout=5)
    assert response.status_code == 200 and response.json() == {'phase': 'restoring'}
    assert calls == []
    response = session.get(base + '/datetime.js', timeout=5)
    assert response.status_code == 503 and response.json()['restoring']
    assert calls == ['enter']


def test_unknown_methods_and_command_validation_keep_http_contract(protocol_server):
    _, session, base = protocol_server
    assert session.get(base + '/missing', timeout=5).status_code == 404
    assert session.post(base + '/missing', json={}, timeout=5).status_code == 404
    assert session.put(base + '/missing', json={}, timeout=5).status_code == 405
    response = session.post(base + '/api/stock-groups', data='{}', timeout=5)
    assert response.status_code == 400
    assert response.json()['error'] == '请使用 JSON 操作分组'
    assert response.headers['Cache-Control'] == 'no-store'
    response = session.post(base + '/api/stock-groups', json={}, headers={'Origin': 'https://invalid.test'}, timeout=5)
    assert response.status_code == 400 and response.json()['error'] == '不允许跨站修改分组'
    response = session.get(base + '/api/maintenance/restore/status', headers={'Host': 'invalid.test'}, timeout=5)
    assert response.status_code == 400


def test_feature_route_methods_cannot_shadow_each_other():
    seen = set()
    for base in server.Handler.__bases__:
        if base is server.BaseHTTPRequestHandler:
            continue
        tree = ast.parse(inspect.getsource(base))
        for node in tree.body[0].body:
            if isinstance(node, ast.FunctionDef):
                assert node.name not in seen, f'Unreachable route method: {node.name}'
                seen.add(node.name)
