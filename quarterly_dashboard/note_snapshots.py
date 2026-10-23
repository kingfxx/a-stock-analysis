"""Note insertions from saved facts only; never call update/ensure/load_stock."""
from datetime import date, datetime
from zoneinfo import ZoneInfo
import math

from .sources import normalize_code
from .fundamental_service import FundamentalService, SOURCE as FINANCIAL_SOURCE
from .dividend_service import DividendService
from .valuation_service import ValuationService
from .price_service import PriceService
from .update_service import ChipService, FINANCING_SOURCE
from .business_segment_service import BusinessSegmentService
from .statements import read_statements
from .core import build_period_rows, view_rows
from .valuation import daily_dividend_yield_observations, valuation_summary, valuation_summary_observations

FINANCIAL = {
    'revenue': ('营业收入', '亿元', 1e8, 'lrb.BIZINCO/BIZTOTINCO'),
    'profit': ('归母净利润', '亿元', 1e8, 'lrb.PARENETP'),
    'adjusted_profit': ('归母扣非净利润', '亿元', 1e8, 'gjzb.NPCUT，缺值回退适用利润表科目'),
    'revenue_growth': ('营业收入同比', '%', 1, '同口径收入÷上年同期收入−1；基期须为正'),
    'profit_growth': ('归母净利润同比', '%', 1, '同口径归母利润÷上年同期归母利润−1；基期须为正'),
    'operating_cash_flow': ('经营活动现金流净额', '亿元', 1e8, 'llb.MANANETR'),
    'main_business_cash_flow': ('主营现金流', '亿元', 1e8, 'llb.LABORGETCASH，现金流入'),
    'capex': ('资本开支', '亿元', 1e8, 'llb.ACQUASSETCASH'),
    'free_cash_flow': ('简化自由现金流', '亿元', 1e8, '经营活动现金流净额−资本开支'),
    'gross_margin': ('毛利率', '%', 1, '（营业收入−营业成本）÷营业收入；收入须为正'),
    'net_margin': ('合并净利率', '%', 1, '合并净利润÷营业收入；收入须为正'),
    'roe': ('ROE（TTM估算）', '%', 1, 'TTM归母利润÷两端归母净资产均值'),
    'roic': ('ROIC（简化估算）', '%', 1, '复用企业经营趋势简化ROIC公式'),
    'net_assets': ('净资产（含少数股东权益）', '亿元', 1e8, 'fzb合并股东权益，期末值'),
    'interest_bearing_debt': ('有息负债（简化）', '亿元', 1e8, '复用趋势页六项债务加总及缺值规则，期末值'),
    'net_cash': ('净现金／净负债', '亿元', 1e8, '期末货币资金−简化有息负债'),
}
MODES = {'quarter': '单季度', 'ytd': '年初至期末累计', 'year': '年度', 'ttm': 'TTM'}


