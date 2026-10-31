"""Targeted missing-row financial collection; never revises an existing latest row."""
import json
import re

from stock_analysis.collection.collection import (
    begin_batch, finish_batch, parsed_source, read_source, reusable_source,
)
from stock_analysis.domains.market import market_financial as financial
from stock_analysis.domains.industries.industry_sources import EM

PARSER = 'market-financial-missing-v1'
CHUNK_SIZE = 200


def require_initialised(conn, period):
    if not any(conn.execute(f'SELECT 1 FROM market_financial_{kind} WHERE report_date=? AND is_latest=1 LIMIT 1',
                            (period,)).fetchone() for kind in financial.FIELDS):
        raise ValueError('该报告期尚未初始化，请先普通采集，再使用补缺')


def missing_plan(conn, period):
    require_initialised(conn, period)
    pointer = conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()
    member_id = pointer[0] if pointer else None
    universe = {(r[0], 'sh' if r[0].startswith('6') else 'sz') for r in conn.execute(
        'SELECT stock_code FROM sw_memberships WHERE import_id=?', (member_id,))}
    quarter_start = period[:5] + f'{((int(period[5:7])-1)//3)*3+1:02d}-01'
    snapshot_date = conn.execute('SELECT max(trade_date) FROM market_trade_snapshot WHERE is_latest=1 '
                                 'AND trade_date BETWEEN ? AND ?', (quarter_start, period)).fetchone()[0]
    if snapshot_date:
        universe.update(tuple(r) for r in conn.execute('SELECT security_code,exchange FROM market_trade_snapshot '
                        'WHERE trade_date=? AND is_latest=1 AND exchange IN (\'sh\',\'sz\',\'bj\')', (snapshot_date,)))
    known = {}
    for kind in financial.FIELDS:
        known[kind] = {tuple(r) for r in conn.execute(f'SELECT security_code,exchange FROM market_financial_{kind} '
                                                     'WHERE report_date=? AND is_latest=1', (period,))}
        universe.update(known[kind])
    universe = {key for key in universe if key[0]}
    return {'universe': universe, 'missing': {kind: sorted(universe - keys) for kind, keys in known.items()},
            'member_import_id': member_id, 'snapshot_date': snapshot_date}


def request_params(kind, period, keys):
    if (not 0 < len(keys) <= CHUNK_SIZE or len({code for code, _ in keys}) != len(keys)
            or any(not re.fullmatch(r'[0-9]{6}', code) or exchange not in {'sh','sz','bj'} for code, exchange in keys)):
        raise ValueError('补缺证券范围无效或超出单段容量')
    spec = financial.FIELDS[kind]
    field = spec.get('date_field', 'REPORT_DATE')
    codes = ','.join('"' + code + '"' for code, _ in keys)
    return {'reportName': spec['report'], 'columns': 'ALL',
            'filter': f'({field}=\'{period}\')(SECURITY_TYPE_CODE="058001001")(SECURITY_CODE in ({codes}))',
            'pageSize': 500, 'pageNumber': 1, 'sortColumns': 'SECURITY_CODE', 'sortTypes': '1',
            'source': 'WEB', 'client': 'WEB'}


def validate_response(payload, kind, period, keys):
    if payload.get('code') == 9201 and payload.get('message') == '返回数据为空':
        return []
    result = payload.get('result') or {}
    rows, count = result.get('data'), result.get('count')
    if (payload.get('success') is not True or not isinstance(rows, list) or type(count) is not int
            or count != len(rows) or count > len(keys) or result.get('pages') != (1 if count else 0)):
        raise ValueError('补缺响应分页或数量无效，未发布')
    expected, seen = set(keys), set()
    spec = financial.FIELDS[kind]
    date_field = spec.get('date_field', 'REPORT_DATE')
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(spec['fields']) or str(row.get(date_field, ''))[:10] != period:
            raise ValueError('补缺响应字段或报告期不匹配，未发布')
        _, exchange = financial.normalized(kind, row, period)
        key = (row['SECURITY_CODE'], exchange)
        if key not in expected or key in seen:
            raise ValueError('补缺返回越界或重复公司，未发布')
        seen.add(key)
    return rows


