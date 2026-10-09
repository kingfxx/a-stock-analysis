"""Full-field Eastmoney summary statements, isolated from individual Sina reports."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import requests

from .industry_sources import EM
from .network import create_data_session

FIELDS = json.loads((Path(__file__).parent/'resources/market_financial_fields.json').read_text(encoding='utf-8'))
DESCRIPTIVE = {'SECUCODE', 'SECURITY_CODE', 'INDUSTRY_CODE', 'ORG_CODE', 'SECURITY_NAME_ABBR',
               'INDUSTRY_NAME', 'MARKET', 'SECURITY_TYPE_CODE', 'TRADE_MARKET_CODE',
               'TRADE_MARKET', 'SECURITY_TYPE', 'UPDATE_DATE', 'EITIME', 'PUBLISHNAME',
               'TRADE_MARKET_ZJG', 'ISNEW', 'BOARD_NAME', 'ORI_BOARD_CODE', 'BOARD_CODE'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(uuid4().hex[:12]+'.tmp')
    try:
        temp.write_bytes(raw)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def normalized(dataset, row, period):
    spec = FIELDS[dataset]
    unknown = set(row)-set(spec['fields'])
    if unknown:
        raise ValueError(f'{dataset} 出现未登记字段 {sorted(unknown)}；原始响应已保留，请更新字段映射')
    code, secu = row.get('SECURITY_CODE'), row.get('SECUCODE')
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
        raise ValueError('证券代码无效')
    if secu not in {code+'.SH', code+'.SZ', code+'.BJ'}:
        raise ValueError('证券代码与交易所标识不一致')
    if row.get('SECURITY_TYPE_CODE') != '058001001':
        raise ValueError('来源返回非 A 股记录')
    date_field=spec.get('date_field','REPORT_DATE')
    if not isinstance(row.get(date_field), str) or row[date_field][:10] != period:
        raise ValueError('来源报告期不匹配')
    values = {}
    for field, value in row.items():
        item = spec['fields'][field]
        if item['type'] == 'REAL':
            if value is None or value == '':
                value = None
            else:
                if isinstance(value, bool):
                    raise ValueError(f'{dataset}.{field} 数值无效')
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f'{dataset}.{field} 数值无效') from None
                if not math.isfinite(value):
                    raise ValueError(f'{dataset}.{field} 数值无效')
        elif value is not None:
            if isinstance(value, (dict, list, bool)):
                raise ValueError(f'{dataset}.{field} 文本无效')
            value = str(value)
        values[item['column']] = value
    return values, secu[-2:].lower()


def begin_batch(db, period):
    stamp = now()
    with db.connection(write=True) as conn:
        return conn.execute("INSERT INTO market_financial_batches(report_date,started_at,status,obtained_at) "
                            "VALUES(?,?,'running',?)", (period, stamp, stamp)).lastrowid


def fail_batch(db, batch_id, error):
    with db.connection(write=True) as conn:
        conn.execute("UPDATE market_financial_batches SET status='failed',finished_at=?,error=? "
                     "WHERE id=? AND status='running'", (now(), str(error), batch_id))


def collect_dataset(session, root, dataset, period, pacer, progress, *, checkpoint=True, resume=True):
    """Cache only interrupted same-day runs; successful runs discard checkpoints."""
    spec = FIELDS[dataset]
    report, columns, date_field = spec['report'], 'ALL', spec.get('date_field','REPORT_DATE')
    label = spec['label']
    root = Path(root)
    checkpoint_path = root/'checkpoints'/period/f'{date.today().isoformat()}-{dataset}.json'
    cached = {}
    if checkpoint and resume and checkpoint_path.exists():
        try:
            cached = json.loads(checkpoint_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            cached = {}
    pages, count, page = 1, None, 1
    rows, sources = [], []
    while page <= pages:
        params = {'reportName': report, 'columns': columns,
                  'filter': f'({date_field}=\'{period}\')(SECURITY_TYPE_CODE="058001001")',
                  'sortColumns': 'SECURITY_CODE', 'sortTypes': '1', 'pageSize': 500,
                  'pageNumber': page, 'source': 'WEB', 'client': 'WEB'}
        cache_key = digest(encoded({'params': params, 'parser': 'market-summary-v1'}).encode())
        source = cached.get(cache_key)
        raw = None
        if source:
            path = root/source['file_path']
            try:
                stored = path.read_bytes()
                raw = gzip.decompress(stored)
                if (digest(raw) != source.get('raw_sha256', source['sha256']) or
                        digest(stored) != source['sha256'] or source['params'] != params):
                    raw = None
            except (OSError, ValueError, EOFError):
                raw = None
        if raw is None:
            for attempt in range(3):
                try:
                    pacer.before_request()
                    response = session.get(EM, params=params, timeout=30)
                    response.raise_for_status()
                    raw = response.content
                    payload = json.loads(raw)
                    if not payload.get('success') and payload.get('code') != 9201:
                        raise ValueError(str(payload.get('message', '接口未成功返回')))
                    break
                except (requests.RequestException, ValueError, OSError):
                    if attempt == 2:
                        raise
                    progress(f'{label} 请求失败，冷却 {60*(attempt+1)} 秒')
                    time.sleep(60*(attempt+1))
            sha = digest(raw)
            relative = Path(period)/dataset/'responses'/(sha+'.json.gz')
            path = root/relative
            if path.exists():
                if digest(gzip.decompress(path.read_bytes())) != sha:
                    raise ValueError('已有原始响应哈希校验失败')
            else:
                compressed = gzip.compress(raw, mtime=0)
                if digest(gzip.decompress(compressed)) != sha:
                    raise ValueError('响应压缩校验失败')
                atomic_write(path, compressed)
            source = {'url': EM, 'params': params, 'sha256': digest(path.read_bytes()), 'raw_sha256': sha,
                      'file_path': relative.as_posix(),
                      'file': str(path.resolve()), 'compression': 'gzip', 'obtained_at': now(),
                      'raw_bytes': len(raw), 'stored_bytes': path.stat().st_size, 'page_number': page}
            cached[cache_key] = source
            if checkpoint:
                atomic_write(checkpoint_path, encoded(cached).encode('utf-8'))
        payload = json.loads(raw)
        result = payload.get('result') or {}
        if payload.get('code') == 9201 and payload.get('message') == '返回数据为空' and page == 1:
            progress(f'{label} {period} · 来源明确返回空，记录缺报')
            return {'dataset': dataset, 'rows': [], 'sources': [source], 'checkpoint': checkpoint_path}
        if not payload.get('success') or not isinstance(result.get('data'), list):
            raise ValueError(f'{label} 分页响应无效')
        if count is None:
            count, pages = result.get('count'), result.get('pages')
            if type(count) is not int or not 0 < count <= 20000 or pages != math.ceil(count/500):
                raise ValueError(f'{label} 范围或分页数量异常')
        if result.get('count') != count or result.get('pages') != pages:
            checkpoint_path.unlink(missing_ok=True)
            raise ValueError(f'{label} 分页数量变化，缓存已失效，请重试')
        if len(result['data']) != min(500, count-(page-1)*500):
            raise ValueError(f'{label} 分页记录不完整')
        for row in result['data']:
            if not isinstance(row.get(date_field), str) or row[date_field][:10] != period:
                raise ValueError(f'{label} 报告期不匹配')
            normalized(dataset, row, period)
            if set(row) != set(spec['fields']):
                raise ValueError(f'{label} 返回字段缺失，原始响应已保留，请核对接口结构')
        rows.extend(result['data'])
        sources.append(source)
        progress(f'{label} {page}/{pages} 页 · 已获取 {len(rows)}/{count} 家')
        page += 1
    if len({r['SECURITY_CODE'] for r in rows}) != len(rows):
        raise ValueError(f'{label} 股票重复，未发布')
    return {'dataset': dataset, 'rows': rows, 'sources': sources, 'checkpoint': checkpoint_path}


def collect(root, period, pacer, progress, *, resume=False):
    with create_data_session() as session:
        session.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'})
        return [collect_dataset(session, root, dataset, period, pacer, progress, resume=resume)
                for dataset in FIELDS]


def publish(conn, batch_id, period, datasets):
    """Caller owns the transaction, including the legacy industry publication."""
    batch = conn.execute('SELECT * FROM market_financial_batches WHERE id=?', (batch_id,)).fetchone()
    if batch is None or batch['status'] != 'running' or batch['report_date'] != period:
        raise ValueError('采集批次状态不适用')
    if {d['dataset'] for d in datasets} != set(FIELDS) or len(datasets) != len(FIELDS):
        raise ValueError('必须包含三表及业绩指标，未发布')
    summaries = {}
    for data in datasets:
        dataset = data['dataset']
        source_ids = []
        for source in data['sources']:
            page_offset=(source['page_number']-1)*500
            page_fields=sorted(data['rows'][page_offset]) if page_offset<len(data['rows']) else []
            source_ids.append(conn.execute('INSERT INTO market_financial_sources '
                '(batch_id,dataset,page_number,url,params_json,file_path,content_hash,raw_bytes,stored_bytes,obtained_at,fields_json) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (batch_id, dataset, source['page_number'], source['url'], encoded(source['params']),
                 source['file_path'], source.get('raw_sha256', source['sha256']), source['raw_bytes'], source['stored_bytes'],
                 source['obtained_at'], encoded(page_fields))).lastrowid)
        summary = {'rows': len(data['rows']), 'new': 0, 'revised': 0, 'enriched': 0, 'unchanged': 0,
                   'raw_bytes': sum(s['raw_bytes'] for s in data['sources']),
                   'stored_bytes': sum(s['stored_bytes'] for s in data['sources'])}
        summaries[dataset] = summary
        spec = FIELDS[dataset]
        table = 'market_financial_'+dataset
        old = {(r['security_code'], r['exchange']): dict(r) for r in conn.execute(
            f'SELECT f.*,s.fields_json FROM {table} f JOIN market_financial_sources s ON s.id=f.source_id '
            'WHERE f.report_date=? AND f.is_latest=1', (period,))}
        if old and len(data['rows']) < len(old)*.8:
            raise ValueError(f'{spec["label"]} 较同报告期已有覆盖骤减超过20%，未发布')
        seen = set()
        for index, row in enumerate(data['rows']):
            values, exchange = normalized(dataset, row, period)
            key = (row['SECURITY_CODE'], exchange)
            if key in seen:
                raise ValueError('证券报告期重复')
            seen.add(key)
            source_id = source_ids[index//500]
            source = data['sources'][index//500]
            before = old.get(key)
            content_hash = digest(encoded(values).encode())
            revised, enriched = False, False
            if before:
                old_fields = set(json.loads(before['fields_json']))
                for field in (old_fields | set(row))-DESCRIPTIVE:
                    column = spec['fields'][field]['column']
                    previous, current = before[column], values.get(column)
                    if previous != current:
                        if previous is None and current is not None:
                            enriched = True
                        else:
                            revised = True
                revised |= before['definition_version'] != spec['definition_version']
            stamp = now()
            if before and not revised:
                summary['enriched' if enriched else 'unchanged'] += 1
                columns = list(values)
                conn.execute(f'UPDATE {table} SET '+','.join(c+'=?' for c in columns)+
                    ',source_id=?,batch_id=?,content_hash=?,updated_at=? WHERE id=?',
                    (*values.values(), source_id, batch_id, content_hash, stamp, before['id']))
            else:
                summary['revised' if before else 'new'] += 1
                if before:
                    conn.execute(f'UPDATE {table} SET is_latest=0 WHERE id=?', (before['id'],))
                values.update(exchange=exchange, report_date=period, version=before['version']+1 if before else 1,
                    is_latest=1, batch_id=batch_id, source_id=source_id, content_hash=content_hash,
                    definition_version=spec['definition_version'],
                    obtained_at=source['obtained_at'], updated_at=stamp)
                conn.execute(f'INSERT INTO {table} ('+','.join(values)+') VALUES ('+
                             ','.join('?' for _ in values)+')', tuple(values.values()))
        summary['missing_previous'] = len(set(old)-seen)
    conn.execute("UPDATE market_financial_batches SET status='complete',finished_at=?,result_json=? WHERE id=?",
                 (now(), encoded(summaries), batch_id))
    return summaries


def clear_checkpoints(datasets):
    for data in datasets:
        try:
            Path(data['checkpoint']).unlink(missing_ok=True)
        except OSError:
            # Publishing has committed; cleanup failure cannot invalidate it.
            # New successful-run rechecks do not load this leftover pointer.
            data['checkpoint_cleanup_failed'] = True
