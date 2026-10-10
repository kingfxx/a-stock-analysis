"""Sparse quarter prices appended to the daily cap task, with isolated SDK runtime."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import market_price_backfill as backfill


def sdk_error(message):
    text = message.lower()
    if any(word in text for word in ('quota', 'limit exceeded', '额度', '配额')):
        return 'BigQuant 数据查询额度不足，请检查账户额度后重试'
    if any(word in text for word in ('permission', 'forbidden', 'unauthorized', 'access denied',
                                     '401', '403', 'expired', 'expire', '权限', '授权', '过期',
                                     'access key', 'authentication', 'invalid ak', 'invalid sk')):
        return 'BigQuant 访问被拒绝或授权已过期，请检查账户及 cn_stock_real_bar1d 数据表权限后重试'
    if any(word in text for word in ('凭据', 'filenotfounderror')):
        return 'BigQuant 凭据未配置或无效，请检查 data/credentials/bigquant.txt 后重试'
    return 'BigQuant 股价查询失败，请检查 SDK 环境、网络及数据表访问权限后重试'


def run_sdk(folder):
    runtime = backfill.ROOT/'data/verification/tools/bigquant-runtime/Scripts/python.exe'
    if not runtime.is_file():
        raise ValueError('BigQuant SDK 环境缺失，请配置 data/verification/tools/bigquant-runtime 后重试')
    try:
        process = subprocess.run([str(runtime), '-m', 'quarterly_dashboard.industry_prices', str(folder)],
                                 cwd=backfill.ROOT, capture_output=True, text=True, encoding='utf-8',
                                 timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ValueError('BigQuant 股价查询超时，请稍后重试') from None
    # Never expose subprocess diagnostics or SDK tracebacks to the UI.
    try:
        result = json.loads(process.stdout)
    except (ValueError, TypeError):
        raise ValueError('BigQuant SDK 未返回有效结果，请检查 SDK 环境及网络后重试') from None
    if process.returncode or not result.get('ok'):
        raise ValueError(result.get('error') or 'BigQuant 股价查询失败')


def sync_quarter(service, foundation, quarter_end, trade_date, stocks, recheck, progress):
    try:
        return _sync_quarter(service, foundation, quarter_end, trade_date, stocks, recheck, progress)
    except Exception as exc:
        return {'status':'failed', 'error':str(exc)}


def _sync_quarter(service, foundation, quarter_end, trade_date, stocks, recheck, progress):
    universe = set(stocks)
    members = [m for m in foundation['members'] if m['stock_code'] in universe]
    if {m['stock_code'] for m in members} != universe:
        raise ValueError('季末名单与当前分类名单不一致，股价未写入，请核对分类资料')
    with service.db.connection() as conn:
        known = {r['security_code'] for r in conn.execute(
            'SELECT security_code FROM market_quarterly_prices WHERE quarter_end=? AND trade_date=?',
            (quarter_end, trade_date))}
        member_id = conn.execute('SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1').fetchone()[0]
    if not recheck and universe <= known:
        return {'status':'complete', 'inserted':0, 'skipped':len(stocks), 'missing':0, 'reused':True}
    progress(f'BigQuant 季末未复权股价 · {trade_date} · 批量查询全市场')
    folder = backfill.ROOT/'data/verification/samples/quarterly-price-updates'/backfill.utc_now().replace(':','').replace('+','')
    folder.mkdir(parents=True)
    backfill.save(folder/'inventory.json', {'member_import_id':member_id, 'members':members,
        'targets':[{'quarter_end':quarter_end, 'trade_date':trade_date}], 'calendar_sources':[]})
    try:
        run_sdk(folder)
        candidate_path = folder/'candidate.json'
        # Revalidate archived responses in the parent before publishing.
        backfill.prepare(folder)
        candidate = backfill.load(candidate_path)
        result = backfill.publish(service.db, candidate, backfill.digest(candidate_path))
        missing = sum(c['missing'] for c in result['coverage'])
        result.update(status='complete', missing=missing)
        backfill.save(folder/'result.json', result)
        return result
    except Exception as exc:
        result = {'status':'failed', 'error':str(exc)}
        backfill.save(folder/'result.json', result)
        return result
    finally:
        for name in ('candidate.json', 'missing.json'):
            (folder/name).unlink(missing_ok=True)


def price_status(result):
    price = result.get('quarter_prices')
    if not price:
        return ''
    if price['status']=='failed':
        return ' · 未复权股价同步失败：'+price['error']+'；已保存市值保留'
    return f" · 未复权股价新增 {price['inserted']} 家，已有 {price['skipped']} 家，待补 {price['missing']} 家"


def main():
    folder = Path(sys.argv[1]).resolve()
    if not folder.is_relative_to(backfill.ROOT/'data/verification/samples/quarterly-price-updates'):
        raise ValueError('Invalid collection directory')
    try:
        backfill.collect(folder, backfill.ROOT/'data/credentials/bigquant.txt')
        result = {'ok':True}
    except Exception as exc:
        result = {'ok':False, 'error':sdk_error(str(exc))}
    print(json.dumps(result, ensure_ascii=True))


if __name__=='__main__':
    main()
