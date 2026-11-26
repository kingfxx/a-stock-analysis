"""Publish Hong Kong F10 statements to the shared financial report store."""

import json

from stock_analysis.collection.collection import parsed_source
from stock_analysis.domains.financials.hk_statement_parser import (
    CATALOG, REPORTS, SOURCE, VERSION, normalize_statements, report_catalog, statement_page,
)
from stock_analysis.domains.stocks.collection_plan import selected
from stock_analysis.domains.stocks.fact_revisions import retain_previous
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch
from stock_analysis.domains.stocks.sync_support import data_lock, encoded, instrument_id, mark_failed, sync_state
from stock_analysis.infrastructure.storage import SyncKey, SyncResult

URL = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
PAGE_SIZE = 500


def collect(db, collector, code, full):
    identity = db.ensure_instrument(code)
    catalog_snapshot = None
    for kind, interface in REPORTS.items():
        dataset = 'financial.' + kind
        if not selected(dataset, interface):
            continue
        with data_lock(db, 'hk:' + dataset, code):
            catalog_snapshot = _collect_statement(db, collector, identity, code, kind, interface, full,
                                                  catalog_snapshot)


def _collect_statement(db, collector, identity, code, kind, interface, full, catalog_snapshot):
    dataset = 'financial.' + kind
    key = SyncKey(identity, 'financial:' + kind, SOURCE, 'raw')
    state = sync_state(db, key)
    saved = {row['period']: row for row in db.financial_reports(identity, SOURCE, kind)}
    lower = sorted(saved)[-min(2, len(saved))] if saved and not full else None
    run = db.start_sync(key, parser_version=VERSION, methodology_version=VERSION,
                        trigger_reason='full_audit' if full else 'incremental', requested_start=lower)
    batch = None
    sources = []
    try:
        batch = begin_stock_batch(db, run, 'eastmoney', dataset, code, start=lower,
                                  scope={'exchange': 'hk', 'source_interface': interface},
                                  parser_version=VERSION, definition_version=VERSION)
        base = {'filter': f'(SECUCODE="{code}.HK")', 'source': 'F10', 'client': 'PC'}

        def request(params, parse):
            def healthy(response):
                try:
                    return bool(parse(response.json()))
                except (TypeError, ValueError, KeyError, AttributeError):
                    return False
            response, source = collector.get(batch, 'eastmoney', 'stock.' + dataset, URL, params,
                evidence_mode='metadata_only', healthy_response=healthy)
            sources.append(source)
            return parse(response.json())

        if catalog_snapshot is None:
            catalog = request({**base, 'reportName': CATALOG, 'columns':
                'SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,START_DATE,REPORT_DATE,FISCAL_YEAR,'
                'CURRENCY,ACCOUNT_STANDARD,REPORT_TYPE'}, lambda payload: report_catalog(payload, code))
            catalog_snapshot = catalog, sources[-1]
        catalog, catalog_source_id = catalog_snapshot
        periods = [period for period in sorted(catalog) if lower is None or period >= lower]
        if not periods:
            raise ValueError('东财港股报告目录未覆盖本地报告期')
        bounds = f"(REPORT_DATE>='{periods[0]}')(REPORT_DATE<='{periods[-1]}')"
        params = {**base, 'filter': base['filter'] + bounds, 'reportName': interface,
                  'columns': 'ALL', 'sortColumns': 'REPORT_DATE,STD_ITEM_CODE',
                  'sortTypes': '-1,1', 'pageSize': PAGE_SIZE}
        allowed_periods = set(periods)

        def read_page(page):
            return request({**params, 'pageNumber': page},
                           lambda payload: statement_page(payload, code, allowed_periods, page, PAGE_SIZE))

        first = read_page(1)
        if first[1] > 100:
            raise ValueError('东财港股报表分页数异常')
        records = list(first[2])
        for page in range(2, first[1] + 1):
            result = read_page(page)
            if result[:2] != first[:2]:
                raise ValueError('东财港股报表跨页总数变化')
            records.extend(result[2])
        if first[1] > 1 and read_page(1) != first:
            raise ValueError('东财港股报表首页复核不一致')
        reports = normalize_statements(records, kind, catalog)
        if not reports or (saved.keys() & set(periods)) - reports.keys():
            raise ValueError('东财港股报表遗漏已有报告期')
        db.ensure_instrument(code, catalog[max(catalog)]['name'])
        rows = [{'period': period, 'raw_json': encoded(report)} for period, report in reports.items()]
        for source in sources:
            parsed_source(db, source, fields=['SECUCODE', 'REPORT_DATE', 'STD_ITEM_CODE', 'AMOUNT'])
        published_periods = sorted(saved.keys() | reports.keys())
        first, last = published_periods[0], published_periods[-1]
        result = SyncResult(len(rows), min(state.get('coverage_start') or first, first), last, last)

        def write(conn):
            for row in rows:
                previous = saved.get(row['period'])
                if previous and previous['raw_json'] != row['raw_json']:
                    retain_previous(conn, row['period'], previous)
            return db.upsert_financial_reports(conn, key, run, rows)

        db.complete_sync(run, result, write, collection_batch_id=batch,
                         batch_result={'actual_periods': sorted(reports), 'catalog_source_id': catalog_source_id})
        return catalog_snapshot
    except Exception as exc:
        for source in sources:
            parsed_source(db, source, error=type(exc).__name__)
        mark_failed(db, run, exc, collection_batch_id=batch)
        raise


def read_reports(db, code):
    identity = instrument_id(db, code)
    if identity is None:
        return {}, None
    grouped, stamps = {}, []
    for kind in REPORTS:
        for row in db.financial_reports(identity, SOURCE, kind):
            grouped.setdefault(row['period'], {})[kind] = json.loads(row['raw_json'])
            stamps.append(row['obtained_at'])
    return grouped, max(stamps, default=None)
