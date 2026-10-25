"""Current-date dividend assumptions over saved facts; no collection or writes.

Amounts: CNY yuan; shares: actual shares; rates: decimal fractions.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta

from stock_analysis.domains.valuation.dcf import sum_interest_bearing_debt
from stock_analysis.domains.financials.financial_storage import decompress_json
from stock_analysis.domains.financials.fundamental_service import SOURCE
from stock_analysis.domains.stocks.price_service import SOURCE as PRICE_SOURCE
from stock_analysis.domains.stocks.sources import normalize_code
from stock_analysis.domains.financials.statements import metric


VERSION = 'dividend_forecast_v3'
SCENARIOS = ('pessimistic', 'neutral', 'optimistic')


def finite(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def profit_basis(reports, year, mode='A', window=3, method='median', scenario='neutral'):
    by_period = {r['period']: r for r in reports}
    periods = [f'{y}-12-31' for y in range(year-window, year)]
    current = next((r for r in reversed(sorted(reports, key=lambda r: r['period']))
                    if r['period'].startswith(str(year)) and finite(r.get('profit')) is not None), None)
    realized = None
    if mode == 'A':
        if not current:
            return dict(value=None, samples=[], periods=periods, realized=None, reason='当年缺少已披露归母利润，请选B/C或手填利润')
        realized = current['profit']
        if current['period'].endswith('12-31'):
            return dict(value=realized, samples=[], periods=[current['period']], realized=realized, reason='全年已披露，使用实际值')
        pairs = [(by_period.get(p, {}).get('profit'), by_period.get(p[:4]+current['period'][4:], {}).get('profit')) for p in periods]
        samples = [a-b if finite(a) is not None and finite(b) is not None else None for a, b in pairs]
    else:
        samples = [by_period.get(p, {}).get('profit') for p in periods]
    if not all(finite(v) is not None for v in samples):
        return dict(value=None, samples=samples, periods=periods, realized=realized, reason='连续历史窗口缺年或缺同口径利润，未跳年')
    fraction = {'pessimistic': .25, 'neutral': .5, 'optimistic': .75}[scenario]
    base = sum(samples)/len(samples) if scenario == 'neutral' and method == 'mean' else quantile(samples, fraction)
    return dict(value=finite(base+(realized or 0)), samples=samples, periods=periods, realized=realized,
                reason=('累计已实现＋历史剩余利润' if mode == 'A' else '历史全年正常化利润') +
                       ('均值' if scenario == 'neutral' and method == 'mean' else f'{fraction*100:g}%分位'))


def build_history(reports, events, year, edits=None, confirmations=None, exclude_special=True):
    edits, confirmations = edits or {}, confirmations or {}
    annual = {int(r['period'][:4]): r for r in reports if r['period'].endswith('12-31')}
    history = []
    for y in range(year-5, year+1):
        report = annual.get(y, {})
        group = [e for e in events if e.get('report_period', '')[:4] == str(y)]
        cash, dps, missing, pending, unknown, excluded = 0, 0, False, False, False, 0
        active = 0
        for event in group:
            edit = edits.get(event['id'], {})
            if not str(edit.get('note', '')).strip():
                edit = {}
            kind = edit.get('kind', 'unknown')
            if event['state'] == 'cancelled':
                continue
            if event['state'] != 'paid':
                pending = True
                continue
            unknown = unknown or kind == 'unknown'
            if exclude_special and kind == 'special':
                excluded += 1
                continue
            active += 1
            amount = finite(edit.get('cash')) if 'cash' in edit else finite(event.get('cash'))
            if amount is None or amount < 0 or event.get('conflict'):
                missing = True
            else:
                cash += amount
            if finite(event.get('dps')) is None:
                missing = True
            else:
                dps += event['dps']
        confirmation = confirmations.get(str(y), {})
        confirmed = bool(confirmation.get('complete') and str(confirmation.get('note', '')).strip())
        zero = confirmed and confirmation.get('zero') and not active and not pending
        known = (active > 0 or excluded > 0 or zero) and not missing
        total = finite(cash) if known else None
        profit, fcf = finite(report.get('profit')), finite(report.get('fcf'))
        ratio = finite(total/profit) if total is not None and profit is not None and profit > 0 else None
        complete = bool(known and confirmed and not pending and (not exclude_special or not unknown))
        history.append(dict(year=y, profit=profit, fcf=fcf, cash=total, dps=finite(dps) if known else None,
                            payout_ratio=ratio, coverage=finite(fcf/total) if fcf is not None and total is not None and total > 0 else None,
                            complete=complete, pending=pending, unknown=unknown, excluded=excluded,
                            state='已确认不分红' if zero else '已确认完整' if complete else '仍待实施/核实' if pending else '已实施估算，覆盖未核实' if known else '缺失',
                            note=confirmation.get('note', ''), sources=report.get('sources', {})))
    return history


def payout_basis(history, year, window):
    """Estimate from the available fiscal-year events, without manual approval.

    Pending events, missing amounts or missing fiscal years still block a window.
    Unknown completeness/special status is disclosed separately, not fabricated.
    """
    rows = [next((r for r in history if r['year'] == y), {}) for y in range(year-window, year)]
    if not all(not r.get('pending') and finite(r.get('cash')) is not None and r['cash'] >= 0
               and finite(r.get('profit')) is not None for r in rows):
        return None
    profit = finite(sum(r['profit'] for r in rows))
    cash = finite(sum(r['cash'] for r in rows))
    return finite(cash/profit) if profit is not None and profit > 0 and cash is not None else None


def calculate_scenario(years, parameters, price):
    """Pure arithmetic shared with the JS implementation through parity fixtures."""
    rows, previous = [], finite(parameters.get('base_profit'))
    price = finite(price)
    for index, year in enumerate(years):
        entry = parameters.get('rows', [{}]*3)[index]
        growth, ratio, shares = (finite(entry.get(k)) for k in ('growth_rate', 'payout_ratio', 'shares'))
        errors = []
        if parameters.get('mode') == 'C' or index > 0:
            profit = finite(previous*(1+growth)) if previous is not None and growth is not None and growth >= -1 else None
        else:
            profit = finite(parameters.get('first_profit'))
        if 'profit' in entry:
            profit = finite(entry['profit'])
        if profit is None:
            errors.append('利润或增长基准无效')
        implied = finite(profit/previous-1) if profit is not None and previous is not None and previous > 0 else None
        if 'cash' in entry:
            cash = finite(entry['cash'])
            cash = cash if cash is not None and cash >= 0 else None
        else:
            cash = finite(profit*ratio) if profit is not None and profit > 0 and ratio is not None and ratio >= 0 else None
        actual_ratio = finite(cash/profit) if cash is not None and profit is not None and profit > 0 else None
        if cash is None:
            errors.append('分红不可用；亏损时请手填非负现金金额')
        if actual_ratio is not None and actual_ratio > 1:
            errors.append('分红超过归母净利润')
        if shares is None or shares <= 0:
            errors.append('总股本须为正数')
        dps = finite(cash/shares) if cash is not None and shares is not None and shares > 0 else None
        yield_rate = finite(dps/price) if dps is not None and price is not None and price > 0 else None
        if cash is not None and shares is not None and shares > 0 and dps is None:
            errors.append('每股金额溢出，请核对数量级')
        if price is None or price <= 0:
            errors.append('未复权股价须为正数')
        rows.append(dict(year=year, profit=profit, growth_rate=implied, payout_ratio=actual_ratio,
                         cash=cash, shares=shares, dps=dps, yield_rate=yield_rate, warnings=errors))
        previous = profit
    cumulative = finite(sum(r['dps'] for r in rows)) if all(r['dps'] is not None for r in rows) else None
    return dict(rows=rows, cumulative_dps=cumulative,
                cumulative_yield=finite(cumulative/price) if cumulative is not None and price is not None and price > 0 else None)


def scenario_factors(reports, year, mode='A', window=3, method='median'):
    """Editable relative spreads, independent of the user's neutral overrides."""
    mode = 'A' if mode == 'C' else mode
    center = profit_basis(reports, year, mode, window, method)
    if center['value'] is None and mode == 'A':
        mode = 'B'
        center = profit_basis(reports, year, mode, window, method)
    result = {}
    for scenario, fallback in [('pessimistic', .8), ('optimistic', 1.2)]:
        edge = profit_basis(reports, year, mode, window, method, scenario)
        value = None
        if center['value'] is not None and center['value'] != 0 and edge['value'] is not None:
            value = finite(1+(edge['value']-center['value'])/abs(center['value']))
        historical = value is not None and (value < 1 if scenario == 'pessimistic' else value > 1)
        result[scenario] = dict(profit_factor=value if historical else fallback, payout_factor=1, shares_factor=1,
                                source=f'{window}年历史利润25%/75%分位相对中性基准的差异' if historical else
                                '历史样本不足或无方向差异，0.8/1.2为可调整的情景假设，非公司预测')
    return result


