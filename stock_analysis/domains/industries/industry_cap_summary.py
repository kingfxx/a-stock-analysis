"""Versioned, local aggregation of quarterly market snapshots by industry."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


DEFINITION = 'market-snapshot-industry-cap-v1'
SOURCE_SCOPE = 'market_snapshot_industry_cap_aggregation'
PRIORITY = {
    'em-valueanalysis-v1': 3,
    'historical-suspended-preclose-v1': 2,
    'historical-composite-v1': 1,
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def current_aggregation_versions(conn):
    """Return the latest successfully published aggregation mapping per quarter."""
    current = {}
    for row in conn.execute("SELECT id,result_json FROM collection_operations WHERE action IS NOT NULL AND result_json IS NOT NULL ORDER BY id"):
        try:
            result = json.loads(row['result_json'])
        except (TypeError, json.JSONDecodeError):
            continue
        for quarter, item in (result.get('aggregation_results') or {}).items():
            if not isinstance(item, dict) or item.get('status') != 'complete':
                continue
            old = current.get(quarter)
            if old is None or (item.get('published_at') or '', row['id']) > (old.get('published_at') or '', old['_run_id']):
                current[quarter] = {**item, '_run_id': row['id']}
    return current


def _candidate(conn, quarter):
    roster = conn.execute('SELECT * FROM sw_cap_quarter_rosters WHERE quarter=?', (quarter,)).fetchone()
    if roster is None:
        raise ValueError(f'{quarter} 缺少已冻结季度成员名单')
    members = [dict(row) for row in conn.execute(
        'SELECT stock_code,industry_code FROM sw_cap_quarter_members WHERE quarter=? ORDER BY stock_code',
        (quarter,))]
    if not members:
        raise ValueError(f'{quarter} 冻结成员名单为空，不能发布行业汇总')
    snapshots = {}
    for row in conn.execute(
            "SELECT id,security_code,exchange,definition_version,total_market_cap,source_id,batch_id "
            "FROM market_trade_snapshot WHERE trade_date=? AND is_latest=1 "
            "AND exchange IN ('sh','sz') AND definition_version IN "
            "('em-valueanalysis-v1','historical-suspended-preclose-v1','historical-composite-v1') "
            'AND total_market_cap IS NOT NULL ORDER BY security_code,exchange,id',
            (roster['target_date'],)):
        old = snapshots.get(row['security_code'])
        rank = (PRIORITY[row['definition_version']], row['id'])
        if old is None or rank > old['_rank']:
            snapshots[row['security_code']] = {**dict(row), '_rank': rank}
    grouped = {}
    inputs = []
    unclassified = 0
    for member in members:
        stock = member['stock_code']
        snapshot = snapshots.get(stock)
        inputs.append((stock, member['industry_code'],
                       snapshot['id'] if snapshot else None,
                       snapshot['definition_version'] if snapshot else None,
                       snapshot['total_market_cap'] if snapshot else None))
        code = member['industry_code']
        if code is None:
            unclassified += 1
            continue
        item = grouped.setdefault(code, {'expected_count': 0, 'known_count': 0, 'values': []})
        item['expected_count'] += 1
        if snapshot:
            item['known_count'] += 1
            item['values'].append(snapshot['total_market_cap'])
    signature_members = [(item[0], item[1], item[3], item[4]) for item in inputs]
    input_payload = {
        'definition': DEFINITION,
        'quarter': quarter,
        'target_date': roster['target_date'],
        'composition': roster['composition'],
        'member_import_id': roster['member_import_id'],
        'members': signature_members,
    }
    signature = _digest(_json(input_payload))
    rows = []
    for code, item in sorted(grouped.items()):
        known_cap = sum(item['values']) if item['values'] else None
        total_cap = known_cap if item['known_count'] == item['expected_count'] and item['expected_count'] else None
        rows.append({
            'industry_code': code,
            'quarter': quarter,
            'trade_date': roster['target_date'],
            'target_date': roster['target_date'],
            'composition': roster['composition'],
            'expected_count': item['expected_count'],
            'known_count': item['known_count'],
            'known_cap': known_cap,
            'total_cap': total_cap,
        })
    manifest = {
        'scope': SOURCE_SCOPE,
        'definition_version': DEFINITION,
        'quarter': quarter,
        'target_date': roster['target_date'],
        'composition': roster['composition'],
        'roster_member_import_id': roster['member_import_id'],
        'input_signature': signature,
        'selected_snapshots': [
            {'id': item[2], 'definition_version': item[3], 'source_id': snapshots[item[0]]['source_id'],
             'batch_id': snapshots[item[0]]['batch_id']}
            for item in inputs if item[2] is not None
        ],
        'coverage': rows,
        'unclassified_count': unclassified,
    }
    return signature, rows, manifest


def publish_in_transaction(conn, quarter, run_id, operation_id):
    """Compute from committed snapshots and atomically publish rows plus run mapping."""
    signature, rows, manifest = _candidate(conn, quarter)
    active = current_aggregation_versions(conn).get(quarter)
    published_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    if active and active.get('input_signature') == signature:
        import_id = active['import_id']
        created = False
    else:
        existing = conn.execute(
            "SELECT id FROM sw_imports WHERE json_extract(source_manifest_json,'$.scope')=? "
            "AND json_extract(source_manifest_json,'$.quarter')=? "
            "AND json_extract(source_manifest_json,'$.input_signature')=? ORDER BY id DESC LIMIT 1",
            (SOURCE_SCOPE, quarter, signature)).fetchone()
        if existing:
            import_id = existing['id']
            created = False
        else:
            current = conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()
            if current is None or current['member_import_id'] is None:
                raise ValueError('无法确定当前行业名单版本，拒绝写入汇总审计版本')
            manifest.update(operation_id=operation_id, aggregation_imported_at=published_at,
                            snapshot_input_rows=len(manifest['selected_snapshots']))
            content_hash = _digest(_json({'scope': SOURCE_SCOPE, 'quarter': quarter, 'signature': signature}))
            import_id = conn.execute(
                "INSERT INTO sw_imports(obtained_at,content_hash,source_manifest_json,status,member_import_id) "
                "VALUES(?,?,?,'complete',?)",
                (published_at, content_hash, _json(manifest), current['member_import_id'])).lastrowid
            conn.executemany(
                'INSERT INTO sw_industry_cap_quarters '
                '(import_id,industry_code,quarter,trade_date,expected_count,known_count,known_cap,total_cap,target_date,composition) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                [(import_id, row['industry_code'], quarter, row['trade_date'], row['expected_count'],
                  row['known_count'], row['known_cap'], row['total_cap'], row['target_date'], row['composition'])
                 for row in rows])
            created = True
    run = conn.execute('SELECT result_json FROM collection_operations WHERE id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError(f'行业更新任务 {run_id} 不存在')
    try:
        result = json.loads(run['result_json'] or '{}')
    except json.JSONDecodeError:
        result = {}
    aggregation_results = result.setdefault('aggregation_results', {})
    aggregation_results[quarter] = {
        'status': 'complete',
        'import_id': import_id,
        'input_signature': signature,
        'published_at': published_at,
        'created': created,
        'row_count': len(rows),
        'unclassified_count': manifest['unclassified_count'],
    }
    result['aggregation_results'] = aggregation_results
    result['aggregation_message'] = f'{quarter} 行业市值汇总已发布'
    conn.execute('UPDATE collection_operations SET result_json=? WHERE id=?', (_json(result), run_id))
    return aggregation_results[quarter]


def aggregate_quarter(db, quarter, run_id, operation_id):
    with db.connection(write=True) as conn:
        return publish_in_transaction(conn, quarter, run_id, operation_id)
