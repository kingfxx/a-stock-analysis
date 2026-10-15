"""Transparent, read-only inputs and pure arithmetic for simplified DCF.

Amounts are CNY yuan, shares are actual shares, and all rates are percentages.
Discounted CFO less Capex is not an enterprise or parent equity valuation.
"""
from __future__ import annotations

import json
import math
from datetime import date
from statistics import median, pstdev

from .core import build_period_rows, view_rows
from .fundamental_service import FundamentalService, SOURCE
from .price_service import PriceService
from .sources import normalize_code
from .statements import metric


DEFAULTS = dict(base_window='3', growth_pct=3, discount_pct=9, terminal_growth_pct=2)
FCFF_GAPS = [
    '缺少已核实的利息现金支出归类及实际税盾，不能由简化FCF自动转为FCFF。',
    '经营必需现金、受限现金与超额现金尚未区分，不能把全部货币资金加回。',
    '非经营资产与相关收益未完成一致调整。',
    '有息债务、租赁及一年内到期负债需核实，避免缺项归零或重复扣除。',
    '少数股东权益及其他优先索取权缺少可靠估值。',
]


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def calculate_dcf(base_fcf, growth_pct, discount_pct, terminal_growth_pct, *, shares=None, price=None):
    """Ten year-end cash flows and year eleven perpetuity; reject invalid input."""
    values = [_finite(v) for v in (base_fcf, growth_pct, discount_pct, terminal_growth_pct)]
    if any(v is None for v in values):
        raise ValueError('DCF参数必须是有效有限数值')
    base, growth, rate, terminal = values
    growth, rate, terminal = (v / 100 for v in (growth, rate, terminal))
    if growth <= -1 or rate <= 0 or terminal <= -1 or rate <= terminal:
        raise ValueError('增长率必须大于-100%，折现率必须为正且大于永续增长率')
    try:
        rows = [dict(year=t, fcf=base * (1 + growth)**t,
                     present_value=base * (1 + growth)**t / (1 + rate)**t) for t in range(1, 11)]
        explicit = sum(r['present_value'] for r in rows)
        terminal_value = rows[-1]['fcf'] * (1 + terminal) / (rate - terminal)
        terminal_pv = terminal_value / (1 + rate)**10
        total = explicit + terminal_pv
    except (OverflowError, ZeroDivisionError):
        raise ValueError('DCF计算溢出，请缩小参数范围') from None
    if not all(math.isfinite(v) for v in [explicit, terminal_value, terminal_pv, total,
                                         *[v for row in rows for v in row.values()]]):
        raise ValueError('DCF计算溢出，请缩小参数范围')
    count, quote = _finite(shares), _finite(price)
    per_share = total / count if count is not None and count > 0 else None
    difference = (1 - quote / per_share) * 100 if per_share is not None and per_share > 0 and quote is not None and quote > 0 else None
    if per_share is not None and not math.isfinite(per_share) or difference is not None and not math.isfinite(difference):
        raise ValueError('每股换算溢出，请核对股数与价格单位')
    return dict(rows=rows, explicit_value=explicit, terminal_value=terminal_value,
                terminal_present_value=terminal_pv, total_value=total,
                terminal_share_pct=terminal_pv / total * 100 if total else None,
                per_share=per_share, reference_difference_pct=difference,
                mode='simplified', warnings=['基准FCF非正，不适合持续增长DCF。'] if base <= 0 else [])


def estimate_wacc(risk_free_pct, beta, equity_risk_premium_pct, debt_cost_pct, tax_rate_pct, debt_weight_pct):
    """Manual assumptions only; never fill missing capital-cost data with zero."""
    values = [_finite(v) for v in (risk_free_pct, beta, equity_risk_premium_pct, debt_cost_pct, tax_rate_pct, debt_weight_pct)]
    if any(v is None for v in values):
        raise ValueError('WACC需要全部六项有效输入')
    rf, b, erp, kd, tax, weight = values
    if min(rf, b, erp, kd) < 0 or not 0 <= tax <= 100 or not 0 <= weight <= 100:
        raise ValueError('WACC输入范围无效')
    ke = rf + b * erp
    result = ke * (1 - weight / 100) + kd * (1 - tax / 100) * weight / 100
    if not math.isfinite(result) or result <= 0:
        raise ValueError('WACC必须为有限正数')
    return dict(equity_cost_pct=ke, wacc_pct=result, status='manual_assumptions')