def derive_scenario(years, neutral_parameters, price, factors):
    """Apply each coefficient once per year; never compound the scenario spread.

    Signed profit adjustment makes a factor below one worsen a loss. Explicit
    neutral cash overrides stay amount assumptions, scaled once, never negative.
    """
    neutral = calculate_scenario(years, neutral_parameters, price)
    profit_factor, payout_factor, shares_factor = (finite(factors.get(k)) for k in
                                                  ('profit_factor', 'payout_factor', 'shares_factor'))
    rows = []
    for original, configured in zip(neutral['rows'], neutral_parameters['rows']):
        profit = original['profit']
        row = dict(profit=finite(profit+abs(profit)*(profit_factor-1)) if profit is not None and profit_factor is not None else None,
                   growth_rate=0,
                   payout_ratio=finite(original['payout_ratio']*payout_factor) if original['payout_ratio'] is not None and payout_factor is not None and payout_factor >= 0 else None,
                   shares=finite(original['shares']*shares_factor) if original['shares'] is not None and original['shares'] > 0 and shares_factor is not None and shares_factor > 0 else None)
        if 'cash' in configured:
            row['cash'] = finite(original['cash']*profit_factor*payout_factor) if original['cash'] is not None and profit_factor is not None and profit_factor >= 0 and payout_factor is not None and payout_factor >= 0 else None
        rows.append(row)
    result = calculate_scenario(years, dict(mode='linked', base_profit=neutral_parameters.get('base_profit'), rows=rows), price)
    for row in result['rows']:
        row['warnings'] = ['派生分红不可用，请核对中性现金金额及情景系数' if w == '分红不可用；亏损时请手填非负现金金额' else w for w in row['warnings']]
    return result


