"""Individual-stock MAINOP history: validated pages and immutable business revisions."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from stock_analysis.infrastructure.network import create_data_session
from stock_analysis.domains.stocks.source_parsers import normalize_code
from stock_analysis.infrastructure.storage import SyncKey, SyncResult
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch, routed_stock_requests
from stock_analysis.domains.stocks.sync_support import audit_due, backup_before_update, data_lock, mark_failed, next_audit, recently_checked, recent_sync_failure, sync_state

SOURCE = 'eastmoney:RPT_F10_FN_MAINOP'
DATASET = 'business_segments'
DEFINITION = 'mainop-v1'
URL = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
FIELDS = {'revenue': 'MAIN_BUSINESS_INCOME', 'cost': 'MAIN_BUSINESS_COST',
          'gross_profit': 'MAIN_BUSINESS_RPOFIT', 'revenue_share': 'MBI_RATIO',
          'cost_share': 'MBC_RATIO', 'profit_share': 'MBR_RATIO', 'gross_margin': 'GROSS_RPOFIT_RATIO'}


def today_shanghai():
    return datetime.now(timezone(timedelta(hours=8))).date()


def ten_year_floor(today):
    try:
        return today.replace(year=today.year - 10)
    except ValueError:
        return today.replace(year=today.year - 10, day=28)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def number(value):
    if value is None or value == '' or value == '--':
        return None
    if isinstance(value, bool):
        raise ValueError('主营构成包含布尔数值')
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('主营构成数值无效') from exc
    if not parsed.is_finite():
        raise ValueError('主营构成包含非有限数值')
    if parsed == 0:
        return '0'
    # Decimal normalization without the context precision rounding long source values.
    token = format(parsed, 'f')
    return token.rstrip('0').rstrip('.') if '.' in token else token


def parse_rows(rows, secucode, start, end):
    result = []
    for raw in rows:
        if not isinstance(raw, dict) or raw.get('SECUCODE') != secucode:
            raise ValueError('主营构成股票身份不匹配')
        period = date.fromisoformat(str(raw.get('REPORT_DATE', ''))[:10]).isoformat()
        if not start <= period <= end:
            raise ValueError('主营构成报告期越界')
        dimension = str(raw.get('MAINOP_TYPE'))
        name = raw.get('ITEM_NAME')
        if dimension not in ('1', '2', '3') or not isinstance(name, str) or not name.strip():
            raise ValueError('主营构成分类或名称无效')
        code = raw.get('ITEM_CODE')
        code = str(code) if code not in (None, '') else None
        rank = raw.get('RANK')
        if rank is not None:
            rank_token = number(rank)
            if rank_token is None or Decimal(rank_token) != int(Decimal(rank_token)):
                raise ValueError('主营构成排序无效')
            rank = int(Decimal(rank_token))
        result.append({'report_date': period, 'report_name': raw.get('REPORT_NAME'),
                       'dimension_type': dimension, 'item_code': code, 'item_name': name,
                       'item_key': encoded([code, name]), 'display_rank': int(rank) if rank is not None else None,
                       **{field: number(raw.get(source)) for field, source in FIELDS.items()}})
    return result


def business_content(rows):
    return sorted(({k: v for k, v in row.items() if k not in
                    ('display_rank', 'source_id', 'obtained_at')} for row in rows),
                  key=lambda row: (row['report_date'], row['dimension_type'], row['item_key']))


class BusinessSegmentService:
    def __init__(self, db, *, collector=None):
        self.db, self.collector = db, collector

    def _collect(self, code, start, end):
        secucode = code + ('.SH' if code.startswith('6') else '.SZ')
        params = {'reportName': 'RPT_F10_FN_MAINOP', 'columns': 'ALL',
                  'filter': f'(SECUCODE="{secucode}")(REPORT_DATE>=\'{start}\')(REPORT_DATE<=\'{end}\')',
                  'pageSize': 500, 'sortColumns': 'REPORT_DATE,MAINOP_TYPE,RANK,ITEM_NAME,ITEM_CODE',
                  'sortTypes': '1,1,1,1,1', 'source': 'HSF10', 'client': 'PC'}
        with create_data_session() as session:
            def page(index):
                response = session.get(URL, params={**params, 'pageNumber': index}, timeout=30)
                payload = response.json(parse_float=Decimal)
                result = payload.get('result') if isinstance(payload, dict) else None
                if isinstance(payload, dict) and payload.get('code') == 9201 and result is None:
                    result = {'count': 0, 'pages': 0, 'data': []}
                    payload = {**payload, 'success': True}
                if payload.get('success') is not True or not isinstance(result, dict):
                    raise ValueError('主营构成来源未返回有效分页结果')
                count, pages, raw = result.get('count'), result.get('pages'), result.get('data')
                if type(count) is not int or type(pages) is not int or count < 0 or not isinstance(raw, list):
                    raise ValueError('主营构成分页结构无效')
                if pages not in ((0, 1) if count == 0 else (max(1, (count + 499) // 500),)):
                    raise ValueError('主营构成总页数不符')
                if len(raw) != min(500, max(0, count - (index - 1) * 500)):
                    raise ValueError('主营构成分页截断')
                rows = parse_rows(raw, secucode, start, end)
                source = session.source_ids[-1]
                with self.db.connection() as conn:
                    obtained = conn.execute('SELECT obtained_at FROM collection_sources WHERE id=?', (source,)).fetchone()[0]
                if not obtained:
                    raise ValueError('主营构成请求未记录实际取得时间')
                return rows, count, pages, source, obtained

            first, count, pages, source, obtained = page(1)
            collected = []
            seen = set()
            for index in range(1, max(1, pages) + 1):
                if index == 1:
                    rows = first
                else:
                    rows, current_count, current_pages, source, obtained = page(index)
                    if (current_count, current_pages) != (count, pages):
                        raise ValueError('主营构成分页总数变化')
                for row in rows:
                    key = (row['report_date'], row['dimension_type'], row['item_key'])
                    if key in seen:
                        raise ValueError('主营构成重复项目或跨页重复')
                    seen.add(key)
                    collected.append({**row, 'source_id': source, 'obtained_at': obtained})
            if pages > 1:
                check, check_count, check_pages, _, _ = page(1)
                if (check_count, check_pages) != (count, pages) or business_content(check) != business_content(first):
                    raise ValueError('主营构成分页期间首页变化')
        return collected, {'row_count': count, 'pages': pages, 'query_start': start, 'query_end': end,
                           'report_dates': sorted({r['report_date'] for r in collected}),
                           'missing_fields': {field: sum(r[field] is None for r in collected) for field in FIELDS}}

    def _saved(self, identity):
        with self.db.connection() as conn:
            versions = [dict(r) for r in conn.execute('SELECT * FROM business_segment_versions WHERE instrument_id=? AND source=? AND is_current=1 ORDER BY report_date', (identity, SOURCE))]
            for version in versions:
                version['items'] = [dict(r) for r in conn.execute('SELECT * FROM business_segment_items WHERE version_id=? ORDER BY dimension_type,item_key', (version['id'],))]
        return versions

    def read(self, code, *, start=None, end=None):
        code = normalize_code(code)
        today = today_shanghai()
        start = date.fromisoformat(start).isoformat() if start else ten_year_floor(today).isoformat()
        end = date.fromisoformat(end).isoformat() if end else today.isoformat()
        if start > end:
            raise ValueError('主营构成查询范围倒置')
        with self.db.connection() as conn:
            instrument = conn.execute('SELECT id,name FROM instruments WHERE code=?', (code,)).fetchone()
        if not instrument:
            return {'code': code, 'name': None, 'periods': [], 'sync': {}, 'warnings': []}
        key = SyncKey(instrument['id'], DATASET, SOURCE)
        state = sync_state(self.db, key)
        with self.db.connection() as conn:
            last = conn.execute('SELECT status,error,finished_at FROM sync_runs WHERE instrument_id=? AND dataset=? AND source=? AND adjustment=? ORDER BY id DESC LIMIT 1', key.values()).fetchone()
            latest_report = conn.execute('SELECT max(period) FROM financial_reports WHERE instrument_id=?', (instrument['id'],)).fetchone()[0]
        periods = []
        for version in self._saved(instrument['id']):
            if not start <= version['report_date'] <= end:
                continue
            dimensions = {kind: [] for kind in ('1', '2', '3')}
            for item in version['items']:
                dimensions[item['dimension_type']].append({**item, **{field: float(Decimal(item[field])) if item[field] is not None else None for field in FIELDS}})
            periods.append({k: v for k, v in version.items() if k != 'items'} | {'dimensions': dimensions})
        warnings = ['主营构成为来源实际累计报告期；行业、产品、地区不能跨维度加总，跨期分类未自动映射。']
        if latest_report and (not state.get('data_watermark') or latest_report > state['data_watermark']):
            warnings.append(f'本地财报最新期为{latest_report}，主营构成尚无对应记录；来源可能尚未更新，也可能未披露该期构成。')
        if last and last['status'] == 'failed':
            warnings.append('主营构成最近检查失败，保留已存版本：' + str(last['error']))
        return {'code': code, 'name': instrument['name'], 'periods': periods,
                'sync': {**state, 'status': last['status'] if last else 'uninitialized',
                         'error': last['error'] if last else None}, 'warnings': warnings}

    def update(self, code, *, refresh=False, full=False):
        code = normalize_code(code)
        trigger = 'manual_full' if full else 'manual_refresh' if refresh else 'automatic'
        with data_lock(self.db, DATASET, code):
            identity = self.db.ensure_instrument(code)
            key = SyncKey(identity, DATASET, SOURCE)
            state = sync_state(self.db, key)
            if not (refresh or full) and (recent_sync_failure(self.db, key) or
                    (recently_checked(state, 86400) and not audit_due(state, DATASET))):
                return self.read(code)
            backup_before_update(self.db)
            today = today_shanghai()
            floor, end = ten_year_floor(today).isoformat(), today.isoformat()
            saved = self._saved(identity)
            periods = [v['report_date'] for v in saved if floor <= v['report_date'] <= end]
            full = bool(full or not state.get('succeeded_at') or audit_due(state, DATASET))
            start = floor if full or not periods else periods[-2] if len(periods) > 1 else periods[-1]
            run = self.db.start_sync(key, parser_version=DEFINITION, methodology_version=DEFINITION,
                                     requested_start=start, requested_end=end, trigger_reason='full' if full else 'refresh')
            batch = None
            try:
                batch = begin_stock_batch(self.db, run, 'eastmoney', DATASET, code, start=start, end=end,
                                          trigger=trigger,
                                          scope={'source_interface': 'RPT_F10_FN_MAINOP', 'mode': 'full' if full else 'incremental'},
                                          parser_version=DEFINITION, definition_version=DEFINITION)
                with routed_stock_requests(self.db, batch, 'eastmoney', DATASET, collector=self.collector):
                    rows, summary = self._collect(code, start, end)
                by_period = {}
                for row in rows:
                    by_period.setdefault(row['report_date'], []).append(row)
                for old in saved:
                    if not start <= old['report_date'] <= end:
                        continue
                    fresh = {(r['dimension_type'], r['item_key']): r for r in by_period.get(old['report_date'], [])}
                    for item in old['items']:
                        candidate = fresh.get((item['dimension_type'], item['item_key']))
                        if candidate is None or any(item[f] is not None and candidate[f] is None for f in FIELDS):
                            raise ValueError('主营构成已有项目或有效字段消失，保留旧事实待核实')
                summary['sync_run_id'] = run
                old_by_period = {v['report_date']: v for v in saved}
                def write(conn):
                    from stock_analysis.collection.publication_counts import counts, observe, published
                    counters = counts(len(rows))
                    for period, items in sorted(by_period.items()):
                        names = {r['report_name'] for r in items}
                        if len(names) != 1:
                            raise ValueError('主营构成同一期报告名称冲突')
                        fingerprint = hashlib.sha256(encoded({'definition': DEFINITION, 'items': business_content(items)}).encode()).hexdigest()
                        old = old_by_period.get(period)
                        if old and old['content_hash'] == fingerprint:
                            counters['unchanged'] += len(items)
                            continue
                        previous_items = {(r['dimension_type'],r['item_key']):r for r in old['items']} if old else {}
                        fields = ('item_code','item_name',*FIELDS)
                        for item in items:
                            previous = previous_items.get((item['dimension_type'],item['item_key']))
                            observe(counters, (old['report_name'], *(previous[f] for f in fields)) if previous else None,
                                    (items[0]['report_name'], *(item[f] for f in fields)))
                        if old:
                            conn.execute('UPDATE business_segment_versions SET is_current=0 WHERE id=?', (old['id'],))
                        version = conn.execute('INSERT INTO business_segment_versions(instrument_id,source,report_date,report_name,revision,supersedes_id,is_current,content_hash,definition_version,row_count,run_id,batch_id,obtained_at) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?)',
                            (identity, SOURCE, period, items[0]['report_name'], old['revision'] + 1 if old else 1,
                             old['id'] if old else None, fingerprint, DEFINITION, len(items), run, batch, max(r['obtained_at'] for r in items))).lastrowid
                        columns = ('dimension_type', 'item_key', 'item_code', 'item_name', *FIELDS, 'display_rank', 'source_id')
                        for item in items:
                            conn.execute('INSERT INTO business_segment_items(version_id,' + ','.join(columns) + ',batch_id) VALUES (' + ','.join('?' for _ in range(len(columns) + 2)) + ')',
                                         (version, *(item[c] for c in columns), batch))
                    return published(counters)
                actual = sorted({v['report_date'] for v in saved} | set(by_period))
                coverage_start = min(start, state.get('coverage_start') or start)
                coverage_end = max(end, state.get('coverage_end') or end)
                self.db.complete_sync(run, SyncResult(len(rows), coverage_start, coverage_end,
                    actual[-1] if actual else None, no_data=not actual,
                    next_full_audit_at=next_audit(DATASET) if full else state.get('next_full_audit_at')), write,
                    collection_batch_id=batch, batch_result=summary)
            except Exception as exc:
                mark_failed(self.db, run, exc, collection_batch_id=batch)
            return self.read(code)
