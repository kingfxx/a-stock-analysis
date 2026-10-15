"""DCF endpoint uses stored data without refresh or provider calls."""

import io
import json
from types import SimpleNamespace

from quarterly_dashboard import server
from quarterly_dashboard.fundamental_service import FundamentalService
from quarterly_dashboard.price_service import PriceService
from quarterly_dashboard.storage import Database


def request(path):
    statuses, headers = [], {}
    handler = SimpleNamespace(path=path, wfile=io.BytesIO(),
                              send_response=statuses.append,
                              send_header=lambda key, value: headers.update({key: value}),
                              end_headers=lambda: None)
    server.Handler._do_GET(handler)
    return statuses[0], headers, handler.wfile.getvalue()


def test_dcf_route_reads_isolated_store_without_updates(tmp_path, monkeypatch):
    db = Database(server.DATABASE_PATH)
    db.initialize()
    db.ensure_instrument('600887', '伊利股份')
    def forbidden(*args, **kwargs):
        raise AssertionError('DCF must not refresh source data')
    monkeypatch.setattr(FundamentalService, 'update', forbidden)
    monkeypatch.setattr(PriceService, 'ensure', forbidden)
    status, headers, body = request('/api/dcf?code=600887&refresh=1')
    assert status == 200
    payload = json.loads(body)
    assert payload['code'] == '600887'
    assert payload['growth_defaults'] == dict(stage1_pct=15, stage2_pct=8, stage3_pct=4)
    assert payload['defaults'] == dict(base_window='3', growth_pct=3, discount_pct=9, terminal_growth_pct=2)
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Content-Type'].startswith('application/json')


def test_dcf_route_rejects_invalid_code():
    status, _, body = request('/api/dcf?code=bad')
    assert status == 400
    assert json.loads(body)['error']


def test_dcf_assets_served():
    for name, content_type in [('dcf.js', 'text/javascript'), ('dcf.css', 'text/css')]:
        status, headers, body = request('/' + name)
        assert status == 200
        assert headers['Content-Type'].startswith(content_type)
        assert body
