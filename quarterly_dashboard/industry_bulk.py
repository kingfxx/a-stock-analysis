"""Explicit, resumable SSE/SZSE industry history; no individual refresh hooks."""
from __future__ import annotations

import hashlib
import json
import math
import time
import random
from datetime import date, datetime, timezone
from pathlib import Path

from .industry_service import IndustryService, dumps
from .industry_sources import EM, FINANCIAL_COLUMNS, attach_financial_source, financial_rows, number
from .industry_updates import cap_roster, quarter_end, target_trade_date
from .network import create_data_session


def recent_quarters(today, years=1):
    current = today.year * 4 + (today.month - 1) // 3
    return [f'{n // 4}Q{n % 4 + 1}' for n in range(current - 4 * years, current)]


class RequestPacer:
    """Serial requests, jitter, periodic rest, and bounded retry backoff."""
    def __init__(self, progress=print):
        self.requests = 0
        self.progress = progress

    def before_request(self):
        if self.requests and self.requests % 20 == 0:
            self.progress('阶段休息 30 秒（每 20 个网络请求）')
            time.sleep(30)
        time.sleep(random.uniform(2.5, 3.5))
        self.requests += 1


def fetch_pages(session, directory, report, columns, filter_, progress, pacer=None, cache_directory=None):
    """Cache exact requests; reject changing totals, missing pages and duplicates."""
    params = {'reportName': report, 'columns': columns, 'filter': filter_,
              'sortColumns': 'SECURITY_CODE', 'sortTypes': '1',
              'pageSize': 500, 'pageNumber': 1, 'source': 'WEB', 'client': 'WEB'}
    rows, files, count, pages = [], [], None, 1
    page = 1
    while page <= pages:
        params = {**params, 'pageNumber': page}
        key = hashlib.sha256(dumps(params).encode()).hexdigest()
        path = directory / (key + '.json')
        if not path.exists() and cache_directory and (Path(cache_directory) / path.name).exists():
            path = Path(cache_directory) / path.name
        if path.exists():
            saved = json.loads(path.read_text(encoding='utf-8'))
            if saved['params'] != params:
                raise ValueError('缓存请求不匹配')
            payload = saved['response']
        else:
            for attempt in range(3):
                try:
                    if pacer:
                        pacer.before_request()
                    response = session.get(EM, params=params, timeout=30)
                    response.raise_for_status()
                    payload = response.json()
                    # Eastmoney explicitly reports an empty query as code 9201.
                    if payload.get('code') == 9201 and payload.get('message') == '返回数据为空':
                        break
                    if not payload.get('success') or not payload.get('result'):
                        raise ValueError(str(payload.get('message', '来源为空')))
                    break
                except (ValueError, OSError) as exc:
                    if attempt == 2:
                        raise ValueError(f'{report} 读取失败：{exc}') from exc
                    progress(f'来源请求失败，冷却 {60 * (attempt + 1)} 秒：{exc}')
                    time.sleep(60 * (attempt + 1))
                finally:
                    if not pacer:
                        time.sleep(1.5)
            path.write_text(dumps({'params': params, 'response': payload}), encoding='utf-8')
        result = payload.get('result') or {}
        evidence = {'file': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'url': EM, 'params': params}
        if payload.get('code') == 9201 and payload.get('message') == '返回数据为空':
            if page != 1:
                raise ValueError('非首分页突然为空，未发布')
            progress(f'{report} {filter_} · 来源明确返回空，留空')
            return [], [evidence]
        if not payload.get('success') or not isinstance(result.get('data'), list):
            raise ValueError('分页响应无效')
        if count is None:
            count, pages = result['count'], result['pages']
            if not 0 < count <= 10000 or pages != math.ceil(count / 500):
                raise ValueError('单期范围或分页数量异常')
        if result['count'] != count or result['pages'] != pages:
            raise ValueError('来源分页期间数量变化；请换新目录重试')
        expected = min(500, count - (page - 1) * 500)
        if len(result['data']) != expected:
            raise ValueError('分页记录数量不完整')
        rows.extend(result['data'])
        files.append(evidence)
        progress(f'{report} {filter_} · {page}/{pages} · {len(rows)}/{count}')
        page += 1
    if len({r['SECURITY_CODE'] for r in rows}) != count:
        raise ValueError('来源股票重复，未发布')
    return rows, files


