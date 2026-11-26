"""Collect HKEXnews title metadata through the shared stock operation."""

from datetime import date, datetime, timedelta

from stock_analysis.collection.collection import parsed_source
from stock_analysis.domains.stocks.hk_announcement_parser import HOST, INTERFACE, LOCAL, SOURCE, VERSION, filings, issuer_id
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch
from stock_analysis.domains.stocks.sync_support import data_lock, digest, encoded, instrument_id, mark_failed, sync_state
from stock_analysis.infrastructure.storage import SyncKey, SyncResult, utc_now


def collect(db, collector, code, full):
    identity = db.ensure_instrument(code)
    with data_lock(db, 'hk:announcements', code):
        _collect(db, collector, identity, code, full)


def _collect(db, collector, identity, code, full):
    key = SyncKey(identity, 'announcements', SOURCE)
    state = sync_state(db, key)
    today = datetime.now(LOCAL).date()
    floor = date(today.year - 10, today.month, min(today.day, 28) if today.month == 2 else today.day)
    start = floor if full or not state.get('coverage_end') else max(floor, date.fromisoformat(state['coverage_end']) - timedelta(days=7))
    run = db.start_sync(key, parser_version=VERSION, methodology_version=VERSION, requested_start=start.isoformat(),
                        requested_end=today.isoformat(), trigger_reason='full' if full else 'incremental')
    batch = None
    try:
        batch = begin_stock_batch(db, run, 'hkexnews', 'announcements', code, start=start.isoformat(), end=today.isoformat(),
                                  scope={'exchange': 'hk', 'source_interface': INTERFACE},
                                  parser_version=VERSION, definition_version=VERSION)

        def request(interface, params, parse, *, expect='json'):
            def healthy(response):
                try:
                    parse(response)
                    return True
                except (AttributeError, KeyError, TypeError, ValueError):
                    return False
            response, source = collector.get(batch, 'hkexnews', 'stock.announcements', HOST + '/search/' + interface,
                params, expect=expect, healthy_response=healthy, evidence_mode='metadata_only')
            try:
                value = parse(response)
            except Exception as exc:
                parsed_source(db, source, error=type(exc).__name__)
                raise
            parsed_source(db, source, fields=['stockId'] if expect == 'text' else ['NEWS_ID', 'STOCK_CODE', 'DATE_TIME', 'FILE_LINK'])
            return value, source

        org, _ = request('prefix.do', {'lang': 'EN', 'type': 'A', 'name': code, 'market': 'SEHK',
                         'callback': 'callback'}, lambda response: issuer_id(response.text, code), expect='text')
        def window(left, right):
            params = {'stockId': org, 'fromDate': left.strftime('%Y%m%d'), 'toDate': right.strftime('%Y%m%d'),
                      'sortDir': 0, 'sortByOptions': 'DateTime', 'category': 0, 'market': 'SEHK', 'documentType': -1,
                      'title': '', 'searchType': 0, 't1code': -2, 't2Gcode': -2, 't2code': -2,
                      'rowRange': 2000, 'lang': 'EN'}
            rows, source = request(INTERFACE, params, lambda response: filings(response.json(), code, left.isoformat(), right.isoformat()))
            if rows is None:
                if left == right:
                    raise ValueError('披露易单日公告仍被截断')
                mid = left + (right - left) // 2
                return window(left, mid) + window(mid + timedelta(days=1), right)
            return [(row, source) for row in rows]

        rows = window(start, today)
        old_watermark = state.get('data_watermark')
        watermark = max((r['date'] for r, _ in rows), default=old_watermark)
        if old_watermark and watermark:
            watermark = max(old_watermark, watermark)
        coverage = min(state.get('coverage_start') or start.isoformat(), start.isoformat())
        db.complete_sync(run, SyncResult(len(rows), coverage, today.isoformat(), watermark, no_data=not watermark),
                         lambda conn: _publish(conn, identity, org, run, batch, rows),
                         collection_batch_id=batch)
    except Exception as exc:
        mark_failed(db, run, exc, collection_batch_id=batch)
        raise


