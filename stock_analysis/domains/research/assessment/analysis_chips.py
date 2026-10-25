"""Short-window facts for the user's chips/price hypotheses."""
from calendar import monthrange
from datetime import date


def months_before(day, months):
    current = date.fromisoformat(day)
    year, month = divmod(current.year * 12 + current.month - 1 - months, 12)
    return date(year, month + 1, min(current.day, monthrange(year, month + 1)[1])).isoformat()


def _prior(rows, field, day, tolerance):
    candidates = [r for r in rows if r[field] <= day]
    row = max(candidates, key=lambda r: r[field], default=None)
    if row and (date.fromisoformat(day) - date.fromisoformat(row[field])).days <= tolerance:
        return row
    return None


def _change(start, end):
    return (end / start - 1) * 100 if start is not None and start > 0 and end is not None else None


def joint_evidence(holders, financing, qfq, raw, *, report_periods, as_of):
    """Use exactly two report dates and three calendar months, with matched prices."""
    holders = [r for r in holders if r['stat_date'] <= as_of and
               (not r.get('announced_on') or r['announced_on'] <= as_of)]
    financing = [r for r in financing if r['trade_date'] <= as_of]
    qfq = [r for r in qfq if r['date'] <= as_of]
    periods = sorted({p for p in report_periods if p <= as_of})[-2:]
    holder_end = periods[-1] if periods else None
    holder_start = periods[0] if len(periods) == 2 else None
    candidates = [r for r in holders if r['stat_date'] == holder_end]
    last_holder = max(candidates, key=lambda r: (r['holder_scope'] != 'unknown',
                      'F10' in r['source'], r['source']), default=None)
    first_holder = next((r for r in reversed(holders) if r['stat_date'] == holder_start and last_holder
                        and r['source'] == last_holder['source'] and r['holder_scope'] == last_holder['holder_scope']), None)
    last_financing = max(financing, key=lambda r: (r['trade_date'], r['source']), default=None)
    financing_rows = [r for r in financing if last_financing and r['source'] == last_financing['source']]
    finance_target = months_before(last_financing['trade_date'], 3) if last_financing else None
    first_financing = _prior(financing_rows, 'trade_date', finance_target, 15) if finance_target else None
    result = []
    for kind, first, latest, begin, end, target, value_field in (
        ('shareholders', first_holder, last_holder, holder_start, holder_end, holder_start, 'holders'),
        ('financing', first_financing, last_financing, first_financing['trade_date'] if first_financing else None,
         last_financing['trade_date'] if last_financing else None, finance_target, 'margin_balance'),
    ):
        price_start = _prior(qfq, 'date', begin, 15) if begin else None
        price_end = _prior(qfq, 'date', end, 15) if end else None
        start_value = first[value_field] if first else None
        end_value = latest[value_field] if latest else None
        price_valid = bool(price_start and price_end and price_start['close'] > 0 and price_end['close'] > 0)
        scope_known = kind != 'shareholders' or bool(latest and latest['holder_scope'] != 'unknown')
        missing_days, sample_count = None, sum(r is not None for r in (first, latest))
        if kind == 'financing' and begin:
            known_days = {r['date'] for r in raw if begin <= r['date'] <= end}
            selected = [r for r in financing_rows if begin <= r['trade_date'] <= end]
            missing_days = len(known_days - {r['trade_date'] for r in selected}) if known_days else None
            sample_count = len(selected)
            price_valid = price_valid and price_start['date'] == begin and price_end['date'] == end
        reasons = []
        if not latest: reasons.append('缺少最新财报期股东人数' if kind == 'shareholders' else '没有本地融资记录')
        if not first: reasons.append('缺少上一财报期同来源同口径股东人数' if kind == 'shareholders' else '缺少三个月窗口起点，不能用较短区间替代')
        if not scope_known: reasons.append('股东人数统计口径未知')
        if first and (start_value is None or start_value <= 0): reasons.append('起点数值非正，无法计算变化率')
        if not price_valid: reasons.append('缺少对应日期的有效正值前复权股价')
        if kind == 'financing' and (missing_days is None or missing_days): reasons.append('融资交易日覆盖不足')
        values = {
            'usable': not reasons, 'unavailable_reason': '；'.join(reasons) or None,
            'target_start_on': target, 'start_on': begin, 'end_on': end,
            'start_value': start_value, 'end_value': end_value,
            'change_pct': _change(start_value, end_value) if scope_known else None,
            'price_start_on': price_start['date'] if price_start else None,
            'price_end_on': price_end['date'] if price_end else None,
            'price_start': price_start['close'] if price_start else None,
            'price_end': price_end['close'] if price_end else None,
            'price_change_pct': _change(price_start['close'], price_end['close']) if price_valid else None,
            'sample_count': sample_count, 'missing_trading_days': missing_days,
            'scope': latest.get('holder_scope') if latest else None,
            'start_announced_on': first.get('announced_on') if first else None,
            'end_announced_on': latest.get('announced_on') if latest else None,
        }
        result.append({'id': 'shareholders.price.latest_reports' if kind == 'shareholders' else 'financing.price.3m',
            'metric': 'holders_price' if kind == 'shareholders' else 'financing_price',
            'value': values, 'window_label': '最近两个财报期' if kind == 'shareholders' else '最近3个月',
            'unit': '户' if kind == 'shareholders' else '元',
            'observed_on': end if latest else None, 'source': latest['source'] if latest else None,
            'price_source': 'tencent', 'adjustment': 'qfq',
            'methodology': '股东人数只比较最新两个财报期末同来源同口径值；融资回看3个日历月，起点最多向前15天且与股价同日。股东股价向前匹配最多15天。实际日期单列，端点变化不代表持续同向；短窗口信号不能证明半年至一年趋势。'})
    return result