def _growth_recommendation(history):
    metrics = []
    for field, label in [('revenue', '营业收入'), ('profit', '归母净利润'), ('free_cash_flow', '简化FCF')]:
        changes = []
        values = [_finite(r.get(field)) for r in history]
        for old, new in zip(history, history[1:]):
            a, b = _finite(old.get(field)), _finite(new.get(field))
            if int(new['period'][:4]) == int(old['period'][:4]) + 1 and a is not None and a > 0 and b is not None and b >= 0:
                change = (b / a - 1) * 100
                if math.isfinite(change):
                    changes.append(change)
        metrics.append(dict(field=field, label=label, sample_count=len(changes),
                            available_years=sum(v is not None for v in values),
                            median_growth_pct=median(changes) if changes else None,
                            volatility_pct=pstdev(changes) if changes else None,
                            negative_years=sum(v is not None and v < 0 for v in values)))
    reliable = [m['median_growth_pct'] for m in metrics if m['sample_count'] >= 4]
    if not reliable:
        return dict(value_pct=3, status='insufficient', metrics=metrics,
                    basis='不足5年可比数据；3%为配置假设，不使用异常端点CAGR。')
    recommendation = max(-3, min(8, median(reliable)))
    unstable = any(m['negative_years'] or (m['volatility_pct'] or 0) > 40 for m in metrics)
    if unstable:
        recommendation = min(recommendation, 3)
    return dict(value_pct=round(recommendation, 2), status='estimated', metrics=metrics,
                basis='最近最多10个完整年度、相邻正基数同比的中位数；至少4对同比参与指标中位数汇总，范围-3%至8%；负值或同比波动标准差>40个百分点时上限3%。推荐是历史参考。')