def collect_chunk(db, operation_id, collector, kind, period, keys, plan):
    dataset = 'market.financial.' + kind
    scope = {'mode': 'missing', 'period': period, 'securities': [list(k) for k in keys],
             'member_import_id': plan['member_import_id'], 'snapshot_date': plan['snapshot_date'],
             'columns': 'ALL', 'page_size': 500, 'report_name': financial.FIELDS[kind]['report']}
    params = request_params(kind, period, keys)
    batch = begin_batch(db, operation_id, dataset, 'eastmoney', period, period, scope=scope,
                        frequency='quarterly', parser_version=PARSER,
                        definition_version=financial.FIELDS[kind]['definition_version'], universe_version='local-known-a-share-v1')
    source = None
    try:
        # Reuse only fulfilled scopes. Partial/empty observations must be checked again.
        with db.connection() as conn:
            candidates = [dict(r) for r in conn.execute(
                'SELECT s.id,s.request_json FROM collection_sources s JOIN collection_batches b ON b.id=s.batch_id '
                "WHERE b.parser_version=? AND b.dataset=? AND b.scope_json=? AND b.source_system='eastmoney' "
                "AND json_extract(b.result_json,'$.full_scope')=1 AND s.parse_status='complete' "
                "AND s.request_status IN ('success','reused') AND s.file_path IS NOT NULL ORDER BY s.id DESC LIMIT 1",
                (PARSER, dataset, financial.encoded(scope)))]
        rows = None
        for candidate in candidates:
            try:
                request = json.loads(candidate['request_json'])
                if request.get('url') != EM or request.get('params') != params:
                    continue
                raw, _ = read_source(db, candidate['id'])
                cached = validate_response(json.loads(raw), kind, period, keys)
                if len(cached) == len(keys):
                    rows = cached
                    source = reusable_source(db, batch, candidate['id'])
            except (OSError, ValueError, TypeError, KeyError):
                continue
        if rows is None:
            response, source = collector.get(batch, 'eastmoney', dataset, EM, params,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'},
                healthy_response=lambda r: r.json().get('success') is True or r.json().get('code') == 9201,
                industry_profile='financial')
            rows = validate_response(response.json(), kind, period, keys)
            parsed_source(db, source, fields=sorted(financial.FIELDS[kind]['fields']) if rows else [])
        validation = {'validation_complete': True, 'full_scope': len(rows) == len(keys),
                      'requested_count': len(keys), 'rows': len(rows), 'mode': 'missing'}
        with db.connection(write=True) as conn:
            conn.execute('UPDATE collection_batches SET result_json=? WHERE id=?', (financial.encoded(validation), batch))
        return {'kind': kind, 'batch_id': batch, 'source_id': source, 'keys': keys, 'scope': scope,
                'params': params, 'rows': rows, 'rows_hash': financial.digest(financial.encoded(rows).encode()),
                'validation': validation}
    except BaseException as exc:
        if source is not None:
            parsed_source(db, source, error=str(exc))
        finish_batch(db, batch, 'failed', error=str(exc))
        raise