def read_dividend_forecast(db, code):
    code = normalize_code(code)
    now = datetime.now(timezone(timedelta(hours=8)))
    as_of, year = now.date().isoformat(), now.year
    warnings, reports, events = [], [], []
    with db.connection() as conn:
        identity = conn.execute('SELECT id,name FROM instruments WHERE code=?', (code,)).fetchone()
        financial = conn.execute('SELECT * FROM financial_reports WHERE instrument_id=? AND source=? AND period<=? ORDER BY period',
                                 (identity['id'], SOURCE, as_of)).fetchall() if identity else []
        saved_events = conn.execute('SELECT * FROM dividend_events WHERE instrument_id=? ORDER BY id', (identity['id'],)).fetchall() if identity else []
        snapshots = conn.execute("SELECT * FROM market_trade_snapshot WHERE security_code=? AND is_latest=1 AND trade_date<=? "
                                 "AND definition_version IN ('em-valueanalysis-v1','historical-composite-v1') ORDER BY trade_date DESC, "
                                 "CASE definition_version WHEN 'em-valueanalysis-v1' THEN 0 ELSE 1 END,updated_at DESC,id DESC", (code, as_of)).fetchall()
        price = conn.execute('SELECT * FROM raw_daily_prices WHERE instrument_id=? AND source=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1',
                             (identity['id'], PRICE_SOURCE, as_of)).fetchone() if identity else None
        overrides = conn.execute('SELECT * FROM report_overrides WHERE instrument_id=?', (identity['id'],)).fetchall() if identity else []
        industry = conn.execute('SELECT d.name FROM sw_memberships m JOIN sw_industries d ON d.code=m.industry_code WHERE m.stock_code=? ORDER BY m.import_id DESC LIMIT 1', (code,)).fetchone()
    raw, evidence = {}, {}
    ignored = 0
    for row in financial:
        published = row['publish_date']
        if not published or published > as_of:
            ignored += 1
            continue
        raw.setdefault(row['period'], {})[row['report_type']] = json.loads(decompress_json(row['raw_json']))
        evidence[(row['period'], row['report_type'])] = dict(publish_date=published, obtained_at=row['obtained_at'],
                    content_hash=row['content_hash'], run_id=row['run_id'], source=row['source'])
    override_map = {(r['period'], r['field_name']): r for r in overrides}
    for period in sorted(raw):
        values, sources = {}, {}
        for key, field in [('profit', 'parent_profit'), ('cfo', 'cfo'), ('capex', 'capex')]:
            detail = metric(raw, period, field, 'ytd')
            detail['provenance'] = evidence.get((period, detail.get('source')))
            override = override_map.get((period, {'profit':'profit_ytd', 'cfo':'operating_cash_flow_ytd', 'capex':'capex_ytd'}[key]))
            if override and detail['value'] is not None:
                detail.update(value=finite(json.loads(override['value_json'])), method='财务人工覆盖',
                              override_updated_at=override['updated_at'], override_origin=override['origin'])
            values[key], sources[key] = finite(detail['value']), detail
        values['fcf'] = finite(values['cfo']-values['capex']) if values['cfo'] is not None and values['capex'] is not None else None
        reports.append(dict(period=period, **values, sources=sources))
    real_periods = {r['report_period'] for r in saved_events if r['source'] != 'legacy'}
    seen = set()
    for row in saved_events:
        if row['source'] == 'legacy' and row['report_period'] in real_periods:
            continue
        event_id = f"{row['source']}:{row['source_event_id'] or row['event_key']}"
        if event_id in seen:
            continue
        seen.add(event_id)
        doc = json.loads(row['raw_json'])
        dps = finite(row['cash_per_ten'])
        dps = dps/10 if dps is not None else None
        shares = finite(doc.get('TOTAL_SHARES', doc.get('total_shares')))
        cash = finite(dps*shares) if dps is not None and shares is not None and shares > 0 else None
        state = 'cancelled' if row['status'] == 'cancelled' else 'paid' if row['status'] == 'implemented' and row['ex_dividend_date'] and row['ex_dividend_date'] <= as_of else 'pending'
        if not row['report_period'] or row['report_period'] > as_of:
            continue
        events.append(dict(id=event_id, report_period=row['report_period'], proposal_date=row['proposal_date'], notice_date=row['notice_date'],
                           ex_date=row['ex_dividend_date'], payment_date=None, state=state, dps=dps, shares=shares, cash=cash,
                           amount_basis='事件原始DPS×来源总股数（估算，未扣回购等不参与股份）', kind='unknown',
                           source=row['source'], content_hash=row['content_hash'], obtained_at=row['obtained_at'], run_id=row['run_id']))
    groups = {}
    for event in events:
        if event['ex_date']:
            groups.setdefault((event['report_period'], event['ex_date']), []).append(event)
    for group in groups.values():
        if len(group) > 1:
            for event in group:
                event['conflict'] = True
            warnings.append(dict(code='event_identity_conflict', message='同报告期同除权日存在多个事件身份，金额留空待核实，未按金额直接去重。'))
    market = dict(price=None, price_date=None, shares=None, shares_date=None)
    share_row = next((r for r in snapshots if (finite(r['total_shares']) or 0) > 0), None)
    price_row = next((r for r in snapshots if (finite(r['close_price']) or 0) > 0), None)
    if share_row:
        market.update(shares=share_row['total_shares'], shares_date=share_row['trade_date'], shares_source='market_trade_snapshot.total_shares（股）',
                      shares_evidence={k: share_row[k] for k in ('source_id', 'batch_id', 'content_hash', 'obtained_at', 'definition_version')})
    if price and (finite(price['close']) or 0) > 0 and (not price_row or price['trade_date'] >= price_row['trade_date']):
        market.update(price=price['close'], price_date=price['trade_date'], price_source='raw_daily_prices / 腾讯未复权收盘价',
                      price_evidence={k: price[k] for k in ('source', 'content_hash', 'obtained_at', 'run_id')})
    elif price_row:
        market.update(price=price_row['close_price'], price_date=price_row['trade_date'], price_source='market_trade_snapshot / 未复权收盘价',
                      price_evidence={k: price_row[k] for k in ('source_id', 'batch_id', 'content_hash', 'obtained_at')})
    if market['shares_date'] != market['price_date']:
        warnings.append(dict(code='market_date_mismatch', message='股本与股价日期不同；请核对回购注销、送转、增发等期间变动，可手动调整股本。'))
    if any(e['shares'] and market['shares'] and e['notice_date'] and market['shares_date'] and e['notice_date'] > market['shares_date'] and e['shares'] != market['shares'] for e in events):
        warnings.append(dict(code='newer_share_evidence', message='较新分红公告中的股数与当前股本快照不同；未自动替换，分红基数不一定等于当前总股数。'))
    for field in ('price', 'shares'):
        day = market[field+'_date']
        if not day or (now.date()-datetime.fromisoformat(day).date()).days > 30:
            warnings.append(dict(code=field+'_stale', message=('股价' if field == 'price' else '股本')+'缺失或距今超过30天，请核对。'))
    name = identity['name'] if identity else ''
    financial_company = any(w in (name or '')+(industry['name'] if industry else '') for w in ('银行', '证券', '保险', '多元金融', '中国平安'))
    balance_periods = [p for p in sorted(raw) if 'fzb' in raw[p]]
    sustainability = dict(financial_company=financial_company, period=None, cash=None, debt=None, net_cash=None, missing_debt=[])
    if balance_periods and not financial_company:
        period = balance_periods[-1]
        doc = raw[period]['fzb']
        if doc.get('rCurrency') == 'CNY' and doc.get('rType') == '合并期末':
            debt = sum_interest_bearing_debt(doc)
            cash_detail = metric(raw, period, 'cash', 'ytd', balance_first=True)
            cash = finite(cash_detail['value'])
            sustainability.update(period=period, cash=cash, debt=debt['value'], missing_debt=debt['missing'], components=debt['components'],
                                  net_cash=finite(cash-debt['value']) if cash is not None and debt['complete'] else None,
                                  source=evidence[(period, 'fzb')], cash_source=cash_detail)
    if ignored:
        warnings.append(dict(code='undisclosed_reports', message=f'{ignored}条财报缺少已到达的真实披露日，未用于自动计算。'))
    warnings.append(dict(code='dividend_evidence_gap', message='分红金额为估算；实际派息日、特别分红性质及年度覆盖尚未核实。历史分红率不构成公司承诺。'))
    history = build_history(reports, events, year, exclude_special=False)
    return dict(code=code, name=name, as_of_date=as_of, years=[year, year+1, year+2], calculated_at=now.isoformat(), methodology_version=VERSION,
                units=dict(amount='CNY元', shares='股', dps='元/股', rate='小数'), reports=reports, events=events, market=market,
                history=history, payout_bases={str(n): payout_basis(history, year, n) for n in (1, 3, 5)},
                sustainability=sustainability, warnings=warnings,
                profit_bases={s: profit_basis(reports, year, scenario=s) for s in SCENARIOS},
                scenario_factors=scenario_factors(reports, year),
                policy=dict(available=False, reason='本地暂无可靠结构化分红政策，允许手填并记录依据'))