def read_dcf(db, code, legacy_root):
    """Read existing facts only. No collector, migration, model, or price lease."""
    code = normalize_code(code)
    financial = FundamentalService(db, legacy_root).read(code)
    reports = financial.get('reports', [])
    with db.connection() as conn:
        raw_rows = conn.execute('SELECT f.* FROM financial_reports f JOIN instruments i ON i.id=f.instrument_id WHERE i.code=? AND f.source=?', (code, SOURCE)).fetchall()
        snapshots = conn.execute("SELECT * FROM market_trade_snapshot WHERE security_code=? AND is_latest=1 AND trade_date<=? AND definition_version IN ('em-valueanalysis-v1','historical-composite-v1') ORDER BY trade_date DESC,CASE definition_version WHEN 'em-valueanalysis-v1' THEN 0 ELSE 1 END,updated_at DESC,id DESC", (code, date.today().isoformat())).fetchall()
        overrides = conn.execute('SELECT o.* FROM report_overrides o JOIN instruments i ON i.id=o.instrument_id WHERE i.code=?', (code,)).fetchall()
        industry = conn.execute('SELECT d.name,m.effective_date FROM sw_memberships m JOIN sw_industries d ON d.code=m.industry_code WHERE m.stock_code=? ORDER BY m.import_id DESC LIMIT 1', (code,)).fetchone()
    raw, metadata = {}, {}
    for r in raw_rows:
        raw.setdefault(r['period'], {})[r['report_type']] = json.loads(r['raw_json'])
        metadata[(r['period'], r['report_type'])] = dict(content_hash=r['content_hash'], obtained_at=r['obtained_at'], run_id=r['run_id'])
    override_map = {(r['period'], r['field_name']): r for r in overrides}
    mappings = [('revenue_ytd', 'revenue'), ('profit_ytd', 'parent_profit'), ('operating_cash_flow_ytd', 'cfo'), ('capex_ytd', 'capex')]
    normalized, evidence = [], {}
    for original in reports:
        row = dict(original)
        period = row['period']
        if period > date.today().isoformat():
            continue
        evidence[period] = {}
        for field, key in mappings:
            detail = metric(raw, period, key, 'ytd')
            selected = raw.get(period, {}).get(detail.get('source'), {})
            detail.update(currency=selected.get('rCurrency'), scope=selected.get('rType'),
                          provenance=metadata.get((period, detail.get('source'))), override=False)
            if period in raw:
                row[field] = detail['value']
            else:
                row[field] = None
                detail = dict(value=None, source='legacy', field=field,
                              period=period, unit='元', method='待补', reason='缺少币种及合并口径原始证据，未使用legacy值')
            override = override_map.get((period, field))
            relevant_kind = 'llb' if key in ('cfo','capex') else 'lrb'
            relevant_raw = raw.get(period, {}).get(relevant_kind, {})
            if override and relevant_raw.get('rCurrency') == 'CNY' and relevant_raw.get('rType') == '合并期末':
                row[field] = _finite(json.loads(override['value_json']))
                detail.update(value=row[field], override=True, method='人工覆盖', override_origin=override['origin'],
                              override_updated_at=override['updated_at'], override_source_path=override['source_path'],
                              currency=relevant_raw['rCurrency'], scope=relevant_raw['rType'])
            evidence[period][field] = detail
        normalized.append(row)
    annual = view_rows(build_period_rows(normalized, [], []), 'year')
    history = []
    latest = max((r['period'] for r in reports if r['period'] <= date.today().isoformat()), default=None)
    last_year = int(latest[:4]) - (latest[5:] != '12-31') if latest else date.today().year - 1
    for row in annual:
        if last_year - 9 <= int(row['period'][:4]) <= last_year:
            sources = evidence[row['period']]
            sources['free_cash_flow'] = dict(method='复用core经营现金流−资本开支', unit='元', inputs=[sources['operating_cash_flow_ytd'], sources['capex_ytd']])
            history.append({k: row.get(k) for k in ('period', 'revenue', 'profit', 'operating_cash_flow', 'capex', 'free_cash_flow')} | dict(sources=sources))
    by_period = {r['period']: r for r in history}
    bases = {}
    for n in (1, 3, 5):
        periods = [f'{y}-12-31' for y in range(last_year - n + 1, last_year + 1)]
        values = [by_period.get(p, {}).get('free_cash_flow') for p in periods]
        available = all(_finite(v) is not None for v in values)
        bases[str(n)] = dict(value=sum(values) / n if available else None, periods=periods,
                             available=available, reason=None if available else '连续完整年度FCF存在缺年或缺值，未跳过缺失年份。',
                             source='现有年度经营现金流−资本开支，元')
    market = dict(total_shares=None, shares_date=None, shares_source=None, price=None, price_date=None, price_source=None)
    warnings = []
    snapshot = next((r for r in snapshots if (_finite(r['total_shares']) or 0) > 0), None)
    price_snapshot = next((r for r in snapshots if (_finite(r['close_price']) or 0) > 0), None)
    if snapshots and snapshot and snapshots[0]['trade_date'] > snapshot['trade_date']:
        warnings.append('最新交易快照缺少有效总股数，使用较早已核实股本并展示日期。')
    if snapshot and (_finite(snapshot['total_shares']) or 0) > 0:
        market.update(total_shares=snapshot['total_shares'], shares_date=snapshot['trade_date'], shares_source='market_trade_snapshot.total_shares（股）',
                      shares_evidence=dict(source_id=snapshot['source_id'], batch_id=snapshot['batch_id'], content_hash=snapshot['content_hash'], definition_version=snapshot['definition_version'], obtained_at=snapshot['obtained_at']))
    elif normalized:
        current = max(normalized, key=lambda r: r['period'])
        # Sina's monetary share-capital line is not automatically a verified share count.
        market.update(shares_date=current['period'], shares_source='最新报表股本（实收资本面值金额，未验证股数）')
        warnings.append('缺少已核实总股数；报表实收资本金额不能自动替代股数，每股参考值留空。')
    prices = PriceService(db).read(code, 'raw', lease=False, end=date.today().isoformat())
    price_row = next((p for p in reversed(prices) if (_finite(p['close']) or 0) > 0), None)
    if price_row and (not price_snapshot or price_row['date'] >= price_snapshot['trade_date']):
        market.update(price=price_row['close'], price_date=price_row['date'], price_source='raw_daily_prices.close / 腾讯未复权收盘价')
    elif price_snapshot:
        market.update(price=price_snapshot['close_price'], price_date=price_snapshot['trade_date'], price_source='market_trade_snapshot.close_price（未复权，较新日期或回退）')
    for field in ('shares_date', 'price_date'):
        if market[field] and (date.today() - date.fromisoformat(market[field])).days > 30:
            warnings.append(('股本' if field == 'shares_date' else '行情') + '数据距今超过30天，请核对时效。')
    if market['shares_date'] and market['price_date'] and market['shares_date'] != market['price_date']:
        warnings.append('股本与行情日期不同，参考换算可能受期间股本变动影响。')
    industry_name = industry['name'] if industry else None
    name = financial.get('name') or ''
    financial_company = any(word in name + (industry_name or '') for word in ('银行', '证券', '保险', '中国平安', '多元金融'))
    if financial_company:
        warnings.append('金融企业不适用本标准DCF模型，结果应禁止展示。')
    elif not industry:
        warnings.append('缺少行业分类，DCF适用性仍需核对。')
    elif any(word in industry_name for word in ('煤炭', '钢铁', '有色', '航运', '石油', '房地产')):
        warnings.append('周期行业需核对跨周期现金流，历史增长可能不可持续。')
    if any(r['free_cash_flow'] is not None and r['free_cash_flow'] <= 0 for r in history):
        warnings.append('历史存在非正FCF年份，请检查投资周期和异常值；不适合直接外推永续增长。')
    return dict(code=code, name=financial.get('name'), mode='simplified', model_label='简化DCF（非FCFF/FCFE）', defaults=dict(DEFAULTS),
                bases=bases, history=history, market=market, updated_at=financial.get('updated_at'), warnings=warnings,
                recommendations=dict(growth=_growth_recommendation(history),
                    discount=dict(value_pct=9, status='assumption', basis='9%为可配置折现率假设；本地缺少已核实的10年国债收益率、Beta、ERP和资本成本数据，非自动WACC。'),
                    terminal=dict(value_pct=2, range_pct=[0, 2], status='assumption', basis='成熟企业0%—2%初步假设区间；企业成熟度未经自动验证，允许覆盖，且必须低于折现率。')),
                applicability=dict(financial_company=financial_company, blocked=financial_company, industry=industry_name,
                                   basis='本地申万行业分类与公司名称关键词' if industry else '公司名称关键词；行业分类不可用'),
                professional=dict(available=False, gaps=list(FCFF_GAPS), adjustments=[]))
