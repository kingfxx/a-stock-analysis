import pytest

from stock_analysis.collection.collection_progress import attach_data_counts
from stock_analysis.domains.indices import index_collection, sina_index_collection as sina
from stock_analysis.domains.indices.index_prices import read_overlay
from test_index_collection import db, http, response


def quote(day, time='15:00:03'):
    fields = ['创业板50', '105', '101', '110', '112', '104', *(['0'] * 24), day, time, '00']
    return response(body=('var hq_str_sz399673="' + ','.join(fields) + '";').encode('gb18030'),
                    headers={'Content-Type': 'application/javascript'})


def test_history_backfill_then_close_snapshot(db, monkeypatch):
    first, latest = '2026-11-26', '2026-11-27'
    client, session, _ = http(db, [response(), quote(latest)])
    operation = index_collection.start_operation(db, 'backfill', ('sina',), first, latest)
    worker = index_collection.IndexCollector(db, operation, request_collector=client)
    worker.trading_days = {first, latest}
    worker.auto_incremental = False
    monkeypatch.setattr(sina, 'parse_history', lambda *_: [
        dict(_code='399673', date=first, open=100, close=101, high=104, low=99)])

    sina.collect(worker, first, first, 'backfill')
    worker.auto_incremental = True
    sina.collect(worker, first, latest, 'incremental')
    assert sina.collect(worker, first, latest, 'incremental') is index_collection.SKIPPED_WORK
    assert [call[1] for call in session.calls] == [sina.HISTORY_URL, sina.QUOTE_URL]
    points = read_overlay(db, 'cn:399673')
    assert points['provider'] == 'sina'
    assert points['rows'] == [dict(observation_date=first, close=101), dict(observation_date=latest, close=110)]
    operation_rows = [dict(operation_ref=operation, result={})]
    with db.connection() as conn:
        attach_data_counts(conn, operation_rows, ['sina'])
    counts, = operation_rows[0]['result']['data_counts']
    assert (counts['provider'], counts['label'], counts['written']) == ('sina', '行情', 2)


@pytest.mark.parametrize(('observed', 'time', 'message'), [
    ('2026-11-26', '15:00:03', '缺少 1 个交易日'),
    ('2026-11-27', '14:59:00', '尚未收盘'),
])
def test_quote_rejects_wrong_session(db, observed, time, message):
    latest = '2026-11-27'
    client, _, _ = http(db, [quote(observed, time)])
    worker = index_collection.IndexCollector(db, 'sina-test', request_collector=client)
    worker.trading_days = {latest}
    worker.auto_incremental = True
    with pytest.raises(ValueError, match=message):
        sina.collect(worker, latest, latest, 'incremental')
    assert not read_overlay(db, 'cn:399673')['available']
