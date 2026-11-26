"""Collect displayed Hong Kong revenue segments and publish common segment versions."""

from stock_analysis.collection.collection import parsed_source
from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService, FIELDS
from stock_analysis.domains.financials.business_segment_storage import publish_versions
from stock_analysis.domains.financials.hk_segment_parser import INTERFACE, SOURCE, VERSION, parse_segments
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch
from stock_analysis.domains.stocks.sync_support import data_lock, mark_failed
from stock_analysis.infrastructure.storage import SyncKey, SyncResult, utc_now


def collect(db, collector, code, full):
    identity = db.ensure_instrument(code)
    with data_lock(db, 'hk:business_segments', code):
        _collect(db, collector, code, identity, full)


def _collect(db, collector, code, identity, full):
    key = SyncKey(identity, 'business_segments', SOURCE)
    saved = {row['report_date']: row for row in BusinessSegmentService(db)._saved(identity, SOURCE)}
    run = db.start_sync(key, parser_version=VERSION, methodology_version=VERSION,
                        trigger_reason='full_audit' if full else 'incremental')
    batch = None
    source = None
    try:
        batch = begin_stock_batch(db, run, 'stockanalysis', 'business_segments', code,
                                  scope={'exchange': 'hk', 'source_interface': INTERFACE},
                                  parser_version=VERSION, definition_version=VERSION)
        url = 'https://stockanalysis.com/quote/hkg/' + code[1:] + '/' + INTERFACE

        def healthy(response):
            try:
                return bool(parse_segments(response.text, code))
            except ValueError:
                return False

        response, source = collector.get(batch, 'stockanalysis', 'stock.business_segments', url,
            expect='html', evidence_mode='metadata_only', healthy_response=healthy)
        currency, periods = parse_segments(response.text, code)
        for period, old in saved.items():
            if min(periods) <= period <= max(periods):
                fresh = {(row['dimension_type'], row['item_key']): row for row in periods.get(period, [])}
                if any((row['dimension_type'], row['item_key']) not in fresh for row in old['items']):
                    raise ValueError('港股主营既有分部消失，已保留原版本')
        stamp = utc_now()
        for items in periods.values():
            for row in items:
                row.update({field: row.get(field) for field in FIELDS})
                row.update(source_id=source, obtained_at=stamp)
        parsed_source(db, source, fields=['Fiscal Year', 'Period Ending', 'Revenue'])
        actual = sorted(saved.keys() | periods.keys())
        db.complete_sync(run, SyncResult(sum(map(len, periods.values())), actual[0], actual[-1], actual[-1]),
            lambda conn: publish_versions(conn, periods, saved, identity, SOURCE, VERSION, run, batch, FIELDS,
                                          currency=currency),
            collection_batch_id=batch, batch_result={'actual_periods': sorted(periods)})
    except Exception as exc:
        if source is not None:
            parsed_source(db, source, error=type(exc).__name__)
        mark_failed(db, run, exc, collection_batch_id=batch)
        raise