def publish_missing(conn, operation_id, period, chunks):
    summaries = {kind: {'rows': 0, 'new': 0, 'revised': 0, 'enriched': 0, 'unchanged': 0,
                       'published_count': 0, 'request_count': 0} for kind in financial.FIELDS}
    for chunk in chunks:
        kind, batch_id, source_id = chunk['kind'], chunk['batch_id'], chunk['source_id']
        batch = conn.execute('SELECT * FROM collection_batches WHERE id=?', (batch_id,)).fetchone()
        source = conn.execute('SELECT * FROM collection_sources WHERE id=?', (source_id,)).fetchone()
        if (batch is None or batch['status'] != 'running' or batch['parser_version'] != PARSER
                or batch['dataset'] != 'market.financial.' + kind or batch['source_system'] != 'eastmoney' or batch['domain'] != 'market'
                or batch['scope_start'] != period or batch['scope_end'] != period
                or batch['definition_version'] != financial.FIELDS[kind]['definition_version']
                or (batch['operation_id'] or batch['legacy_operation_ref']) != operation_id
                or batch['scope_json'] != financial.encoded(chunk['scope'])
                or source is None or source['batch_id'] != batch_id or source['dataset'] != batch['dataset']
                or source['source_system'] != 'eastmoney' or not source['file_path'] or not source['raw_sha256']
                or source['request_status'] not in ('success', 'reused') or source['parse_status'] != 'complete'):
            raise ValueError('补缺来源批次或范围校验失败')
        request = json.loads(source['request_json'])
        if (request.get('url') != EM or request.get('params') != chunk['params']
                or chunk['scope']['securities'] != [list(k) for k in chunk['keys']]
                or chunk['params'] != request_params(kind, period, chunk['keys'])
                or chunk['rows_hash'] != financial.digest(financial.encoded(chunk['rows']).encode())):
            raise ValueError('补缺请求或已校验数据发生变化')
        validate_response({'success': True, 'result': {'data': chunk['rows'], 'count': len(chunk['rows']),
                          'pages': 1 if chunk['rows'] else 0}}, kind, period, chunk['keys'])
        summary = summaries[kind]
        summary['rows'] += len(chunk['rows'])
        summary['request_count'] += 1
        inserted = skipped = 0
        for raw in chunk['rows']:
            values, exchange = financial.normalized(kind, raw, period)
            identity = (raw['SECURITY_CODE'], exchange, period)
            table = 'market_financial_' + kind
            if conn.execute(f'SELECT 1 FROM {table} WHERE security_code=? AND exchange=? AND report_date=? AND is_latest=1', identity).fetchone():
                skipped += 1
                continue
            version = conn.execute(f'SELECT coalesce(max(version),0)+1 FROM {table} WHERE security_code=? AND exchange=? AND report_date=?', identity).fetchone()[0]
            content_hash = financial.digest(financial.encoded(values).encode())
            values.update(exchange=exchange, report_date=period, version=version, is_latest=1,
                          batch_id=None, source_id=None, collection_batch_id=batch_id, collection_source_id=source_id,
                          content_hash=content_hash, definition_version=financial.FIELDS[kind]['definition_version'],
                          obtained_at=source['obtained_at'], updated_at=financial.now())
            conn.execute(f'INSERT INTO {table} (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            inserted += 1
        summary['new'] += inserted
        summary['unchanged'] += skipped
        summary['published_count'] += inserted
        result = {**chunk['validation'], 'new': inserted, 'unchanged': skipped, 'revised': 0, 'enriched': 0,
                  'published_count': inserted, 'remaining_requested': len(chunk['keys']) - len(chunk['rows'])}
        conn.execute("UPDATE collection_batches SET status='complete',coverage_status='unknown',finished_at=?,result_json=? WHERE id=?",
                     (financial.now(), financial.encoded(result), batch_id))
    return summaries


def run_missing(db, operation_id, period, collector, progress=lambda _: None, progress_state=None):
    with db.connection() as conn:
        plan = missing_plan(conn, period)
    chunks = []
    requested = {key for keys in plan['missing'].values() for key in keys}
    progress('补缺：仅请求缺失整行公司；已有字段空值不在本模式范围内')
    try:
        for kind in financial.FIELDS:
            keys = plan['missing'][kind]
            for offset in range(0, len(keys), CHUNK_SIZE):
                collector.check_cancelled()
                if progress_state:
                    progress_state({'kind': 'financial', 'phase': 'downloading', 'message':
                                    f"补缺 {financial.FIELDS[kind]['label']} · {offset}/{len(keys)} 公司"})
                chunks.append(collect_chunk(db, operation_id, collector, kind, period, keys[offset:offset+CHUNK_SIZE], plan))
        with db.connection(write=True) as conn:
            summaries = publish_missing(conn, operation_id, period, chunks)
            after = missing_plan(conn, period)
    except BaseException as exc:
        for chunk in chunks:
            finish_batch(db, chunk['batch_id'], 'failed', result=chunk['validation'], error=str(exc))
        raise
    remaining = {key for keys in after['missing'].values() for key in keys}
    inserted = sum(s['new'] for s in summaries.values())
    message = f'补缺完成：新增 {inserted} 条公司／接口记录；已知范围仍缺 {len(remaining)} 家'
    progress(message)
    if progress_state:
        progress_state({'kind': 'financial', 'phase': 'complete', 'message': message})
    return {'action': 'financial_period', 'target': period, 'mode': 'missing', 'message': message,
            'requested_count': len(requested), 'returned_count': len({(r['SECURITY_CODE'], r['SECUCODE'][-2:].lower()) for chunk in chunks for r in chunk['rows']}),
            'scope_count': len(plan['universe']), 'remaining_count': len(remaining),
            'missing_by_dataset': {kind: len(keys) for kind, keys in after['missing'].items()},
            'failures': [{'stock_code': code, 'error': '本地已知范围仍缺财务整行'} for code, _ in sorted(remaining)[:10]],
            'network_requests': collector.requests, 'collection_batch_ids': [c['batch_id'] for c in chunks],
            'statements': summaries, 'legacy_updated': False,
            'membership_check': {'status': 'local_only', 'member_import_id': plan['member_import_id'], 'snapshot_date': plan['snapshot_date']}}