def cap_rows(rows, target, universe, source):
    facts = []
    for row in rows:
        if row['TRADE_DATE'][:10] != target:
            raise ValueError('返回市值日期不匹配')
        if row['SECURITY_CODE'] not in universe:
            continue
        value = number(row.get('TOTAL_MARKET_CAP'))
        if value is None or value <= 0:
            continue
        shares, close = number(row.get('TOTAL_SHARES')), number(row.get('CLOSE_PRICE'))
        if shares is not None and close is not None and abs(value - shares * close) > max(1, value * 1e-6):
            raise ValueError('总市值与总股本 × 不复权收盘价不一致')
        facts.append({'stock_code': row['SECURITY_CODE'], 'trade_date': target, 'total_cap': value,
                      'provenance': {**source.get(row['SECURITY_CODE'], source), 'source': 'eastmoney:RPT_VALUEANALYSIS_DET',
                                     'field': 'TOTAL_MARKET_CAP', 'source_unit': '元',
                                     'method': '来源总市值直接取数', 'source_date': target}})
    return facts


def collect(service, directory, *, today=None, progress=print, years=1, cache_directory=None):
    """Retired mixed collector; preserve legacy parsers/imports for offline audits."""
    raise ValueError('旧财务/市值混合集采入口已停用：市值全字段使用 python -m quarterly_dashboard.market_trade_snapshot；财务使用 market_financial_backfill。旧数据及离线导入工具保留。')


def publish(service, collection):
    foundation = collection['foundation']
    ids = []
    now = datetime.now(timezone.utc).isoformat()
    with service.db.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM sw_update_runs WHERE status='running' LIMIT 1").fetchone():
            raise ValueError('已有行业任务运行，请稍后发布')
        run_id = conn.execute("INSERT INTO sw_update_runs(action,target,recheck,started_at,status) "
                              "VALUES(?,?,0,?,'running')", ('bulk_history' if collection['manifest'].get('years', 1) > 1
                              else 'bulk_one_year', collection['asof'], now)).lastrowid
    try:
        for index, cap in enumerate(collection['caps']):
            bundle = {'asof': collection['asof'], 'obtained_at': collection['obtained_at'],
                      'taxonomy': foundation['taxonomy'], 'members': foundation['members'],
                      'membership_history': foundation['membership_history'],
                      'financials': collection['financials'] if index == 0 else [], 'caps': cap['rows'],
                      'cap_roster': cap['roster'], 'manifest': collection['manifest']}
            ids.append(service.import_bundle(bundle, capture_local=False))
        with service.db.connection(write=True) as conn:
            conn.execute("UPDATE sw_update_runs SET status='complete',finished_at=?,result_json=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), dumps({'import_ids': ids,
                          'checks': collection['manifest'].get('checks', [])}), run_id))
    except Exception as exc:
        with service.db.connection(write=True) as conn:
            conn.execute("UPDATE sw_update_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), str(exc), run_id))
        raise
    return ids


if __name__ == '__main__':
    import argparse
    from .storage import Database, DEFAULT_DATABASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--publish', action='store_true', help='采集验证后备份并发布到行业表')
    parser.add_argument('--years', type=int, default=1)
    parser.add_argument('--cache-directory', type=Path)
    parser.add_argument('--asof', type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    db = Database(args.database)
    service = IndustryService(db)
    result = collect(service, args.directory, today=args.asof, years=args.years,
                     cache_directory=args.cache_directory, progress=lambda m: print(m, flush=True))
    if args.publish:
        backup = db.backup(db.path.parent / 'backups' / ('before-sw-bulk-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3'))
        ids = publish(service, result)
        report = {'backup': str(backup), 'import_ids': ids, 'checks': result['manifest']['checks']}
        (args.directory / 'publication.json').write_text(dumps(report), encoding='utf-8')
        print(dumps(report), flush=True)