def valid(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def number(value, unit, scale=1):
    if not valid(value):
        return '暂无数据'
    return f'{value / scale:,.2f}{unit}'


def percentile_position(value, metric):
    """Same seven percentile bands and yield color direction as the valuation card."""
    if not valid(value) or not 0 <= value <= 100:
        return {}
    rank = (0 if value <= 5 else 1 if value <= 20 else 2 if value < 40 else
            3 if value <= 60 else 4 if value < 80 else 5 if value < 95 else 6)
    is_yield = metric == 'dividend_yield'
    labels = ['极低', '低息' if is_yield else '低位', '偏低', '中值', '偏高', '高息' if is_yield else '高位', '极高']
    markers = ['🟢', '🟢', '🔵', '🟡', '🟠', '🔴', '🔴']
    return {'position': {'label': labels[rank], 'marker': markers[6-rank if is_yield else rank]}}


def catalog(db, command, cache, valuation_cache, chip_cache):
    if str(command.get('code', '')).startswith('index:'):
        from .index_note_snapshots import catalog as index_catalog
        return index_catalog(db, command)
    code = normalize_code(command.get('code', ''))
    group = command.get('group', 'valuation')
    result = {'code': code, 'group': group, 'items': [], 'periods': [],
              'captured_at': datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')}

    def add(key, label, value, day, basis, source, unit='', scale=1, **extra):
        result['items'].append({'id': str(key), 'label': label, 'value': number(value, unit, scale),
                                'date': day or '未记录', 'basis': basis, 'source': source,
                                'available': valid(value), **extra})

    fundamentals = FundamentalService(db, cache).read(code)
    result['name'] = fundamentals.get('name') or code
    if group == 'financial':
        mode = command.get('mode', 'quarter')
        if mode not in MODES:
            raise ValueError('财务周期无效')
        rows = view_rows(build_period_rows(fundamentals.get('reports', []), [], []), mode)
        result['periods'] = [r['period'] for r in reversed(rows)]
        period = command.get('period') or (rows[-1]['period'] if rows else None)
        row = next((r for r in rows if r['period'] == period), {})
        original = next((r for r in fundamentals.get('reports', []) if r['period'] == period), {})
        for key, (label, unit, scale, field) in FINANCIAL.items():
            short = f'{period[:4]}Q{int(period[5:7]) // 3}' if period else '未记录'
            basis = MODES[mode]
            if key in ('roe', 'roic'):
                basis = '年度或TTM公式，非单季度年化'
            if key in ('net_assets', 'interest_bearing_debt', 'net_cash'):
                basis = '报告期末时点值'
            source = (f'本地 financial_reports，{FINANCIAL_SOURCE}，适用时含显式人工覆盖；{field}；'
                      f'披露日 {original.get("publish_date") or "未记录"}，披露日来源 {original.get("publish_date_source") or "未记录"}；'
                      f'本地资料更新时间 {fundamentals.get("updated_at") or "未记录"}；读取当前保存的修订版本，非历史实时回测')
            add(key, f'{short} {label}', row.get(key), period, basis, source, unit, scale)
    elif group == 'valuation':
        # Reuse pure valuation formulas, anchored to the requested observation day.
        years = int(command.get('years', 10))
        if years not in (3, 5, 10):
            raise ValueError('历史区间无效')
        prices = PriceService(db)
        raw = prices.read(code, 'raw')
        events = DividendService(db, cache).read(code)
        saved = ValuationService(db, valuation_cache).read(code, fundamentals.get('reports', []))
        result['valuation_updated_on'] = saved.get('updated_on')
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        requested = command.get('as_of')
        if requested is not None:
            try:
                if not isinstance(requested, str) or date.fromisoformat(requested).isoformat() != requested or requested > today:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError('数据日期须为有效的 YYYY-MM-DD，且不能晚于今天') from None
        observations = saved.get('observation_rows', [])
        legacy = saved.get('rows', [])
        yield_rows = daily_dividend_yield_observations(raw, events)
        metric_rows = {}
        for metric in ('pe', 'pb', 'dividend_yield'):
            rows = yield_rows if metric == 'dividend_yield' and yield_rows else observations if metric != 'dividend_yield' and observations else legacy
            # Legacy monthly rows can hold different real observation dates per metric.
            metric_rows[metric] = [{**row, 'date': row.get(metric + '_date') or row['date']} for row in rows]
        dates = {row['date'] for metric, rows in metric_rows.items() for row in rows
                 if row['date'] <= today and valid(row.get(metric))
                 and (row[metric] >= 0 if metric == 'dividend_yield' else row[metric] > 0)}
        dates.update(row['date'] for row in raw if row['date'] <= today and valid(row.get('close')) and row['close'] > 0)
        result['available_dates'] = sorted(dates)
        selected_day = requested or (max(dates) if dates else today)
        result['as_of'] = selected_day
        result['has_date_data'] = selected_day in dates
        for key, label, unit in [('pe', 'PE（TTM）', '倍'), ('pb', 'PB', '倍'), ('dividend_yield', '股息率', '%')]:
            rows = metric_rows[key]
            if key != 'dividend_yield' and observations:
                summary = valuation_summary_observations(rows, key, years, {}, as_of=selected_day,
                                                        trading_dates=[row['date'] for row in raw])
            else:
                summary = valuation_summary(rows, key, years, {}, as_of=selected_day)
            exact = summary.get('current_date') == selected_day
            source = ('本地 valuation_observations／估值缓存，页面纯计算投影；'
                      '股息率使用页面已有现金分红及价格口径；'
                      f'估值更新日 {saved.get("updated_on") or "未记录"}；按当前保存资料计算，非历史留存快照')
            basis = '与企业经营趋势页同口径' if exact else '所选日期缺少有效本地数据，不沿用其他日期'
            add(key, label, summary.get('current') if exact else None, selected_day, basis, source, unit)
            frequency = summary.get('percentile_frequency') or ('day' if key == 'dividend_yield' and yield_rows else 'month')
            frequency = {'day': '日频观测', 'week': '周频观测', 'month': '月频观测'}.get(frequency, frequency)
            add(key + '_percentile', f'{label}近{years}年历史分位', summary.get('percentile') if exact else None, selected_day,
                f'截至{selected_day}近{years}年，采样 {frequency}；低于所选日值的有效历史样本占比，0.8即0.8%；'
                f'样本数 {summary.get("sample_count", summary.get("count", "未记录"))}；{summary.get("sparse_hint") or ""}', source, '%',
                **percentile_position(summary.get('percentile') if exact else None, key))
        price = next((row for row in raw if row['date'] == selected_day), {})
        close = price.get('close')
        add('price', '收盘价', close if valid(close) and close > 0 else None, selected_day,
            '未复权；只取所选日期', '本地 raw_daily_prices，腾讯原始日K', '元/股')
    elif group == 'chips':
        service = ChipService(db, chip_cache)
        holder_data = service.cached(code, 'shareholders')
        holders = holder_data.get('rows', [])
        holder_series = next(iter(holder_data.get('series', [])), {})
        financing = service.cached(code, 'financing').get('rows', [])
        kind = command.get('kind', 'holders')
        rows = holders if kind == 'holders' else financing
        result['periods'] = [r['date'] for r in reversed(rows)]
        period = command.get('period') or (rows[-1]['date'] if rows else None)
        row = next((r for r in rows if r['date'] == period), {})
        if kind == 'holders':
            add('holders', '股东户数', row.get('holders'), period, '实际统计截止日，非记录当日',
                f'本地 shareholder_observations；来源 {holder_series.get("source") or "未记录"}；'
                f'披露日 {row.get("announced_on") or "未记录"}；持有人范围 {holder_series.get("scope") or "未记录"}', '户')
            index = next((i for i, r in enumerate(rows) if r['date'] == period), -1)
            previous = rows[index-1] if index > 0 else {}
            base, now = previous.get('holders'), row.get('holders')
            change = (now / base - 1) * 100 if valid(base) and base > 0 and valid(now) else None
            add('holders_change', '股东户数较上期变化', change, period, f'比较 {previous.get("date") or "未记录"}',
                '本地股东户数；（本期÷上期−1）×100，基期须为正', '%')
        else:
            for key, label in [('margin_balance', '融资余额'), ('net_buy', '融资净买入额'), ('total_balance', '融资融券余额')]:
                add(key, label, row.get(key), period, '交易日末余额' if key != 'net_buy' else '当日净买入额，非买入总额',
                    f'本地 financing_daily，{FINANCING_SOURCE}，只读已保存资料', '亿元', 1e8)
            add('margin_buy', '融资买入额', None, None, '现有本地页面未提供该字段', '未记录', '亿元', 1e8)
            add('short_volume', '融券余量', None, None, '现有本地页面未提供该字段', '未记录', '股')
    elif group == 'business':
        saved = BusinessSegmentService(db).read(code)
        periods = saved.get('periods', [])
        result['periods'] = [p['report_date'] for p in reversed(periods)]
        period = command.get('period') or (periods[-1]['report_date'] if periods else None)
        selected = next((p for p in periods if p['report_date'] == period), {})
        dimension = str(command.get('dimension', '2'))
        if dimension not in ('1', '2', '3'):
            raise ValueError('主营维度无效')
        for item in selected.get('dimensions', {}).get(dimension, []):
            for key, label in [('revenue', '收入'), ('cost', '成本'), ('gross_profit', '毛利'), ('revenue_share', '收入占比'), ('gross_margin', '毛利率')]:
                ratio = key in ('revenue_share', 'gross_margin')
                add(f'{item["id"]}:{key}', f'{item["item_name"]} · {label}', item.get(key), period,
                    f'累计；原始名称；维度 {dimension}；毛利非净利润',
                    f'本地 business_segment_items.{key}；来源 {selected.get("source")}；修订 {selected.get("revision")}；'
                    f'内容哈希 {selected.get("content_hash")}；取得时间 {selected.get("obtained_at")}',
                    '%' if ratio else '亿元', .01 if ratio else 1e8)
    elif group == 'statements':
        mode = command.get('mode', 'ytd')
        statement = command.get('statement', 'lrb')
        if statement not in ('fzb', 'lrb', 'llb'):
            raise ValueError('报表类型无效')
        data = read_statements(db, code, period=command.get('period') or None,
                               mode=mode, comparison=command.get('comparison', 'yoy'))
        result['periods'] = list(reversed(data.get('periods', [])))
        section = data.get('sections', {}).get(statement, {})
        def source_text(item):
            fields = '; '.join(str(item.get(k)) for k in ('source', 'item_source', 'field', 'period', 'method', 'formula', 'currency', 'scope', 'reason') if item.get(k) is not None)
            inputs = item.get('inputs', [])
            return fields + ('；输入：' + ' / '.join(source_text(x) for x in inputs) if inputs else '')
        card_keys = {
            'fzb': ('assets', 'equity', 'liabilities', 'cash', 'debt', 'debt_ratio', 'leverage'),
            'lrb': ('revenue', 'parent_profit', 'deducted_profit', 'gross_margin', 'parent_net_margin', 'roe'),
            'llb': ('cfo', 'cfi', 'cff', 'cash_increase', 'cash_profit', 'capex', 'cash_after_capex'),
        }
        for key in card_keys[statement]:
            item = data.get('values', {}).get(key)
            if not item:
                continue
            unit = item.get('unit', '')
            source = f'本地 financial_reports；{source_text(item)}；更新时间 {data.get("updated_at") or "未记录"}；当前保存版本'
            basis = '期末值' if statement == 'fzb' else MODES.get(mode, mode)
            add('card:' + key, item.get('label', key), item.get('value'), data.get('period'), basis,
                source, '亿元' if unit == '元' else unit, 1e8 if unit == '元' else 1)
            comparison = data.get('card_comparisons', {}).get(key, {})
            for change, label in [('yoy', '同比'), ('qoq', '环比')]:
                add(f'card:{key}:{change}', item.get('label', key) + label, comparison.get(change), data.get('period'),
                    ('同口径上年同期' if change == 'yoy' else '期末余额对前期' if statement == 'fzb' else '本季度对上一季度单季') + '；基期须为正', source, '%')
        for i, item in enumerate(section.get('full_items', [])):
            source = source_text(item)
            source = f'本地 financial_reports；{source}；更新时间 {data.get("updated_at") or "未记录"}；当前保存版本'
            unit = item.get('unit', '')
            add(f'{i}:{item.get("field")}', item.get('label', ''), item.get('value'), data.get('period'),
                '期末值' if statement == 'fzb' else MODES.get(mode, mode), source,
                '亿元' if unit == '元' else unit, 1e8 if unit == '元' else 1)
            previous = next((x for x in section.get('comparison_items', []) if x.get('field') == item.get('field')), None)
            if previous:
                add(f'{i}:{item.get("field")}:baseline', item.get('label', '') + '（比较期）', previous.get('value'),
                    section.get('baseline'), '期末值' if statement == 'fzb' else MODES.get(mode, mode),
                    f'本地 financial_reports；{source_text(previous)}；比较期 {section.get("baseline")}',
                    '亿元' if unit == '元' else unit, 1e8 if unit == '元' else 1)
    else:
        raise ValueError('数据分组无效')
    return result