def _publish(conn, identity, org, run, batch, rows):
    from stock_analysis.collection.publication_counts import counts, observe, published
    result = counts(len(rows))
    for row, source in rows:
        fingerprint = digest(row)
        old = conn.execute('SELECT id,metadata_version,metadata_fingerprint FROM stock_announcement_metadata '
            'WHERE instrument_id=? AND announcement_id=? ORDER BY metadata_version DESC LIMIT 1',
            (identity, row['id'])).fetchone()
        observe(result, old['metadata_fingerprint'] if old else None, fingerprint)
        if old and old['metadata_fingerprint'] == fingerprint:
            continue
        kind = row['category'] if row['category'] != 'dividend' else None
        conn.execute(
            'INSERT INTO stock_announcement_metadata(instrument_id,announcement_id,metadata_version,org_id,title,'
            'announcement_time,published_on,adjunct_url,period,report_type,title_category,revision_marker,periods_json,'
            'metadata_fingerprint,source_id,batch_id,run_id,obtained_at,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity, row['id'], old['metadata_version'] + 1 if old else 1, org, row['title'], row['time'],
             row['date'], row['url'], row['period'], kind, row['category'], 'revision' if row['revised'] else 'original',
             encoded([row['period']] if row['period'] else []), fingerprint, source, batch, run, utc_now(),
             old['id'] if old else None))
    periods = [row[0] for row in conn.execute('SELECT DISTINCT period FROM financial_reports WHERE instrument_id=?', (identity,))]
    for period in periods:
        candidates = conn.execute('SELECT * FROM stock_announcement_metadata WHERE instrument_id=? AND period=? '
            'AND report_type IS NOT NULL AND id IN (SELECT max(id) FROM stock_announcement_metadata WHERE '
            'instrument_id=? GROUP BY announcement_id) ORDER BY announcement_time,id', (identity, period, identity)).fetchall()
        original = next((r for r in candidates if r['revision_marker'] == 'original'), None)
        if not original:
            continue
        revision = next((r for r in reversed(candidates) if r['revision_marker'] == 'revision'
                         and r['announcement_time'] >= original['announcement_time']), None)
        old = conn.execute('SELECT * FROM report_disclosure_dates WHERE instrument_id=? AND period=? AND is_current=1',
                           (identity, period)).fetchone()
        refs = (original['id'], revision['id'] if revision else None)
        if old and refs == (old['original_announcement_row_id'], old['revision_announcement_row_id']):
            continue
        if old:
            conn.execute('UPDATE report_disclosure_dates SET is_current=0 WHERE id=?', (old['id'],))
        stamp = utc_now()
        conn.execute('INSERT INTO report_disclosure_dates(instrument_id,period,report_type,rule_version,result_version,'
            'first_published_on,latest_revision_on,status,reason,original_announcement_row_id,revision_announcement_row_id,'
            'candidates_json,is_current,previous_result_id,run_id,created_at,validated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)',
            (identity, period, original['report_type'], VERSION, old['result_version'] + 1 if old else 1,
             original['published_on'], revision['published_on'] if revision else None, 'confirmed', '港交所公告标题明确报告期',
             *refs, encoded([r['id'] for r in candidates]), old['id'] if old else None, run, stamp, stamp))
    return published(result)


def read(db, code):
    identity = instrument_id(db, code)
    with db.connection() as conn:
        rows = conn.execute('SELECT title,published_on,adjunct_url,title_category FROM stock_announcement_metadata '
            'WHERE instrument_id=? AND id IN (SELECT max(id) FROM stock_announcement_metadata WHERE instrument_id=? '
            'GROUP BY announcement_id) ORDER BY announcement_time DESC,id DESC', (identity, identity)).fetchall()
    return {'announcements': [{'title': row['title'], 'published_on': row['published_on'],
             'url': HOST + row['adjunct_url'], 'category': row['title_category']} for row in rows]}
