"""Publish the verified Hong Kong distribution history in the shared dividend table."""

import json

from stock_analysis.collection.collection import parsed_source
from stock_analysis.domains.stocks.fact_revisions import retain_previous
from stock_analysis.domains.stocks.hk_dividend_parser import INTERFACE, SOURCE, VERSION, dividend_page, normalize_events
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch
from stock_analysis.domains.stocks.sync_support import data_lock, encoded, instrument_id, mark_failed, sync_state
from stock_analysis.infrastructure.storage import SyncKey, SyncResult

URL = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
SIZE = 200


def collect(db, collector, code, full):
    identity = db.ensure_instrument(code)
    with data_lock(db, 'hk:dividends', code):
        _collect(db, collector, code, identity, full)


def _collect(db, collector, code, identity, full):
    key = SyncKey(identity, 'dividends', SOURCE)
    state = sync_state(db, key)
    saved = {row['event_key']: row for row in db.dividend_events(identity, SOURCE)}
    run = db.start_sync(key, parser_version=VERSION, methodology_version=VERSION,
                        trigger_reason='full' if full else 'incremental')
    batch = None
    sources = []
    try:
        batch = begin_stock_batch(db, run, 'eastmoney', 'dividends', code,
            scope={'exchange': 'hk', 'source_interface': INTERFACE}, parser_version=VERSION, definition_version=VERSION)

        def page(number):
            params = {'reportName': INTERFACE, 'columns': 'ALL', 'filter': f'(SECUCODE="{code}.HK")', 'source': 'F10',
                      'client': 'PC', 'pageNumber': number, 'pageSize': SIZE, 'sortColumns': 'EX_DIVIDEND_DATE', 'sortTypes': -1}
            def healthy(response):
                try:
                    dividend_page(response.json(), code, number, SIZE)
                    return True
                except (KeyError, TypeError, ValueError):
                    return False
            response, source = collector.get(batch, 'eastmoney', 'stock.dividends', URL, params,
                evidence_mode='metadata_only', healthy_response=healthy)
            sources.append(source)
            return dividend_page(response.json(), code, number, SIZE)

        first = page(1)
        rows = list(first[2])
        if first[1] > 30:
            raise ValueError('东财港股分红页数异常')
        for i in range(2, first[1] + 1):
            result = page(i)
            if result[:2] != first[:2]:
                raise ValueError('东财港股分红跨页总数变化')
            rows.extend(result[2])
        if first[1] > 1 and page(1) != first:
            raise ValueError('东财港股分红首页复核不一致')
        events = normalize_events(rows)
        for event in events:
            old = saved.get(event['event_key'])
            if old and any(old[field] is not None and event[field] is None for field in
                           ('payment_date', 'payment_per_share', 'declared_per_share')):
                raise ValueError('港股分红已知字段消失，保留原事实')
        if saved.keys() - {r['event_key'] for r in events}:
            raise ValueError('港股分红已有除净事件消失，保留原事实')
        for source in sources:
            parsed_source(db, source, fields=['SECUCODE', 'EX_DIVIDEND_DATE', 'DIVIDEND_DATE', 'PLAN_EXPLAIN'])
        days = sorted(row['ex_dividend_date'] for row in events)
        coverage = SyncResult(len(events), min(state.get('coverage_start') or days[0], days[0]), days[-1], days[-1]) if days else SyncResult(0, no_data=True)

        def write(conn):
            columns = ('declared_currency', 'declared_per_share', 'payment_currency', 'payment_per_share', 'payment_date')
            prepared = [{**row, 'raw_json': encoded(row['raw_json'])} for row in events]
            for row in prepared:
                previous = saved.get(row['event_key'])
                if previous and previous['raw_json'] != row['raw_json']:
                    retain_previous(conn, row['event_key'], previous)
            counters = db.upsert_dividend_events(conn, key, run, prepared)
            conn.executemany('UPDATE dividend_events SET ' + ','.join(f'{field}=?' for field in columns)
                + ' WHERE instrument_id=? AND source=? AND event_key=?',
                [(*(row[field] for field in columns), identity, SOURCE, row['event_key']) for row in events])
            return counters

        db.complete_sync(run, coverage, write, collection_batch_id=batch)
    except Exception as exc:
        for source in sources:
            parsed_source(db, source, error=type(exc).__name__)
        mark_failed(db, run, exc, collection_batch_id=batch)
        raise


def read(db, code):
    identity = instrument_id(db, code)
    if identity is None:
        return []
    return [{field: row[field] for field in ('notice_date', 'ex_dividend_date', 'payment_date', 'status',
                'declared_currency', 'declared_per_share', 'payment_currency', 'payment_per_share')}
             | {'plan': json.loads(row['raw_json'])['PLAN_EXPLAIN']}
            for row in db.dividend_events(identity, SOURCE)]
