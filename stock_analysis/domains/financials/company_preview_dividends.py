"""Pure, conservative aggregation of saved per-report dividend plans."""
import re


def parse_plan(description):
    text = re.sub(r'\s+', '', description or '').replace('（', '(').replace('）', ')')
    if not text:
        return {'cash_per_share': None, 'share_change': False, 'status': '未提供方案'}
    if text in {'不分配不转增', '不分红不转增'}:
        return {'cash_per_share': 0.0, 'share_change': False, 'status': '明确不分配'}
    match = re.fullmatch(r'10(?P<shares>(?:(?:送|转)\d+(?:\.\d+)?股?)*)(?:派(?P<cash>\d+(?:\.\d+)?)元)?(?:\([^)]*\))?', text)
    if not match or not (match['shares'] or match['cash']):
        return {'cash_per_share': None, 'share_change': False, 'status': '方案无法可靠解析，可能包含累计或特别安排'}
    change = any(float(v) > 0 for v in re.findall(r'(?:送|转)(\d+(?:\.\d+)?)', match['shares']))
    return {'cash_per_share': float(match['cash'] or 0) / 10, 'share_change': change,
            'status': '含送转，暂不混用每股基准' if change else '已解析税前方案'}


def annual_dividend_plans(records):
    years = {}
    for record in records:
        period = record['report_date']
        year = years.setdefault(period[:4], {'plans': {}, 'share_change_periods': [], 'conflict': False})
        plan = {**record, **parse_plan(record.get('assigndscrpt'))}
        old = year['plans'].get(period)
        if old and old.get('assigndscrpt') != record.get('assigndscrpt'):
            year['conflict'] = True
        year['plans'][period] = plan
        if plan['share_change'] and period not in year['share_change_periods']:
            year['share_change_periods'].append(period)
    for key, year in years.items():
        plans = year['plans']
        reason = ''
        if year['conflict']:
            reason = '同报告期方案冲突'
        elif any(key + end not in plans for end in ('-03-31', '-06-30', '-09-30', '-12-31')):
            reason = '上年业绩报告期覆盖不足'
        elif any(plans[key + end]['cash_per_share'] is None for end in ('-06-30', '-12-31')):
            reason = '上年中报或年报缺少明确方案'
        elif any(p['cash_per_share'] is None and p.get('assigndscrpt') for p in plans.values()):
            reason = '上年存在无法可靠解析的方案'
        elif year['share_change_periods']:
            reason = '上年含送转方案，暂不混用每股基准'
        elif any(p.get('payyear') and str(p['payyear']) != key for p in plans.values()):
            reason = '方案年度标识与报告期不一致'
        year['total_per_share'] = None if reason else sum(p['cash_per_share'] for p in plans.values() if p['cash_per_share'] is not None)
        year['reason'] = reason or '按本地已存方案合计；未保证特别分红完整，Q1/Q3未返回方案不计入合计'
        year['plans'] = [plans[p] for p in sorted(plans)]
        del year['conflict']
    return years


def previous_year_yield(years, period, price):
    previous = years.get(str(int(period[:4]) - 1), {})
    cash = previous.get('total_per_share')
    current_changes = years.get(period[:4], {}).get('share_change_periods', [])
    if any(p <= period for p in current_changes) or cash is None or price is None or price <= 0:
        return None
    return cash / price * 100
